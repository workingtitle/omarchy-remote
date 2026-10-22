#!/usr/bin/env python3
"""Bounded A/V sync target. Once per second the fullscreen window flashes white
and, only with --audio, the default output plays a short 1 kHz tick at the same pipeline time.
The flash follows the audio pipeline's position, so both share one clock; the guest-side offset
is bounded by one display frame plus sink latency and stays constant, so drift over time is
still visible. Announce before using --audio. Never opens a microphone; logs only counters.
Usage: av_sync_target.py [--audio] [--volume 0.2] [--seconds 720]"""
import argparse,json,os,subprocess,time
import gi
gi.require_version('Gtk','3.0');gi.require_version('Gst','1.0')
parser=argparse.ArgumentParser()
parser.add_argument('--audio',action='store_true',help='play ticks (announce first)')
parser.add_argument('--volume',type=float,default=0.2)
parser.add_argument('--seconds',type=int,default=720)
args=parser.parse_args()
if not 10<=args.seconds<=900:parser.error('--seconds must be between 10 and 900')
if not 0.01<=args.volume<=0.5:parser.error('--volume must be between 0.01 and 0.5')
for line in subprocess.check_output(['systemctl','--user','show-environment'],text=True,timeout=3).splitlines():
    k,sep,v=line.partition('=')
    if sep and k in ('WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE','XDG_RUNTIME_DIR','PULSE_SERVER'):os.environ[k]=v
os.environ['GDK_BACKEND']='wayland'
from gi.repository import Gtk,Gst,GLib
Gst.init(None)

SECOND=1_000_000_000;FLASH=100_000_000
# ticks: one 10-period 1 kHz burst per tick-interval, aligned to running time 0, 1 s, 2 s ...
sink='autoaudiosink' if args.audio else 'fakesink sync=true'
volume=args.volume if args.audio else 0.0
pipeline=Gst.parse_launch('audiotestsrc is-live=true wave=ticks tick-interval=%d freq=1000 sine-periods-per-tick=10 volume=%f ! '
                          'audioconvert ! audioresample ! %s'%(SECOND,volume,sink))
window=Gtk.Window(title='Omarchy Remote AV Sync Target');area=Gtk.DrawingArea();window.add(area)
started=time.monotonic();flashes=0;lit=False;errors=[]

def position():
    ok,value=pipeline.query_position(Gst.Format.TIME)
    return value if ok and value>=0 else None
def draw(widget,cr):
    cr.set_source_rgb(*((1,1,1) if lit else (0,0,0)));cr.paint()
    cr.set_source_rgb(.5,.5,.5);cr.select_font_face('monospace');cr.set_font_size(28)
    elapsed=int(time.monotonic()-started);cr.move_to(30,50)
    cr.show_text('%02d:%02d  %s'%(elapsed//60,elapsed%60,'Ton an' if args.audio else 'stumm'))
    return False
def frame(*_):
    global lit,flashes
    pos=position()
    now_lit=pos is not None and pos%SECOND<FLASH
    if now_lit and not lit:flashes+=1
    if now_lit!=lit:lit=now_lit;area.queue_draw()
    if time.monotonic()-started>=args.seconds:window.destroy();return False
    return True
def message(_bus,msg):
    if msg.type==Gst.MessageType.ERROR:errors.append(str(msg.parse_error()[0])[:200]);window.destroy()

bus=pipeline.get_bus();bus.add_signal_watch();bus.connect('message',message)
def key(_w,event):
    if event.keyval==0xff1b:window.destroy()  # Escape closes early
    return True
area.connect('draw',draw);window.connect('key-press-event',key)
window.connect('destroy',lambda *_:Gtk.main_quit())
try:
    gi.require_version('GLibUnix','2.0');from gi.repository import GLibUnix;signal_add=GLibUnix.signal_add
except (ValueError,ImportError):signal_add=GLib.unix_signal_add  # older PyGObject
for number in (15,2):signal_add(GLib.PRIORITY_DEFAULT,number,lambda:window.destroy() or False)
window.fullscreen();window.show_all()
try:
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add(4,frame)  # poll faster than a display frame; redraw only on edges
    Gtk.main()
finally:
    pipeline.set_state(Gst.State.NULL)
print(json.dumps({'audio':args.audio,'flashes':flashes,'seconds':round(time.monotonic()-started,1),'errors':errors}))
raise SystemExit(1 if errors else 0)
