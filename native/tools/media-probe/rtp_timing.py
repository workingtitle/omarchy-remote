"""Constant-space RTP timing diagnostics. Never retains payload or audio content."""
import struct,time
class RtpTiming:
    def __init__(self):
        self.count=0;self.previous=None;self.first_wall=None;self.rtp_span_ticks=0
        self.max_gap_ms=0.;self.max_tick_gap=0;self.nonstandard=0;self.backwards=0
    def observe(self,header,now=None):
        if len(header)<12 or header[0]>>6!=2:return
        now=time.monotonic() if now is None else now
        timestamp=struct.unpack('!I',header[4:8])[0]
        if self.first_wall is None:self.first_wall=now
        if self.previous is not None:
            wall,old=self.previous;delta=(timestamp-old)&0xffffffff
            self.max_gap_ms=max(self.max_gap_ms,(now-wall)*1000)
            if delta>=0x80000000:self.backwards+=1
            else:
                self.rtp_span_ticks+=delta
                self.max_tick_gap=max(self.max_tick_gap,delta)
                if delta!=480:self.nonstandard+=1
        self.previous=(now,timestamp);self.count+=1
    def summary(self):
        wall_span_ms=None;rtp_span_ms=None;span_difference_ms=None
        if self.count>=2 and self.backwards==0:
            wall_span_ms=(self.previous[0]-self.first_wall)*1000
            rtp_span_ms=self.rtp_span_ticks/48
            span_difference_ms=wall_span_ms-rtp_span_ms
        return dict(packets=self.count,max_arrival_gap_ms=self.max_gap_ms,max_rtp_gap_ticks=self.max_tick_gap,
                    non_480_tick_gaps=self.nonstandard,backwards_timestamps=self.backwards,
                    observed_wall_span_ms=wall_span_ms,rtp_span_ms=rtp_span_ms,
                    span_difference_ms=span_difference_ms)
