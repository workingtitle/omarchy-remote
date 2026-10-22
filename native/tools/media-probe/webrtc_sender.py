#!/usr/bin/env python3
"""Experimental GStreamer sender. Signaling ONLY over authenticated SSH stdio."""
import argparse, dataclasses, json, os, re, sys, threading, signal, fcntl, time, subprocess
from input_bridge import InputBridge
from media_config import VideoLimits
from rtp_timing import RtpTiming
from startup_watchdog import StartupWatchdog,startup_error_code
from capture_bootstrap import CaptureBootstrap
from audio_sink import DefaultSinkWatcher,default_sink_name,SinkLookupError
import audio_source
from session_watchdog import SessionWatchdog
from av_sync import configure_sync,sdp_sync_flags
from encoder import select_gstreamer_backend,pipeline_for
import gi
gi.require_version('Gst','1.0'); gi.require_version('GstWebRTC','1.0'); gi.require_version('GstSdp','1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
from portal import ScreenCast

Gst.init(None)
args=argparse.ArgumentParser()
args.add_argument('--audio-diagnostics',action='store_true')
args.add_argument('--request-capture-rate',action='store_true',help='Experimental PipeWire max-framerate negotiation')
args.add_argument('--desktop',action='store_true')
args.add_argument('--capture-backend',choices=('portal','gsr'),default='portal')
# aligned-strict: PipeWire source timestamps made sample-continuous by audiorate. On ARM it removed the
# late-packet concealment of arrival timestamps (3-4 % -> 0 %, jitter buffer ~80 -> ~43 ms).
# On a two-core Intel machine under load (load average 6 on 2 cores) aligned-strict (audiorate tolerance 0) turned every
# timestamp jitter into a silent gap: 120 gaps and 600 ms of silence in 8.6 s of a steady tone, against 7 gaps
# with tolerance 10 ms (measured with the sender's audio path in gst-launch). So 'aligned' is the default.
args.add_argument('--audio-timestamps',choices=('arrival','source','aligned','aligned-strict','gated','sample-clock'),default='aligned')
# Opus in-band FEC exists only in SILK/hybrid modes; at 96 kbit/s stereo Opus uses CELT, so it did not
# reduce concealment on Intel Wi-Fi loss (concealment matched the loss rate). Off by default.
args.add_argument('--audio-fec-percent',type=int,default=0,help='Opus in-band FEC for this expected loss percentage; 0 disables')
args.add_argument('--no-mic',action='store_true',help='do not offer the controlling device a microphone channel (virtual_mic.py)')
args.add_argument('--video-timestamps',choices=('arrival','source'),default='arrival')
args.add_argument('--system-audio',action='store_true',help='Use system monitor with synthetic video for isolated diagnostics')
args.add_argument('--max-width',type=int,default=None,help='default: native up to 3840 with >=4 CPUs, else 1280')
args.add_argument('--max-height',type=int,default=None,help='default: native up to 2400 with >=4 CPUs, else 720')
args.add_argument('--max-fps',type=int,default=None,help='default: 60 with >=4 CPUs, else 30')
args.add_argument('--bitrate-kbps',type=int,default=None,help='default: scaled with the streamed resolution')
args.add_argument('--rtp-mtu',type=int,default=1400)
args.add_argument('--keyframe-seconds',type=int,default=10,help='periodic keyframe interval; the receiver requests more after loss')
args.add_argument('--workers',type=int,default=None,help='default: 4 with >=4 CPUs, else 2')
args.add_argument('--encoder',choices=('auto','x264','va-h264','va-h264-lp'),default='auto')
args.add_argument('--display',choices=('mirror','remote','extend'),default='mirror',help='remote: a headless output sized for the client, physical outputs off for the session; extend: the headless output as an extra screen beside them (virtual_display.py)')
args.add_argument('--side',choices=('right','left'),default='right',help='extend: which side of the physical outputs')
args.add_argument('--quality',choices=('fast','sharp'),default='fast',help='sharp: hardware encoder up to 2560x1600 where the CPU default caps the size; falls back to fast')
args.add_argument('--rate-limit',choices=('videorate','capture','leading'),default='videorate',help='capture: PipeWire max-framerate; leading: drop at capture without holding frames')
args.add_argument('--latency-trace',action='store_true',help='Stage timing for latency_target.py; samples mean luma only')
options=args.parse_args()
if not 512<=options.rtp_mtu<=1400:args.error("rtp-mtu must be 512..1400")
if not 0<=options.audio_fec_percent<=30:args.error("audio-fec-percent must be 0..30")
opus_fec=' inband-fec=true packet-loss-percentage='+str(options.audio_fec_percent) if options.audio_fec_percent else ''
from media_config import auto_max_fps,auto_video_defaults,auto_bitrate_kbps,physical_cores,video_stats_due
_cores=physical_cores()
if options.max_fps is None:options.max_fps=auto_max_fps(_cores)
_auto_width,_auto_height,_auto_workers=auto_video_defaults(_cores)
_explicit_size=options.max_width is not None or options.max_height is not None
if options.max_width is None:options.max_width=_auto_width
if options.max_height is None:options.max_height=_auto_height
if options.workers is None:options.workers=_auto_workers
# Without an explicit bitrate it is resolved from the streamed resolution once the source size is known.
SHARP_SIZE=(2560,1600)
limits=VideoLimits(options.max_width,options.max_height,options.max_fps,options.bitrate_kbps or 4000,options.workers,options.keyframe_seconds)
loop=GLib.MainLoop(); pipeline=None; peer=None; portal=None; gsr=None; pending=[]; remote_ready=False; startup_watchdog=None; startup_watchdog_source=None; startup_watchdog_probes=[]; capture_bootstrap=None; sink_watcher=None; clipboard_sync=None; display_mode=None
state_dir=os.path.expanduser('~/.local/state/omarchy-remote')
os.makedirs(state_dir,mode=0o700,exist_ok=True)
controller_lock=os.open(state_dir+'/webrtc-controller.lock',os.O_RDWR|os.O_CREAT,0o600)
if options.desktop:
    # The mains-powered laptop in power-saver starves the session (power_profile.py); restored by the service after the last session.
    try:
        from power_profile import PowerProfile
        print('POWER_PROFILE '+str(PowerProfile().apply()),file=sys.stderr,flush=True)
    except Exception as error:print('POWER_PROFILE failed '+type(error).__name__,file=sys.stderr,flush=True)
try: fcntl.flock(controller_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit('Another WebRTC controller is already connected')
lock=threading.Lock()
input_bridge=None; input_channel=None; virtual_display=None; virtual_mic=None; captured_position=None
audio_timing=RtpTiming()
video_stages={}
latency_tracer=None
session_watchdog=SessionWatchdog()
last_data_message=[time.monotonic()]
def check_session():
    connection=peer.get_property('connection-state').value_nick if peer else 'new'
    ice_state=peer.get_property('ice-connection-state').value_nick if peer else 'new'
    reason=session_watchdog.check(connection,ice_state)
    if reason:
        # Never try to send an error on a potentially blocked signaling pipe.
        print('SESSION_END %s data_age=%.1fs'%(reason,time.monotonic()-last_data_message[0]),file=sys.stderr,flush=True)
        loop.quit()
        return False
    return True
signal.signal(signal.SIGTERM,lambda *_: GLib.idle_add(loop.quit))
signal.signal(signal.SIGHUP,lambda *_: GLib.idle_add(loop.quit))
def send(message):
    wire=json.dumps(message,separators=(',',':'))+'\n'
    if len(wire)>131072: raise RuntimeError('Oversized signaling message')
    with lock: sys.stdout.write(wire); sys.stdout.flush()
def stop_startup_watchdog():
    global startup_watchdog, startup_watchdog_source, startup_watchdog_probes
    if startup_watchdog:
        startup_watchdog.cancel()
    for pad,probe_id in startup_watchdog_probes:
        try: pad.remove_probe(probe_id)
        except Exception: pass
    startup_watchdog_probes=[]
    if startup_watchdog_source:
        GLib.source_remove(startup_watchdog_source)
    startup_watchdog_source=None
    startup_watchdog=None
def fail(error):
    print(str(error),file=sys.stderr)
    # One journal line with the cause (GStreamer/portal error text; never media, input or SDP).
    print('SENDER_ERROR '+type(error).__name__+' '+' '.join(str(error).split())[:160],file=sys.stderr,flush=True)
    try: send({'type':'error','code':startup_error_code(error)})
    except Exception: pass
    loop.quit()
def incoming_pad(_peer,pad):
    """The controlling device's microphone: decoded into the virtual microphone's sink."""
    global virtual_mic
    if pad.get_direction()!=Gst.PadDirection.SRC:return
    caps=pad.get_current_caps()
    if caps is not None and 'audio' not in caps.to_string():return
    try:
        from virtual_mic import VirtualMic
        mic=VirtualMic()
        if not mic.start():
            print('MIC_INPUT unavailable',file=sys.stderr,flush=True);return
        virtual_mic=mic
        branch=Gst.parse_bin_from_description('rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! queue max-size-time=100000000 leaky=downstream ! taginject tags="title=Omarchy-Remote-Mic-Feed" scope=stream ! pulsesink device='+mic.sink+' sync=false',True)
        pipeline.add(branch);branch.sync_state_with_parent()
        pad.link(branch.get_static_pad('sink'))
        print('MIC_INPUT ready',file=sys.stderr,flush=True)
        GLib.timeout_add_seconds(1,lambda:(mic.keep_feed() or True) if virtual_mic is mic else False)   # the audio panel can move the feed to the speakers
    except Exception as error:
        print('MIC_INPUT failed '+type(error).__name__,file=sys.stderr,flush=True)
def offer_created(promise,*_):
    stop_startup_watchdog()
    reply=promise.get_reply(); offer=reply.get_value('offer')
    # Log only negotiated feature flags, never SDP, SSRCs or network details.
    description=offer.sdp.as_text()
    clock=pipeline.get_clock()
    shared_clock=clock is not None and peer.get_clock()==clock
    print('AV_SYNC '+json.dumps(dict(sdp_sync_flags(description),common_pipeline_clock=shared_clock,**av_clock_flags)),file=sys.stderr)
    video_sections=[section for section in description.split('m=')[1:] if section.startswith('video ')]
    nack=any(re.search(r'^a=rtcp-fb:\d+ nack\r?$',section,re.MULTILINE) for section in video_sections)
    rtx=any(re.search(r'^a=rtpmap:\d+ RTX/',section,re.MULTILINE|re.IGNORECASE) for section in video_sections)
    print('VIDEO_RECOVERY '+json.dumps({'offered_nack':nack,'offered_rtx':rtx}),file=sys.stderr)
    peer.emit('set-local-description',offer,Gst.Promise.new())
    send({'type':'offer','sdp':offer.sdp.as_text()})
def negotiate(_):
    peer.emit('create-offer',None,Gst.Promise.new_with_change_func(offer_created,None,None))
def ice(_,index,candidate):
    if candidate: send({'type':'ice','index':index,'candidate':candidate})
def answered(promise,*_):
    global remote_ready
    remote_ready=True
    for msg in pending: peer.emit('add-ice-candidate',msg['index'],msg['candidate'])
    pending.clear()
def receive(msg):
    try:
        if msg['type']=='heartbeat': session_watchdog.heartbeat(msg)
        elif msg['type']=='answer':
            result,sdp=GstSdp.SDPMessage.new()
            if GstSdp.sdp_message_parse_buffer(msg['sdp'].encode(),sdp)!=GstSdp.SDPResult.OK: raise ValueError('Invalid SDP')
            peer.emit('set-remote-description',GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER,sdp),Gst.Promise.new_with_change_func(answered,None,None))
        elif msg['type']=='ice':
            if type(msg['index'])!=int or not 0<=msg['index']<8 or not isinstance(msg['candidate'],str) or len(msg['candidate'])>4096: raise ValueError('Invalid ICE')
            if remote_ready: peer.emit('add-ice-candidate',msg['index'],msg['candidate'])
            elif len(pending)<128: pending.append(msg)
            else: raise ValueError('Too many candidates')
        elif msg['type']=='stop': loop.quit()
        else: raise ValueError('Unknown message')
    except Exception as error: fail(error)
    return False
def reader():
    # Raw reads avoid Python's buffered-stdin lock during daemon shutdown.
    slots=threading.BoundedSemaphore(128)
    def deliver(msg):
        try: return receive(msg)
        finally: slots.release()
    try:
        buffer=b''
        while True:
            data=os.read(sys.stdin.fileno(),16384)
            if not data:
                if buffer: raise ValueError('Truncated signal')
                break
            buffer+=data
            while b'\n' in buffer:
                line,buffer=buffer.split(b'\n',1)
                if len(line)>131072: raise ValueError('Oversized signal')
                msg=json.loads(line)
                if not isinstance(msg,dict): raise ValueError('Expected signaling object')
                if not slots.acquire(blocking=False): raise ValueError('Signaling backlog exceeded')
                GLib.idle_add(deliver,msg)
                if msg.get('type')=='stop': return
            if len(buffer)>131072: raise ValueError('Oversized signal')
    except Exception as error: print(str(error),file=sys.stderr)
    GLib.idle_add(loop.quit)
def message(bus,msg):
    if msg.type==Gst.MessageType.ERROR:
        error,debug=msg.parse_error(); fail(error)
# The picture is dropped at the encoder's output while the client's app is in the background (sound goes on).
video_gate={'paused':False,'installed':False}
def set_video_active(active):
    encoder=pipeline.get_by_name('encoder') if pipeline else None
    if encoder is None:
        print('VIDEO_PAUSE unsupported',file=sys.stderr,flush=True);return
    if not video_gate['installed']:
        def gate(_pad,_info):return Gst.PadProbeReturn.DROP if video_gate['paused'] else Gst.PadProbeReturn.OK
        encoder.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,gate)
        video_gate['installed']=True
    video_gate['paused']=not active
    if active:
        # The first picture after the pause must be a keyframe, or the receiver shows garbage until the next one.
        try:
            gi.require_version('GstVideo','1.0'); from gi.repository import GstVideo
            encoder.send_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE,True,0))
        except Exception as error:print('VIDEO_PAUSE keyframe-failed '+type(error).__name__,file=sys.stderr,flush=True)
    print('VIDEO_PAUSE active=%s'%active,file=sys.stderr,flush=True)

