#!/usr/bin/env python3
"""Bounded keyboard acceptance window. Prompts one item at a time and stores
only per-item results (see keyboard_items.py); never typed text or a raw key stream.
Usage: keyboard_check.py --layout us|de|de-mac [--seconds N]. Set the guest layout BEFORE connecting:
the input helper reads it when the sender starts."""
import argparse,json,os,pathlib,subprocess,time
import gi
gi.require_version('Gtk','3.0')
from keyboard_items import COMBO,HOLD,OBSERVE,TEXT,MODIFIERS,LAYOUTS,Session,items,profile_for

parser=argparse.ArgumentParser();parser.add_argument('--layout',choices=tuple(LAYOUTS),required=True)
parser.add_argument('--seconds',type=int,default=1800)
parser.add_argument('--only',default='',help='comma-separated item ids to repeat, e.g. demac_21,f1')
args=parser.parse_args()
if not 60<=args.seconds<=3600:parser.error('--seconds must be between 60 and 3600')
for line in subprocess.check_output(['systemctl','--user','show-environment'],text=True,timeout=3).splitlines():
    k,sep,v=line.partition('=')
    if sep and k in ('WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE'):os.environ[k]=v
os.environ['GDK_BACKEND']='wayland'
from gi.repository import Gtk,Gdk,GLib

def option(name):
    try:return json.loads(subprocess.check_output(['hyprctl','getoption','input:'+name,'-j'],text=True,timeout=2)).get('str','')
    except (subprocess.SubprocessError,OSError,ValueError):return None
guest={'kb_layout':option('kb_layout'),'kb_variant':option('kb_variant'),'kb_options':option('kb_options')}
guest_profile=profile_for(guest['kb_layout'],guest['kb_variant'])
selected=items(args.layout)
if args.only:
    wanted=args.only.split(',');known={i.id for i in selected}
    if not set(wanted)<=known:parser.error('unknown item ids: '+','.join(sorted(set(wanted)-known)))
    selected=[i for i in selected if i.id in wanted]
session=Session(selected);shown=None;started=time.monotonic();held=set()
MASKS=[(Gdk.ModifierType.SHIFT_MASK,'shift'),(Gdk.ModifierType.CONTROL_MASK,'control'),(Gdk.ModifierType.MOD1_MASK,'alt'),
       (Gdk.ModifierType.SUPER_MASK,'super'),(Gdk.ModifierType.MOD4_MASK,'super')]

window=Gtk.Window(title='Omarchy Remote Keyboard Check');window.set_default_size(960,540)
box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=14);box.set_border_width(24);window.add(box)
header=Gtk.Label(xalign=0);box.pack_start(header,False,False,0)
prompt=Gtk.Label(xalign=0);box.pack_start(prompt,False,False,0)
hint=Gtk.Label(xalign=0);hint.set_line_wrap(True);box.pack_start(hint,False,False,0)
entry=Gtk.Entry();entry.set_placeholder_text('Hier tippen — Text wird nicht gespeichert');box.pack_start(entry,False,False,0)
status=Gtk.Label(xalign=0);box.pack_start(status,False,False,0)
answers=Gtk.Box(spacing=8);box.pack_start(answers,False,False,0)
controls=Gtk.Box(spacing=8);box.pack_end(controls,False,False,0)
buttons={}
for key,label in [('guest','Gast reagiert'),('macos','macOS reagiert'),('client','Client gibt lokal frei'),('none','Nichts passiert'),
                  ('still-held','Taste war noch gedrückt'),('released-first','Hatte schon losgelassen')]:
    b=Gtk.Button(label=label);b.connect('clicked',lambda _b,k=key:(session.answer(k),refresh()));answers.pack_start(b,False,False,0);buttons[key]=b
for label,action in [('Überspringen',session.skip),('Geht nicht',session.fail)]:
    b=Gtk.Button(label=label);b.connect('clicked',lambda _b,a=action:(a(),refresh()));controls.pack_start(b,False,False,0)

