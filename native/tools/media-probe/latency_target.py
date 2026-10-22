#!/usr/bin/env python3
"""Bounded fullscreen target for the native --latency-test.
Left Shift press toggles black/white. Reads no desktop content, injects nothing,
logs no key names, exits after --seconds or on Escape."""
import argparse,json,os,select,subprocess,time
from guest_state import MESSAGES,focus_state
import gi
gi.require_version('Gtk','3.0')
parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=int,default=180)
parser.add_argument('--no-keepalive',action='store_true',help='static target; some portals then deliver no frames')
args=parser.parse_args()
if not 10<=args.seconds<=600:parser.error('--seconds must be between 10 and 600')
def command_output(command):
 p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
 fd=p.stdout.fileno();os.set_blocking(fd,False);data=bytearray();deadline=time.monotonic()+2
 try:
  while True:
   remaining=deadline-time.monotonic()
   if remaining<=0:raise RuntimeError('bounded command timed out')
   ready,_,_=select.select([fd],[],[],remaining)
   if not ready:raise RuntimeError('bounded command timed out')
   chunk=os.read(fd,65536)
   if not chunk:break
   data.extend(chunk)
   if len(data)>65536:raise RuntimeError('bounded command output exceeded 64 KiB')
  if p.wait(timeout=max(0,deadline-time.monotonic()))!=0:raise RuntimeError('bounded command failed')
 except (RuntimeError,subprocess.TimeoutExpired):
  p.kill();p.wait();raise
 return bytes(data).decode(errors='strict')
for line in command_output(['systemctl','--user','show-environment']).splitlines():
 k,sep,v=line.partition('=')
 if sep and k in ('WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE'):os.environ[k]=v
os.environ['GDK_BACKEND']='wayland'
from gi.repository import Gtk,Gdk,GLib
window=Gtk.Window(title='Omarchy Remote Latency Target');area=Gtk.DrawingArea();window.add(area)
bright=False;toggles=0;ignored=0;tick=0;start=time.monotonic();pending_key=None
KEEPALIVE=16 # px; bottom-right corner, outside the client's central sampling area
def draw(widget,cr):
 global pending_key
 if pending_key is not None:
  # CLOCK_MONOTONIC, comparable with the sender's LATENCY_TRACE on this machine.
  print('TOGGLE '+json.dumps({'n':toggles,'level':'bright' if bright else 'dark','key_ns':pending_key,'draw_ns':time.monotonic_ns()}),flush=True)
  pending_key=None
 value=1.0 if bright else 0.0
 cr.set_source_rgb(value,value,value);cr.paint()
 if not args.no_keepalive:
  # Damage-only portals deliver no frames for a static window; keep a small, same-level patch changing.
  shade=value+(-.08 if bright else .08)*(tick%2)
  cr.set_source_rgb(shade,shade,shade);cr.rectangle(widget.get_allocated_width()-KEEPALIVE,widget.get_allocated_height()-KEEPALIVE,KEEPALIVE,KEEPALIVE);cr.fill()
 return False
def keepalive():
 global tick
 tick+=1;w=area.get_allocated_width();h=area.get_allocated_height()
 area.queue_draw_area(w-KEEPALIVE,h-KEEPALIVE,KEEPALIVE,KEEPALIVE);return True
def key(widget,event):
 global bright,toggles,ignored,pending_key
 now=time.monotonic_ns();name=Gdk.keyval_name(event.keyval)
 if name=='Shift_L':bright=not bright;toggles+=1;pending_key=now;area.queue_draw()
 elif name=='Escape':window.destroy()
 else:ignored+=1
 return True
last_focus=None
def focus():
 global last_focus
 try:state=focus_state(command_output(['hyprctl','activewindow','-j']),command_output(['hyprctl','-j','monitors']),os.getpid())
 except (subprocess.SubprocessError,OSError,RuntimeError):state='unknown'
 if state!=last_focus:
  last_focus=state
  print(MESSAGES[state],flush=True)
 return True
area.connect('draw',draw);window.connect('key-press-event',key);window.connect('destroy',lambda *_:Gtk.main_quit())
window.fullscreen();window.show_all();window.present()
GLib.timeout_add(1000,focus)
if not args.no_keepalive:GLib.timeout_add(33,keepalive)
GLib.timeout_add_seconds(args.seconds,lambda:window.destroy() or False)
Gtk.main()
print(json.dumps({'toggles':toggles,'ignored_keys':ignored,'seconds':round(time.monotonic()-start,3)}))