def start_clipboard():
    # Opt-in text clipboard on its own reliable channel; content is never logged.
    global clipboard_sync
    from clipboard_sync import ClipboardSync
    env=os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR','/run/user/%d'%os.getuid())
    for line in subprocess.check_output(['systemctl','--user','show-environment'],text=True,timeout=3).splitlines():
        name,sep,value=line.partition('=')
        if sep and name=='WAYLAND_DISPLAY':env[name]=value
    channel=peer.emit('create-data-channel','clipboard',None)
    def send_text(message):
        GLib.idle_add(lambda:(channel.emit('send-string',message),False)[1])
    clipboard_sync=ClipboardSync(send_text,env)
    channel.connect('on-open',lambda *_:print('CLIPBOARD_CHANNEL open',file=sys.stderr,flush=True))
    def received(_channel,text):
        try:clipboard_sync.handle(text)
        except ValueError:print('CLIPBOARD_REJECTED invalid-message',file=sys.stderr,flush=True)
    channel.connect('on-message-string',received)
def start_display():
    # Dynamic resolution: the client reports its video area in device pixels; only a single
    # virtual output is changed, and its original mode is restored when the session ends.
    global display_mode
    from display_mode import DisplayMode
    from guest_state import hyprland_env,bounded_command
    env=hyprland_env()
    def monitors():return bounded_command(['hyprctl','-j','monitors'],env)
    def evaluate(lua):
        try:output=bounded_command(['hyprctl','eval',lua],env,max_output_bytes=4096)
        except Exception:return False
        return 'error' not in output.lower()
    channel=peer.emit('create-data-channel','display',None)
    def send_result(message):
        result=json.loads(message)
        print('DISPLAY_MODE applied=%s reason=%s size=%dx%d scale=%.2f'%(result['applied'],result['reason'],result['width'],result['height'],result['scale'] or 0),file=sys.stderr,flush=True)
        if result.get('applied') and result.get('scale'):geometry['w']=result['width'];geometry['s']=result['scale']
        channel.emit('send-string',message)
    display_mode=DisplayMode(monitors,evaluate,send_result,os.path.join(state_dir,'display-restore.json'),limit=(limits.max_width,limits.max_height))
    if not display_mode.restore():print('DISPLAY_MODE restore-failed',file=sys.stderr,flush=True)
    if virtual_display:display_mode.output=virtual_display.name;display_mode.left_of=virtual_display.left_of   # the extra screen, not the physical ones
    display_mode.keep_mode=options.display=='mirror'   # the machine's own screen as it is: not resized to the device, not even a VM's
    # The bar (compact_bar.py BarSync): short for a phone upright, space for the iPad's microphone dot; written in one pass,
    # never while a full-screen window hides the bar. A short bar left by the session before (another device took over; the
    # service restores later) is stale: the original comes back first.
    from compact_bar import CompactBar,BarSync,has_fullscreen
    try:CompactBar().restore()
    except Exception as error:print('COMPACT_BAR stale-restore '+type(error).__name__,file=sys.stderr,flush=True)
    def fullscreen_now():
        try:return has_fullscreen(bounded_command(['hyprctl','-j','activeworkspace'],env))
        except Exception:return False
    bar_sync=BarSync(CompactBar(),fullscreen_now,lambda ms,fn:GLib.timeout_add(ms,lambda:(fn(),False)[1]),GLib.source_remove)
    if virtual_display and options.display=='remote':
        # The shell recreates the bar after every mode change and the new bar shows over a full-screen window: renew its state.
        from bar_nudge import BarNudge
        bar_nudge=BarNudge(lambda arguments:bounded_command(['hyprctl']+arguments,env),lambda:display_mode.output,
                           lambda ms,fn:GLib.timeout_add(ms,lambda:(fn(),False)[1]),GLib.source_remove)
        display_mode.on_applied=lambda width,height,scale:(bar_sync.display(width,scale),bar_nudge.display_changed())
        # A key or a click on the machine's own keyboard or trackpad takes the machine back: the physical screens are off
        # (black) for this session, so the service ends it (close reason "Taken back at the machine") and they come back.
        try:
            from local_reclaim import LocalReclaim
            def reclaimed():
                print('LOCAL_RECLAIM',file=sys.stderr,flush=True)
                try:send({'v':1,'type':'local-reclaim'})
                except Exception:pass
            watched=LocalReclaim(reclaimed).start()
            print('LOCAL_RECLAIM watching=%d'%watched,file=sys.stderr,flush=True)
        except Exception as error:print('LOCAL_RECLAIM unavailable '+type(error).__name__,file=sys.stderr,flush=True)
    mic_dot=[0.0,False]   # last message time, space currently made
    geometry={}
    def mic_dot_update(on,edge,width):
        size=None
        try:
            if on and edge and width and geometry.get('w') and geometry.get('s'):
                size=float(edge)/float(width)*geometry['w']/geometry['s']
        except (TypeError,ValueError,ZeroDivisionError):size=None
        mic_dot[0]=time.monotonic()
        if on or mic_dot[1]:
            bar_sync.dot(on,size)
            if on!=mic_dot[1]:print('MIC_DOT on=%s size=%s'%(on,'-' if size is None else int(size)),file=sys.stderr,flush=True)
            mic_dot[1]=on
    def mic_dot_expire():
        if mic_dot[1] and time.monotonic()-mic_dot[0]>12:
            bar_sync.dot(False);mic_dot[1]=False
            print('MIC_DOT expired',file=sys.stderr,flush=True)
        return True
    GLib.timeout_add_seconds(3,mic_dot_expire)
    def received(_channel,text):
        # {"v":1,"type":"bitrate","factor":f}: the client saw sustained packet loss (or a clean stretch) and asks
        # for less (or more) data at the same resolution; bounded to 1500 kbps … the automatic bitrate.
        if len(text)<=256 and '"bitrate"' in text:
            try:
                hint=json.loads(text)
                if hint.get('v')==1 and hint.get('type')=='bitrate':
                    factor=float(hint['factor'])
                    encoder=pipeline.get_by_name('encoder') if pipeline else None
                    if encoder and 0.25<=factor<=4:
                        # The ceiling follows the current picture size (limits holds the size at session start).
                        ceiling=options.bitrate_kbps or limits.bitrate_kbps
                        caps=pipeline.get_by_name('scale_caps')
                        if caps and options.bitrate_kbps is None:
                            structure=caps.get_property('caps').get_structure(0)
                            ok_w,width=structure.get_int('width');ok_h,height=structure.get_int('height')
                            if ok_w and ok_h:ceiling=auto_bitrate_kbps(width,height)
                        kbps=int(min(ceiling,max(1500,encoder.get_property('bitrate')*factor)))
                        encoder.set_property('bitrate',kbps)
                        print('BITRATE kbps=%d'%kbps,file=sys.stderr,flush=True)
                    return
            except (ValueError,KeyError,TypeError):return
        # {"v":1,"type":"video","active":bool}: the client's app is in the background and cannot show the picture; the
        # video is dropped at the encoder's output (sound goes on) and a keyframe comes first when it is wanted again.
        if len(text)<=256 and '"video"' in text:
            try:
                hint=json.loads(text)
                if hint.get('v')==1 and hint.get('type')=='video' and isinstance(hint.get('active'),bool):
                    set_video_active(hint['active']);return
            except (ValueError,KeyError,TypeError):return
        # {"v":1,"type":"mic","on":bool,"edge":pt,"width":pt}: the iPad's orange microphone dot is showing and covers the bar's
        # right end (`edge` points from the right edge of a view `width` points wide). Repeated every 4 s while it shows; with no
        # message for 12 s the space goes by itself (the iPad vanished).
        if len(text)<=256 and '"mic"' in text:
            try:
                hint=json.loads(text)
                if hint.get('v')==1 and hint.get('type')=='mic' and isinstance(hint.get('on'),bool):
                    mic_dot_update(hint['on'],hint.get('edge'),hint.get('width'));return
            except (ValueError,KeyError,TypeError):return
        try:display_mode.request(text)
        except ValueError:print('DISPLAY_MODE rejected invalid-message',file=sys.stderr,flush=True)
    channel.connect('on-message-string',received)
    def update_pointer_region():
        # Map client positions onto the streamed output, not the whole layout (several outputs).
        from virtual_display import pointer_region,active
        try:
            text=monitors();outputs=active(text)
            if virtual_display:name=virtual_display.name
            elif len(outputs)==1:name=outputs[0]['name']
            else:
                at=tuple(int(v) for v in captured_position) if captured_position else None
                name=next((m['name'] for m in outputs if (m['x'],m['y'])==at),None)
            region=pointer_region(text,name) if name else None
            if region and input_bridge:input_bridge.region=region
        except Exception as error:print('POINTER_REGION error '+type(error).__name__,file=sys.stderr,flush=True)
    update_pointer_region()
    # The focused window for the client's status strip (active_window.py), once a second, sent only on change.
    from active_window import window_message
    last_window=[None]
    def update_window():
        try:message=window_message(bounded_command(['hyprctl','-j','activewindow'],env))
        except Exception:return
        if message!=last_window[0] and channel.get_property('ready-state')==GstWebRTC.WebRTCDataChannelState.OPEN:
            last_window[0]=message;channel.emit('send-string',message)
    def keep_outputs():
        # A reload of the compositor's configuration (Omarchy's theme change) resets the streamed output's mode and switches the
        # physical screens on again: both are put back.
        if not (virtual_display and options.display=='remote'):return
        try:
            off=virtual_display.reassert_isolation()
            if off:print('VIRTUAL_DISPLAY reasserted off=%s'%','.join(off),file=sys.stderr,flush=True)
            if display_mode.reassert(time.monotonic_ns()):update_pointer_region()
        except Exception as error:print('DISPLAY_MODE keep error '+type(error).__name__,file=sys.stderr,flush=True)
    ticks=[0]
    def tick():
        try:
            if display_mode.tick(time.monotonic_ns()):update_pointer_region()
        except Exception as error:print('DISPLAY_MODE error '+type(error).__name__,file=sys.stderr,flush=True)
        ticks[0]+=1
        if ticks[0]%8==0:keep_outputs()   # every 2 s
        if ticks[0]%20==0:update_pointer_region()   # every 5 s: outputs added, moved or rescaled
        if ticks[0]%4==0:update_window()
        return True
    GLib.timeout_add(250,tick)