def refresh():
    item=session.current
    header.set_markup('<b>Tastatur-Check %s</b>   Gast-Layout: %s%s %s   Punkt %d/%d'%(
        args.layout.upper(),guest['kb_layout'] or '?','(%s)'%guest['kb_variant'] if guest['kb_variant'] else '',
        ('' if guest_profile==args.layout else '⚠ passt nicht zum Test (Gast: %s)'%(guest_profile or 'unbekannt')),
        min(session.index+1,len(session.items)),len(session.items)))
    if item is None:
        prompt.set_markup('<big><b>Fertig.</b></big> Fenster schließt sich in 5 s.');hint.set_text('');entry.hide();answers.hide()
        GLib.timeout_add_seconds(5,lambda:window.destroy() or False);return
    prompt.set_markup('<big><b>%s</b></big>'%GLib.markup_escape_text(item.prompt))
    hint.set_text(item.hint+('  (optional)' if item.optional else ''))
    entry.set_visible(item.kind==TEXT)
    global shown
    if shown!=session.index:
        # Only on a new item: grabbing focus mid-composition resets the IM and drops dead keys.
        shown=session.index
        if item.kind==TEXT:entry.grab_focus()
        else:window.set_focus(None)
    answers.set_visible(item.kind==OBSERVE or session.awaiting_hold_answer)
    for key,b in buttons.items():
        b.set_visible(key in ('still-held','released-first') if item.kind==HOLD else key in ('guest','macos','client','none'))
    last=session.results[-1] if session.results else None
    status.set_text(('Last item: %s → %s   '%(last['id'],last['result']) if last else '')+
                    ('Falsche Versuche: %d   '%session.wrong if session.wrong else '')+
                    ('Gedrückt: '+' '.join(sorted(held)) if held else '')+
                    ('   Losgelassen nach %d ms — hieltest du die Taste noch?'%session.held_ms if session.awaiting_hold_answer else ''))

def modifiers(state):return {name for mask,name in MASKS if state & mask}
def press(_w,event):
    name=Gdk.keyval_name(event.keyval) or 'unknown'
    if name in MODIFIERS or (session.current and session.current.kind==HOLD):held.add(name if name in MODIFIERS or name in session.vocabulary else 'other')
    consumed=session.key_press(name,modifiers(event.get_state()),time.monotonic_ns(),event.hardware_keycode)
    refresh();return consumed
def release(_w,event):
    name=Gdk.keyval_name(event.keyval) or 'unknown'
    held.discard(name if name in MODIFIERS or name in session.vocabulary else 'other')
    session.key_release(name,time.monotonic_ns(),event.hardware_keycode);refresh();return False
def changed(widget):
    if session.text(widget.get_text()):GLib.idle_add(lambda:(widget.set_text(''),refresh(),False)[2])
def tick():
    session.tick(time.monotonic_ns())
    if time.monotonic()-started>args.seconds:window.destroy();return False
    refresh();return True

window.connect('key-press-event',press);window.connect('key-release-event',release);entry.connect('changed',changed)
window.connect('destroy',lambda *_:Gtk.main_quit())
# SIGTERM/SIGINT end the session normally, so partial results are still written.
try:
    gi.require_version('GLibUnix','2.0');from gi.repository import GLibUnix;signal_add=GLibUnix.signal_add
except (ValueError,ImportError):signal_add=GLib.unix_signal_add  # older PyGObject
for number in (15,2):signal_add(GLib.PRIORITY_DEFAULT,number,lambda:window.destroy() or False)
window.show_all();refresh();GLib.timeout_add(250,tick)
Gtk.main()
report={'layout':args.layout,'only':args.only or None,'guest':guest,'guest_profile':guest_profile,'seconds':round(time.monotonic()-started),**session.summary()}
path=pathlib.Path.home()/'.local/state/omarchy-remote'/('keyboard-check-%s-%d.json'%(args.layout,int(time.time())))
path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=1));path.chmod(0o600)
print(json.dumps({'report':str(path),'layout':args.layout,'guest_layout':guest['kb_layout'],**{k:report[k] for k in ('counts','unanswered')}}))
