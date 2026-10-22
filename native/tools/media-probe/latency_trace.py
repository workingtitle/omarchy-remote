"""Guest-side stage timing for the black/white latency target. Opt-in diagnostics only.

All times are CLOCK_MONOTONIC nanoseconds on the guest, so sender and target
process timestamps are comparable. They are never compared with Mac clocks.
Only mean luma of a sample grid is inspected; no frame content is stored or logged.
"""
import json,threading
from collections import OrderedDict

DARK,BRIGHT='dark','bright'
STAGES=('bridge_ns','capture_ns','scale_in_ns','encoder_in_ns','encoded_ns','payloaded_ns')

def level(mean):
    if mean is None or mean!=mean:return None
    if mean<64:return DARK
    if mean>192:return BRIGHT
    return None

def grid_mean(extract,width,height,stride,offset=0,bytes_per_pixel=1,channel=0):
    """Mean of a 16x16 grid over the central half; extract(offset,size) returns bytes.
    Reads one short span per row instead of mapping/copying the whole frame."""
    if width<16 or height<16 or stride<width*bytes_per_pixel:return None
    first=width//4;span=((width//2)*15//16+1)*bytes_per_pixel
    total=0
    for row in range(16):
        y=height//4+row*(height//2)//16
        data=extract(offset+y*stride+first*bytes_per_pixel,span)
        if data is None or len(data)<span:return None
        for column in range(16):total+=data[column*((width//2)//16)*bytes_per_pixel+channel]
    return total/256

class TransitionTracer:
    """Follows target transitions from capture through encoder output and RTP payloading.
    Probes run on different streaming threads; all state is behind one lock and bounded."""
    def __init__(self,emit,max_pending=8):
        self.emit=emit;self.max_pending=max_pending;self.lock=threading.Lock()
        self.last_key=None;self.capture_level=None;self.scale_level=None;self.encoder_level=None
        self.waiting=[];self.by_index=OrderedDict();self.by_pts=OrderedDict();self.emitted=0;self.dropped=0
        # Encoders may re-timestamp output (x264enc adds a 1000 h offset on its own frame grid),
        # so input/output are paired by order: zero-latency, no-B-frame encoders emit 1:1 in order.
        self.encoder_inputs=0;self.encoder_outputs=0;self.errors=0
    def error(self):
        """Record a probe failure; unreadable frame samples are not failures."""
        with self.lock:self.errors+=1
    def key(self,now):
        with self.lock:self.last_key=now
    def capture(self,mean,now):
        current=level(mean)
        with self.lock:
            if current is None:return
            previous,self.capture_level=self.capture_level,current
            if previous is None or previous==current:return
            self.waiting.append({'level':current,'bridge_ns':self.last_key,'capture_ns':now})
            while len(self.waiting)>self.max_pending:self._drop(self.waiting.pop(0))
    def scaler_input(self,mean,now):
        """Optional point after queue/rate limiting, before conversion/scaling."""
        current=level(mean)
        with self.lock:
            if current is None:return
            previous,self.scale_level=self.scale_level,current
            if previous is None or previous==current:return
            record=next((r for r in self.waiting if r['level']==current and 'scale_in_ns' not in r),None)
            if record is not None:record['scale_in_ns']=now
    def encoder_input(self,mean,now):
        """Call for EVERY buffer entering the encoder; the count pairs it with its output."""
        current=level(mean)
        with self.lock:
            self.encoder_inputs+=1
            if current is None:return
            previous,self.encoder_level=self.encoder_level,current
            if previous is None or previous==current:return
            record=next((r for r in self.waiting if r['level']==current),None)
            if record is None:record={'level':current,'bridge_ns':self.last_key,'capture_ns':None}
            else:self.waiting.remove(record)
            record['encoder_in_ns']=now;self.by_index[self.encoder_inputs]=record
            while len(self.by_index)>self.max_pending:self._drop(self.by_index.popitem(last=False)[1])
    def encoded(self,pts,now):
        """Call for EVERY encoder output buffer."""
        with self.lock:
            self.encoder_outputs+=1
            record=self.by_index.pop(self.encoder_outputs,None)
            if record is None:return
            if pts is None:self._drop(record);return
            record['encoded_ns']=now;self.by_pts[pts]=record
            while len(self.by_pts)>self.max_pending:self._drop(self.by_pts.popitem(last=False)[1])
    def payloaded(self,pts,now):
        with self.lock:
            record=self.by_pts.pop(pts,None)
            if record is None:return
            record['payloaded_ns']=now;self.emitted+=1
        self.emit(record)
    def _drop(self,record):
        self.dropped+=1;self.emit(dict(record,incomplete=True))
    def close(self):
        with self.lock:
            pending=self.waiting+list(self.by_index.values())+list(self.by_pts.values())
            self.waiting=[];self.by_index.clear();self.by_pts.clear()
        for record in pending:self._drop(record)
        with self.lock:
            inputs,outputs,errors=self.encoder_inputs,self.encoder_outputs,self.errors
            incomplete=self.dropped
        return {'emitted':self.emitted,'incomplete':incomplete,
                'encoder_inputs':inputs,'encoder_outputs':outputs,
                'encoder_count_match':inputs==outputs,'errors':errors,
                'valid':incomplete==0 and inputs==outputs and errors==0}

def delta_ms(record,start,end):
    a,b=record.get(start),record.get(end)
    if a is None or b is None or b<a:return None
    return (b-a)/1e6

def percentile(values,p):
    values=sorted(v for v in values if v is not None)
    if not values:return None
    return values[max(0,-(-p*len(values)//100)-1)]

def breakdown(records,toggles,mac_samples=None,trial_meta=None,trace_summary=None):
    """Join sender records with target toggles (by level and time) and optional Mac totals (by order)."""
    records=list(records); incomplete=sum(1 for r in records if r.get('incomplete'))
    rows=[]
    missing_toggle=0; missing_stage=0
    for record in records:
        if record.get('incomplete'):continue
        row=dict(record)
        anchor=record.get('capture_ns') or record.get('encoder_in_ns')
        match=None
        for toggle in toggles:
            if toggle['level']==record['level'] and toggle['draw_ns']<=anchor and (record.get('bridge_ns') is None or toggle['key_ns']>=record['bridge_ns']):
                match=toggle
        if match:row.update(key_ns=match['key_ns'],draw_ns=match['draw_ns'])
        else:missing_toggle+=1
        if any(record.get(key) is None for key in ('bridge_ns','encoder_in_ns','encoded_ns','payloaded_ns')):missing_stage+=1
        rows.append(row)
    stages=[('bridge_to_key',   'bridge_ns','key_ns'),
            ('key_to_draw',     'key_ns','draw_ns'),
            ('draw_to_capture', 'draw_ns','capture_ns'),
            ('capture_to_encoder_in','capture_ns','encoder_in_ns'),
            ('capture_to_scale_in','capture_ns','scale_in_ns'),
            ('scale_in_to_encoder_in','scale_in_ns','encoder_in_ns'),
            ('encode',          'encoder_in_ns','encoded_ns'),
            ('encoded_to_payloaded','encoded_ns','payloaded_ns'),
            ('guest_total',     'bridge_ns','payloaded_ns')]
    per_stage={name:[delta_ms(r,a,b) for r in rows] for name,a,b in stages}
    trace_summary=trace_summary or {}
    summary_present=bool(trace_summary)
    count_match=trace_summary.get('encoder_count_match',False)
    trace_errors=trace_summary.get('errors',0)
    emitted_match=trace_summary.get('emitted')==len(rows)
    summary_clean=(trace_summary.get('valid') is True and trace_summary.get('incomplete')==0 and
                   emitted_match)
    clean_trace=(summary_present and summary_clean and incomplete==0 and
                 missing_toggle==0 and missing_stage==0 and count_match and trace_errors==0)
    result={'records':len(rows),'incomplete_records':incomplete,
            'missing_toggles':missing_toggle,'missing_stage_records':missing_stage,
            'trace_errors':trace_errors,'encoder_count_match':count_match,
            'summary_present':summary_present,'emitted_match':emitted_match,
            'valid':clean_trace,'stages':{}}
    trial_clean=(trial_meta is not None and trial_meta.get('outcome')=='completed' and
                 trial_meta.get('timeouts')==0 and trial_meta.get('measured')==trial_meta.get('requested_trials') and
                 trial_meta.get('requested_trials')==len(rows))
    if (mac_samples is not None and len(mac_samples)==len(rows) and
            len(toggles)==len(rows) and clean_trace and trial_clean):
        per_stage['mac_minus_guest']=[None if g is None else m-g for m,g in zip(mac_samples,per_stage['guest_total'])]
    elif mac_samples is not None:
        result['mac_join']='skipped: coverage or trace validity failed'
    for name,values in per_stage.items():
        present=[v for v in values if v is not None]
        result['stages'][name]={'count':len(present),'p50':percentile(present,50),'p95':percentile(present,95)}
    return result

def parse_lines(lines,prefix):
    out=[]
    for line in lines:
        index=line.find(prefix+' ')
        if index<0:continue
        try:value=json.loads(line[index+len(prefix)+1:])
        except ValueError:continue
        if isinstance(value,dict):
            if len(out)>=4096:raise ValueError('latency log record bound exceeded')
            out.append(value)
    return out