def follow_source_size():
    # When the captured output changes size, re-fit the encoder input and (auto) bitrate in the
    # streaming thread before the new caps reach the scaler; H.264 carries the new size in-band.
    scale_caps=pipeline.get_by_name('scale_caps'); encoder=pipeline.get_by_name('encoder')
    capture=pipeline.get_by_name('capture')
    if not (scale_caps and encoder and capture):return
    current={'size':None}
    def on_event(_pad,info):
        event=info.get_event()
        if event is None or event.type!=Gst.EventType.CAPS:return Gst.PadProbeReturn.OK
        structure=event.parse_caps().get_structure(0)
        ok_w,width=structure.get_int('width'); ok_h,height=structure.get_int('height')
        if not (ok_w and ok_h):return Gst.PadProbeReturn.OK
        fitted=limits.fit(width,height)
        if current['size'] is not None and fitted!=current['size']:
            old=scale_caps.get_property('caps').copy()
            old.set_value('width',fitted[0]); old.set_value('height',fitted[1])
            scale_caps.set_property('caps',old)
            if options.bitrate_kbps is None:encoder.set_property('bitrate',auto_bitrate_kbps(*fitted))
            print('VIDEO_RESIZE %dx%d'%fitted,file=sys.stderr,flush=True)
        current['size']=fitted
        return Gst.PadProbeReturn.OK
    capture.get_static_pad('src').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM,on_event)
    # VA scaler: while the captured output changes size (remote display mode resizes its headless output),
    # a frame of the old size can follow the new caps; vapostproc rejects it ("invalid video buffer") and the
    # stream stops. Such stale frames are dropped here; x264's software scaler copes on its own.
    scale=pipeline.get_by_name('scale')
    if scale and scale.get_factory().get_name()=='vapostproc':
        gi.require_version('GstVideo','1.0'); from gi.repository import GstVideo
        expected={'caps':None,'info':None};dropped=[0]
        def stale(pad,info):
            buffer=info.get_buffer();caps=pad.get_current_caps()
            if buffer is None or caps is None:return Gst.PadProbeReturn.OK
            if expected['caps'] is None or not caps.is_equal(expected['caps']):
                expected['caps']=caps;expected['info']=GstVideo.VideoInfo.new_from_caps(caps)
            video=expected['info'];meta=GstVideo.buffer_get_video_meta(buffer)
            wrong=((meta.width,meta.height)!=(video.width,video.height)) if meta else buffer.get_size()<video.size
            if not wrong:return Gst.PadProbeReturn.OK
            dropped[0]+=1
            if dropped[0]<=3 or dropped[0]%100==0:print('VIDEO_STALE_FRAME dropped=%d'%dropped[0],file=sys.stderr,flush=True)
            return Gst.PadProbeReturn.DROP
        scale.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER,stale)
