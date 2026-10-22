"""Capability-based encoder selection; no host/VM/container assumptions."""
from dataclasses import dataclass

@dataclass(frozen=True)
class EncoderBackend:
    name: str
    factories: tuple
    hardware: bool

BACKENDS=(EncoderBackend('va-h264',('vapostproc','vah264enc'),True),
          EncoderBackend('va-h264-lp',('vapostproc','vah264lpenc'),True),
          EncoderBackend('x264',('videoconvertscale','x264enc'),False))

def pipeline_for(name,limits,width,height,rate_limit='videorate'):
    if name=='x264':return limits.software_pipeline(width,height,rate_limit)
    if name not in ('va-h264','va-h264-lp'):raise ValueError('Unknown encoder backend')
    element='vah264lpenc' if name=='va-h264-lp' else 'vah264enc'
    width,height=limits.fit(width,height)
    return (limits.rate_limit(rate_limit)+'vapostproc name=scale ! '
            'capsfilter name=scale_caps caps="video/x-raw(memory:VAMemory),format=NV12,width='+str(width)+',height='+str(height)+'" ! '+
            element+' name=encoder b-frames=0 target-usage=7 bitrate='+str(limits.bitrate_kbps)+' '
            'key-int-max='+str(limits.va_keyframe_interval())+' ! video/x-h264,profile=constrained-baseline')

# At or below this output size 'auto' prefers x264: on a two-core Intel machine at 1152x720 x264 encoded in
# ~12 ms and cut input->frame latency by ~100 ms versus vah264enc, which held frames 100-120 ms.
SOFTWARE_PREFERRED_PIXELS=1280*720

def choose_backend(requested,available,probe,prefer_software=False):
    if requested not in ('auto',*(b.name for b in BACKENDS)):raise ValueError('Unknown encoder backend')
    order=sorted(BACKENDS,key=lambda b:b.hardware) if requested=='auto' and prefer_software else BACKENDS
    for backend in order:
        # The low-power VA encoder was slower on the only tested machine; explicit selection only.
        if requested=='auto' and backend.name=='va-h264-lp':continue
        if requested!='auto' and requested!=backend.name:continue
        if not all(available(factory) for factory in backend.factories):continue
        if not backend.hardware or probe(backend.name):return backend.name
    raise RuntimeError('No usable encoder for '+requested)

def select_gstreamer_backend(Gst,requested,limits,width,height):
    def available(factory):return Gst.ElementFactory.find(factory) is not None
    def probe(name):
        pipeline=None
        try:
            # Two frames at requested output dimensions; bounded wait, no desktop capture.
            w,h=limits.fit(width,height)
            pipeline=Gst.parse_launch('videotestsrc num-buffers=2 pattern=black ! '
                'video/x-raw,format=BGRx,width='+str(w)+',height='+str(h)+',framerate='+str(limits.max_fps)+'/1 ! '+
                pipeline_for(name,limits,w,h)+' ! fakesink sync=false')
            pipeline.set_state(Gst.State.PLAYING)
            result=pipeline.get_bus().timed_pop_filtered(2*Gst.SECOND,Gst.MessageType.EOS|Gst.MessageType.ERROR)
            return result is not None and result.type==Gst.MessageType.EOS
        except Exception:return False
        finally:
            if pipeline:pipeline.set_state(Gst.State.NULL)
    w,h=limits.fit(width,height)
    return choose_backend(requested,available,probe,prefer_software=w*h<=SOFTWARE_PREFERRED_PIXELS)
