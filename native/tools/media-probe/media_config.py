"""Validated media limits, independent of compositor, VM, transport and UI."""
from dataclasses import dataclass

@dataclass(frozen=True)
class VideoLimits:
    max_width: int = 1280
    max_height: int = 720
    max_fps: int = 30
    bitrate_kbps: int = 4000
    workers: int = 2
    # Keyframes every second were sent as ~150 KB bursts; on Intel Wi-Fi those bursts lost ~124 video
    # packets each and took interleaved audio packets with them. 10 s cut audio loss 4-13x.
    keyframe_seconds: int = 10

    def __post_init__(self):
        for name,low,high in [('max_width',64,7680),('max_height',64,4320),
                              ('max_fps',10,120),('bitrate_kbps',250,50000),('workers',1,8),('keyframe_seconds',1,600)]:
            value=getattr(self,name)
            if type(value)!=int or not low<=value<=high:
                raise ValueError('Invalid '+name)

    def fit(self, width, height):
        if type(width)!=int or type(height)!=int or not (1<=width<=16384 and 1<=height<=16384):
            raise ValueError('Invalid source dimensions')
        # The limit box follows the source orientation, so a portrait phone gets the same pixel budget.
        box_w,box_h=(self.max_height,self.max_width) if height>width else (self.max_width,self.max_height)
        ratio=min(1,box_w/width,box_h/height)
        return max(2,int(width*ratio)//2*2),max(2,int(height*ratio)//2*2)

    def keyframe_interval(self):
        """Frames between periodic keyframes; the receiver requests extra ones (PLI) after loss."""
        return self.max_fps*self.keyframe_seconds

    def va_keyframe_interval(self):
        """vah264enc accepts at most 1024; larger values silently fall back to its automatic GOP."""
        return min(1024,self.keyframe_interval())

    def rate_limit(self,mode='videorate'):
        """'videorate' limits in GStreamer (drop-only; may hold a frame until the next one).
        'capture' relies on PipeWire max-framerate negotiation instead; the caller must add it."""
        if mode in ('capture','leading'):return ''
        if mode!='videorate':raise ValueError('Invalid rate limit')
        return ('videorate name=rate drop-only=true max-rate='+str(self.max_fps)+' ! '
                'video/x-raw,framerate='+str(self.max_fps)+'/1 ! ')

    def leading_limiter(self):return LeadingEdgeLimiter(self.max_fps)

    def software_pipeline(self,width,height,rate_limit='videorate'):
        width,height=self.fit(width,height)
        # Limit frame work BEFORE CPU scaling/encoding. Drop-only never synthesizes frames.
        return (self.rate_limit(rate_limit)+
                'videoconvertscale name=scale n-threads='+str(self.workers)+' ! '
                'capsfilter name=scale_caps caps="video/x-raw,format=I420,width='+str(width)+',height='+str(height)+'" ! '
                'x264enc name=encoder tune=zerolatency speed-preset=ultrafast '
                'threads='+str(self.workers)+' bitrate='+str(self.bitrate_kbps)+' '
                'key-int-max='+str(self.keyframe_interval())+' bframes=0 ! '
                'video/x-h264,profile=constrained-baseline')


class LeadingEdgeLimiter:
    """Frame-rate limit that never holds a frame: a frame passes at once if its slot is open,
    otherwise it is dropped. Slots advance on a fixed grid, so the long-run rate stays at or
    below fps. Relies on the source re-sending its latest frame while idle (pipewiresrc
    keepalive-time), so a dropped final update is delivered at the next open slot."""
    def __init__(self,fps,slack_ns=4_000_000):
        if type(fps)!=int or not 10<=fps<=120:raise ValueError('Invalid fps')
        self.interval=1_000_000_000//fps;self.slack=slack_ns;self.next=None
    def allow(self,now):
        if self.next is not None and now<self.next-self.slack:return False
        # Continue the grid while frames keep coming; restart it after an idle gap.
        self.next=now+self.interval if self.next is None or now-self.next>self.interval else self.next+self.interval
        return True


def physical_cores(root='/sys/devices/system/cpu',fallback=None):
    """Physical cores (hyperthreads merged via core_cpus_list/thread_siblings_list). A 2-core
    Intel machine with hyperthreading reports os.cpu_count()==4, which must not count as a capable guest."""
    import glob,os
    groups=set()
    for topology in glob.glob(os.path.join(root,'cpu[0-9]*','topology')):
        for name in ('core_cpus_list','thread_siblings_list'):
            try:
                with open(os.path.join(topology,name)) as handle:groups.add(handle.read(64).strip());break
            except OSError:continue
    return len(groups) or (fallback if fallback is not None else os.cpu_count())


def auto_max_fps(cpu_count):
    """Default FPS limit when the client does not request one: 60 with at least 4 physical cores,
    else 30. A two-core Intel machine missed its input lease at 60 FPS; an 8-core ARM guest encoded in ~1.5 ms."""
    return 60 if isinstance(cpu_count,int) and cpu_count>=4 else 30


def auto_video_defaults(cpu_count):
    """Default (max_width, max_height, workers) when the client does not request limits.
    With >=4 CPUs the guest's native output is streamed up to 3840x2400: on the 8-core ARM guest
    2940x1848 encoded in ~5.5 ms and did not add latency versus 720p. Weaker guests keep 720p."""
    if isinstance(cpu_count,int) and cpu_count>=4:return 3840,2400,4
    return 1280,720,2


def auto_bitrate_kbps(width,height):
    """Scale the 720p default of 4 Mbit/s with the pixel count, rounded to 500, within 4..20 Mbit/s."""
    kbps=width*height/(1280*720)*4000
    return int(min(20000,max(4000,round(kbps/500)*500)))

def video_stats_due(now,last_report,in_fps,out_fps,max_fps,every=60):
    """VIDEO_STATS once a minute, and at once when the encoder fell behind: fewer than 90 % of the frames it could
    have sent (the capture's rate, capped at max_fps). A still desktop delivers few frames and is not a dip."""
    if not in_fps and not out_fps: return False
    return now-last_report>=every or out_fps<0.9*min(in_fps,max_fps)