def data_message(channel,text):
    try:
        arrived=time.monotonic_ns()
        input_bridge.receive(text)
        last_data_message[0]=time.monotonic();session_watchdog.alive()   # the data channel proves the client is alive
        if text.startswith('{') and '"capture"' in text[:80]:
            try:
                message=json.loads(text)
                if message.get('type')=='capture' and message.get('enabled') is True:
                    channel.emit('send-string','{"v":1,"type":"capture-state","enabled":true}')   # the client asks again until it sees this
            except ValueError:pass
        if latency_tracer:
            msg=json.loads(text)  # already validated and bounded by the bridge
            if msg.get('type')=='key' and msg.get('usage')==225 and msg.get('down')==1: latency_tracer.key(arrived)
    except Exception as error:
        input_bridge.close(); fail(error)
def check_input_lease():
    try:
        if input_bridge:
            input_bridge.expire()
            if input_bridge.lapsed():
                print('INPUT_LEASE lapsed',file=sys.stderr,flush=True)
                input_channel.emit('send-string','{"v":1,"type":"capture-state","enabled":false}')
    except Exception as error: fail(error);return False
    return True
def channel_open(channel):
    channel.emit('send-string','{"v":1,"type":"ready"}')
def capture_start_timeout(phase):
    code = {'capture-no-buffers':'capture-start-timeout',
            'encoding-no-buffers':'encoder-start-timeout',
            'encoded-no-offer':'negotiation-start-timeout'}[phase]
    if phase=='capture-no-buffers':
        from guest_state import read_capture_failure_code
        code=read_capture_failure_code()
    label = {'capture-no-buffers':'CAPTURE_START_TIMEOUT',
             'encoding-no-buffers':'ENCODER_START_TIMEOUT',
             'encoded-no-offer':'NEGOTIATION_START_TIMEOUT'}[phase]
    print(label+' '+phase,file=sys.stderr)
    try: send({'type':'error','code':code})
    except Exception: pass
    loop.quit()
def add_latency_trace():
    global latency_tracer
    from latency_trace import TransitionTracer,grid_mean
    gi.require_version('GstVideo','1.0'); from gi.repository import GstVideo
    latency_tracer=TransitionTracer(lambda record: print('LATENCY_TRACE '+json.dumps(record,separators=(',',':')),file=sys.stderr,flush=True))
    packed={'BGRx':(4,1),'BGRA':(4,1),'RGBx':(4,1),'RGBA':(4,1),'xRGB':(4,2),'ARGB':(4,2),'xBGR':(4,2),'ABGR':(4,2)}
    planar=('I420','NV12','YV12')
    def mean_luma(pad,buffer):
        # Small per-row extracts only; memory that cannot be read (e.g. VA surfaces) yields None.
        caps=pad.get_current_caps()
        if not caps:return None
        info=GstVideo.VideoInfo.new_from_caps(caps)
        if info is None:return None
        name=info.finfo.name
        if name not in packed and name not in planar:return None
        meta=GstVideo.buffer_get_video_meta(buffer)
        stride=meta.stride[0] if meta else info.stride[0]
        offset=meta.offset[0] if meta else info.offset[0]
        bpp,channel=packed.get(name,(1,0))
        def extract(start,size):
            try:return buffer.extract_dup(start,size)
            except Exception:return None
        return grid_mean(extract,info.width,info.height,stride,offset,bpp,channel)
    def pts(buffer):return None if buffer.pts==Gst.CLOCK_TIME_NONE else buffer.pts
    def never_drop(callback):
        # A probe returning anything but OK (including after an exception) can drop media.
        def probe(pad,info):
            try:callback(pad,info)
            except Exception:
                # Invalidate the trace without flooding logs or changing media flow.
                latency_tracer.error()
            return Gst.PadProbeReturn.OK
        return probe
    def on_capture(pad,probe):
        now=time.monotonic_ns();buffer=probe.get_buffer()
        if buffer:latency_tracer.capture(mean_luma(pad,buffer),now)
        return Gst.PadProbeReturn.OK
    def on_scale_in(pad,probe):
        now=time.monotonic_ns();buffer=probe.get_buffer()
        if buffer:latency_tracer.scaler_input(mean_luma(pad,buffer),now)
        return Gst.PadProbeReturn.OK
    def on_encoder_in(pad,probe):
        now=time.monotonic_ns();buffer=probe.get_buffer()
        if buffer:latency_tracer.encoder_input(mean_luma(pad,buffer),now)
        return Gst.PadProbeReturn.OK
    def on_encoded(pad,probe):
        now=time.monotonic_ns();buffer=probe.get_buffer()
        if buffer:latency_tracer.encoded(pts(buffer),now)
        return Gst.PadProbeReturn.OK
    def on_payloaded(pad,probe):
        now=time.monotonic_ns()
        if probe.type & Gst.PadProbeType.BUFFER:buffers=[probe.get_buffer()]
        else:
            group=probe.get_buffer_list()
            buffers=[group.get(i) for i in range(group.length())] if group else []
        for buffer in buffers:
            if buffer:latency_tracer.payloaded(pts(buffer),now)
        return Gst.PadProbeReturn.OK
    probes=[('capture','src',on_capture,Gst.PadProbeType.BUFFER),('scale','sink',on_scale_in,Gst.PadProbeType.BUFFER),('encoder','sink',on_encoder_in,Gst.PadProbeType.BUFFER),
            ('encoder','src',on_encoded,Gst.PadProbeType.BUFFER),('video_pay','src',on_payloaded,Gst.PadProbeType.BUFFER|Gst.PadProbeType.BUFFER_LIST)]
    for element,pad,callback,kind in probes:
        found=pipeline.get_by_name(element)
        if found:found.get_static_pad(pad).add_probe(kind,never_drop(callback))
    print('LATENCY_TRACE_ENABLED',file=sys.stderr)
def probe_audio_source(kind,sink):
    """Runs the sound source for a moment: False when it reports an error. gst-plugin-pipewire 1.6.9 answered 'target not found' for
    every sink of a machine without a sound card, and the whole session ended on it."""
    try:
        done=subprocess.run(audio_source.probe_arguments(kind,sink),capture_output=True,text=True,timeout=audio_source.PROBE_SECONDS)
        return audio_source.probe_started(done.returncode,done.stderr)
    except subprocess.TimeoutExpired as late:
        text=late.stderr.decode('utf-8','replace') if isinstance(late.stderr,bytes) else late.stderr
        return audio_source.probe_started(None,text)
    except OSError:return False
audio_kind='pipewire'
def start(node=None,fd=None,metadata=None):
    global capture_bootstrap, sink_watcher, captured_position, audio_kind
    captured_position=(metadata or {}).get('position')
    global pipeline,peer,input_bridge,input_channel,gsr,startup_watchdog,startup_watchdog_source,startup_watchdog_probes,limits
    video=('pipewiresrc name=capture do-timestamp='+str(options.video_timestamps == 'arrival').lower()+' keepalive-time=16 provide-clock=false'
           if options.desktop else 'videotestsrc is-live=true pattern=ball')
    if options.desktop and (options.request_capture_rate or options.rate_limit=='capture'):
        video+=' ! video/x-raw,max-framerate=(fraction)'+str(limits.max_fps)+'/1'
    audio_kind='pipewire'
    if options.desktop or options.system_audio:
        try:
            audio_kind=audio_source.choose(default_sink_name(),probe_audio_source)
            if audio_kind!='pipewire':print('AUDIO_SOURCE kind=%s (pipewiresrc could not start)'%audio_kind,file=sys.stderr,flush=True)
        except SinkLookupError:pass   # reported below, as before
    audio=audio_source.description(audio_kind,options.audio_timestamps == "arrival") if (options.desktop or options.system_audio) else 'audiotestsrc is-live=true wave=sine volume=0.02'
    source_width,source_height=1280,720
    if metadata and 'size' in metadata:
        source_width,source_height=map(int,metadata['size'])
    if options.bitrate_kbps is None:
        limits=dataclasses.replace(limits,bitrate_kbps=auto_bitrate_kbps(*limits.fit(source_width,source_height)))
    if options.desktop and options.capture_backend == 'gsr':
        from gsr_capture import GsrCapture
        gsr=GsrCapture(output='screen',fps=limits.max_fps,bitrate_kbps=limits.bitrate_kbps,width=limits.max_width,height=limits.max_height)
        gsr.start()
        GLib.timeout_add_seconds(40,lambda:(loop.quit(),False)[1])
        # Encoded data cannot use a leaky queue: dropping bytes or reference
        # frames here would corrupt decoding. Kernel pipe and queue are bounded.
        video_path='fdsrc name=encoded_source ! matroskademux ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0'
        print('ENCODER backend=gsr-gpu max_fps='+str(limits.max_fps),file=sys.stderr)
    else:
        backend=None
        # Sharp: a machine whose CPU default caps the picture (2 cores: 720p) streams up to 2560x1600 with its
        # hardware encoder instead; more latency there (VA held frames ~100 ms on a two-core Intel machine), much crisper text.
        if options.quality=='sharp' and options.encoder=='auto' and not _explicit_size:
            sharp=dataclasses.replace(limits,max_width=max(limits.max_width,SHARP_SIZE[0]),max_height=max(limits.max_height,SHARP_SIZE[1]))
            if options.bitrate_kbps is None:sharp=dataclasses.replace(sharp,bitrate_kbps=auto_bitrate_kbps(*sharp.fit(source_width,source_height)))
            try:backend=select_gstreamer_backend(Gst,'va-h264',sharp,source_width,source_height);limits=sharp
            except RuntimeError:print('QUALITY sharp unavailable: no hardware encoder',file=sys.stderr)
        if backend is None:backend=select_gstreamer_backend(Gst,options.encoder,limits,source_width,source_height)
        encoder_path=pipeline_for(backend,limits,source_width,source_height,options.rate_limit if options.desktop else 'videorate')
        video_path=video+' ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '+encoder_path
        print('RATE_LIMIT '+(options.rate_limit if options.desktop else 'videorate'),file=sys.stderr)
        print('ENCODER backend='+backend+' resolution='+str(limits.fit(source_width,source_height))+' max_fps='+str(limits.max_fps)+' bitrate_kbps='+str(limits.bitrate_kbps)+' workers='+str(limits.workers)+' physical_cores='+str(_cores),file=sys.stderr)
    audio_alignment=''
    if options.audio_timestamps == 'aligned':
        audio_alignment='audiorate name=audio_rate skip-to-first=true tolerance=10000000 ! '
    elif options.audio_timestamps == 'aligned-strict':
        audio_alignment='audiorate name=audio_rate skip-to-first=true tolerance=0 ! '
    audio_caps='audio/x-raw,rate=48000,channels=2'
    if options.audio_timestamps == 'sample-clock':
        audio_caps+=',format=S16LE,layout=interleaved'
        audio_alignment='identity datarate=192000 signal-handoffs=false ! '
    audio_gate='valve name=audio_gate drop=true drop-mode=forward-sticky-events ! ' if options.audio_timestamps in ('gated','aligned-strict','sample-clock') else ''
    pipeline=Gst.parse_launch('webrtcbin name=peer bundle-policy=max-bundle latency=20 '+video_path+' ! h264parse ! rtph264pay name=video_pay mtu='+str(options.rtp_mtu)+' config-interval=-1 pt=96 ! application/x-rtp,media=video,encoding-name=H264,payload=96 ! peer. '+audio+' ! queue max-size-time=40000000 max-size-buffers=0 max-size-bytes=0 leaky=downstream ! audioconvert ! audioresample ! '+audio_caps+' ! '+audio_alignment+'opusenc frame-size=10 bitrate=96000'+opus_fec+' ! rtpopuspay name=audio_pay pt=111 ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111 ! '+audio_gate+'peer.')
    if gsr:
        pipeline.get_by_name('encoded_source').set_property('fd',gsr.stdout_fd)
    elif options.desktop:
        import subprocess
        capture=pipeline.get_by_name('capture'); capture.set_property('fd',fd); capture.set_property('path',str(node))
    if options.desktop or options.system_audio:
        import subprocess
        name=default_sink_name()
        source=pipeline.get_by_name('audio'); audio_source.apply(audio_kind,source,name)
        # Fixed node name lets diagnostics find this capture stream's links; it carries no sink identity.
        if audio_kind=='pipewire':source.set_property('stream-properties',Gst.Structure.new_from_string('props,stream.capture.sink=(boolean)true,node.name=(string)omarchy-remote-audio-capture'))
        def retarget(new_name):
            # Never capture the microphone's own sink: its monitor is the controlling device's voice (feedback).
            from virtual_mic import SINK as mic_sink
            if new_name==mic_sink:
                print('AUDIO_SINK_SKIPPED mic-sink',file=sys.stderr,flush=True);return
            # Called on the watcher thread; restart only the audio source on the GLib loop.
            def apply():
                if pipeline is None:return False
                element=pipeline.get_by_name('audio')
                element.set_state(Gst.State.NULL);audio_source.apply(audio_kind,element,new_name)
                print('AUDIO_SINK_CHANGED retargeted=%s'%bool(element.sync_state_with_parent()),file=sys.stderr,flush=True)
                return False
            GLib.idle_add(apply)
        sink_watcher=DefaultSinkWatcher(retarget,name);sink_watcher.start()
        if virtual_display and options.display=='remote':
            # Nobody sits at the machine: its speakers stay silent, the sound plays on the controlling device (local_mute.py).
            from local_mute import LocalMute
            print('LOCAL_MUTE '+str(LocalMute().apply(name)),file=sys.stderr,flush=True)
    if options.audio_diagnostics:
        def observe_stage(label):
            video_stages[label]={'buffers':0,'first_pts':None,'last_pts':None,'caps':None}
            def observe(pad,info):
                buffer=info.get_buffer()
                if buffer:
                    stage=video_stages[label];stage['buffers']+=1
                    if stage['caps'] is None:
                        caps=pad.get_current_caps()
                        if caps and caps.get_size():
                            structure=caps.get_structure(0)
                            stage['caps']={key:structure.get_string(key) if key=='format' else structure.get_int(key)[1] for key in ('format','width','height') if structure.has_field(key)}
                            if structure.has_field('framerate'):
                                valid,numerator,denominator=structure.get_fraction('framerate')
                                if valid:stage['caps']['framerate']=[numerator,denominator]
                    if buffer.pts != Gst.CLOCK_TIME_NONE:
                        if stage['first_pts'] is None:stage['first_pts']=buffer.pts
                        stage['last_pts']=buffer.pts
                return Gst.PadProbeReturn.OK
            return observe
        for label,element_name,pad_name in [('capture','capture','src'),('encoder_in','encoder','sink'),('encoder_out','encoder','src')]:
            element=pipeline.get_by_name(element_name)
            if element:element.get_static_pad(pad_name).add_probe(Gst.PadProbeType.BUFFER,observe_stage(label))
        def observe_audio(pad,info):
            buffer=info.get_buffer()
            if buffer and buffer.get_size()>=12:audio_timing.observe(buffer.extract_dup(0,12))
            return Gst.PadProbeReturn.OK
        timing_element=pipeline.get_by_name('audio_gate') or pipeline.get_by_name('audio_pay')
        timing_element.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,observe_audio)
    if options.desktop and not gsr and options.rate_limit=='leading':
        limiter=limits.leading_limiter()
        def limit(_pad,info):
            return Gst.PadProbeReturn.OK if limiter.allow(time.monotonic_ns()) else Gst.PadProbeReturn.DROP
        pipeline.get_by_name('capture').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,limit)
    if options.desktop and not gsr:
        # Frame rate in and out of the encoder, sampled every 5 s (no content): VIDEO_STATS in/out fps and the capture size,
        # logged once a minute and for every sample in which the encoder fell behind (media_config.video_stats_due).
        counts={'in':0,'out':0}; last={'t':time.monotonic(),'in':0,'out':0,'report':float('-inf')}
        def count(key):
            def probe(_pad,_info):
                counts[key]+=1
                return Gst.PadProbeReturn.OK
            return probe
        capture_src=pipeline.get_by_name('capture').get_static_pad('src'); encoder_src=pipeline.get_by_name('encoder').get_static_pad('src')
        capture_src.add_probe(Gst.PadProbeType.BUFFER,count('in')); encoder_src.add_probe(Gst.PadProbeType.BUFFER,count('out'))
        def report_video_stats():
            now=time.monotonic(); span=max(0.001,now-last['t'])
            caps=capture_src.get_current_caps(); size=''
            if caps and caps.get_size():
                structure=caps.get_structure(0); size=str(structure.get_value('width'))+'x'+str(structure.get_value('height'))
            in_fps=(counts['in']-last['in'])/span; out_fps=(counts['out']-last['out'])/span
            if video_stats_due(now,last['report'],in_fps,out_fps,limits.max_fps):
                last['report']=now
                print('VIDEO_STATS in_fps=%.1f out_fps=%.1f capture=%s'%(in_fps,out_fps,size or '?'),file=sys.stderr)
            last.update(t=now,**{'in':counts['in'],'out':counts['out']})
            return True
        GLib.timeout_add_seconds(5,report_video_stats)
    if options.latency_trace and options.desktop and not gsr: add_latency_trace()
    global av_clock_flags
    peer=pipeline.get_by_name('peer')
    av_clock_flags=configure_sync(peer)
    peer.connect('on-negotiation-needed',negotiate); peer.connect('on-ice-candidate',ice)
    if options.desktop and options.capture_backend == 'portal':
        watchdog=StartupWatchdog(8, capture_start_timeout)
        startup_watchdog=watchdog
        capture_bootstrap=CaptureBootstrap(metadata,lambda: watchdog.capture_buffers==0)
        capture_pad=pipeline.get_by_name('capture').get_static_pad('src')
        encoder_pad=pipeline.get_by_name('encoder').get_static_pad('src')
        def observe_capture(_pad, info):
            if info.get_buffer(): watchdog.observe_capture()
            return Gst.PadProbeReturn.OK
        def observe_encoded(_pad, info):
            if info.get_buffer(): watchdog.observe_encoded()
            return Gst.PadProbeReturn.OK
        startup_watchdog_probes=[
            (capture_pad,capture_pad.add_probe(Gst.PadProbeType.BUFFER, observe_capture)),
            (encoder_pad,encoder_pad.add_probe(Gst.PadProbeType.BUFFER, observe_encoded)),
        ]
    # parse_launch has already requested both sink pads/transceivers. Enable
    # library-managed retransmission before creating the offer; the default
    # is false and otherwise lost packets cannot be recovered through RTX.
    # Video only: advertising Opus RTX fails with the Apple libwebrtc receiver.
    if not options.no_mic:
        # The audio transceiver also receives: a device that attaches its microphone answers sendrecv, and the
        # decoded audio goes to the virtual microphone (incoming_pad). Devices without a microphone answer recvonly.
        audio_transceiver=peer.emit('get-transceiver',1)
        if audio_transceiver is not None:
            audio_transceiver.set_property('direction',GstWebRTC.WebRTCRTPTransceiverDirection.SENDRECV)
        peer.connect('pad-added',incoming_pad)
    for index in range(1):
        transceiver=peer.emit('get-transceiver',index)
        if transceiver is None or transceiver.find_property('do-nack') is None:
            raise RuntimeError('GStreamer retransmission capability unavailable')
        transceiver.set_property('do-nack',True)
    if options.audio_timestamps in ('gated','aligned-strict','sample-clock'):
        def open_audio_when_connected(element,prop):
            if element.get_property('connection-state') == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
                pipeline.get_by_name('audio_gate').set_property('drop',False)
        peer.connect('notify::connection-state',open_audio_when_connected)
    bus=pipeline.get_bus(); bus.add_signal_watch(); bus.connect('message',message)
    pipeline.set_state(Gst.State.READY)
    if options.desktop:
        input_bridge=InputBridge()
        input_channel=peer.emit('create-data-channel','input',None)
        input_channel.connect('on-message-string',data_message)
        input_channel.connect('on-open',channel_open)
        GLib.timeout_add(100,check_input_lease)
        start_clipboard()
        if not gsr:
            follow_source_size()
            start_display()
    pipeline.set_state(Gst.State.PLAYING)
    if startup_watchdog:
        def watchdog_tick():
            global startup_watchdog_source
            if watchdog.bootstrap_due():capture_bootstrap.start()
            keep_running=watchdog.check()
            if not keep_running: startup_watchdog_source=None
            return keep_running
        startup_watchdog_source=GLib.timeout_add(100, watchdog_tick)
try:
    session_watchdog_source=GLib.timeout_add(250,check_session)
    stall=[time.monotonic()]
    def watch_loop():
        now=time.monotonic()
        if now-stall[0]>2.0:print('LOOP_STALL %.1fs'%(now-stall[0]),file=sys.stderr,flush=True)
        stall[0]=now;return True
    GLib.timeout_add(500,watch_loop)
    threading.Thread(target=reader,daemon=True).start()
    if options.desktop and options.capture_backend == 'portal' and options.display in ('remote','extend'):
        from virtual_display import VirtualDisplay
        from guest_state import hyprland_env,bounded_command
        _vd_env=hyprland_env()
        def _hyprctl(arguments):return bounded_command(['hyprctl']+arguments,_vd_env)
        def _evaluate(lua):
            try:return 'error' not in _hyprctl(['eval',lua]).lower()
            except Exception:return False
        virtual_display=VirtualDisplay(_hyprctl,_evaluate)
        try:
            print('VIRTUAL_DISPLAY '+virtual_display.start(extend=options.display=='extend',side=options.side),file=sys.stderr,flush=True)
        except Exception as error:
            # Fall back to mirroring the physical screen; start() already restored it.
            # The reasons are fixed phrases of virtual_display.py ('no physical output', 'output not disabled' ...).
            print('VIRTUAL_DISPLAY failed '+type(error).__name__+' '+' '.join(str(error).split())[:80],file=sys.stderr,flush=True)
            virtual_display=None
    if options.desktop and options.capture_backend == 'portal':
        # A silent portal (its backend leaks and finally spins) is restarted once and asked again; see portal_recovery.py.
        from portal_recovery import PortalGuard,restart_portal_services
        def begin_portal(arrived):
            global portal
            portal=ScreenCast(auto_consent=True,persist=virtual_display is None,output=virtual_display.name if virtual_display else None); portal.start(arrived,fail)
        def restart_portal(done):
            if portal:
                # Not close(): its Session.Close call would wait for the silent portal (25 s). The restart ends its sessions anyway.
                try: portal._disarm(); portal._close_requests()
                except Exception: pass
            def work():
                restart_portal_services(); GLib.idle_add(lambda:done() and False)
            threading.Thread(target=work,daemon=True).start()
        PortalGuard(begin_portal,start,lambda:fail(RuntimeError('Portal not answering')),restart_portal,
                    lambda seconds,callback:GLib.timeout_add(int(seconds*1000),callback),log=lambda text:print(text,file=sys.stderr,flush=True)).run()
    else: start()
    loop.run()
finally:
    GLib.source_remove(session_watchdog_source) if not session_watchdog.reason else None
    stop_startup_watchdog()
    if input_bridge: input_bridge.close()
    if capture_bootstrap: capture_bootstrap.close()
    if sink_watcher: sink_watcher.stop()
    if clipboard_sync: clipboard_sync.close()
    if gsr: gsr.close()
    if pipeline:
        rate=pipeline.get_by_name('audio_rate')
        if rate:
            # Samples audiorate inserted/dropped to keep timestamps continuous; nonzero values mean
            # the source timing was irregular (possible audible artifacts with real content).
            print('AUDIO_RATE in=%d out=%d add=%d drop=%d'%tuple(rate.get_property(k) for k in ('in','out','add','drop')),file=sys.stderr)
        pipeline.set_state(Gst.State.NULL)
    if virtual_mic:virtual_mic.stop()
    if display_mode:
        from local_mute import LocalMute
        LocalMute().restore()
        restored=display_mode.restore()
        print('DISPLAY_MODE restored='+str(restored),file=sys.stderr,flush=True)
    # The virtual display stays up: the service restores the screens a few seconds later unless the next
    # remote session reuses it (webrtc.mjs scheduleRestore).
    if latency_tracer: print('LATENCY_TRACE_SUMMARY '+json.dumps(latency_tracer.close()),file=sys.stderr)
    if options.audio_diagnostics:
        print('RTP_AUDIO '+json.dumps(audio_timing.summary()),file=sys.stderr)
        print('VIDEO_STAGES '+json.dumps(video_stages),file=sys.stderr)
    if portal: portal.close()
    os.close(controller_lock)
