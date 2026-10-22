"""Validated data-channel input to the private Wayland helper. No shell."""
import json, os, subprocess, sys, time

# USB HID keyboard usages to Linux evdev codes (physical keys, not text).
HID = dict(zip(range(4,30),[30,48,46,32,18,33,34,35,23,36,37,38,50,49,24,25,16,19,31,20,22,47,17,45,21,44]))
HID.update(dict(zip(range(30,40),[2,3,4,5,6,7,8,9,10,11])))
HID.update({40:28,41:1,42:14,43:15,44:57,45:12,46:13,47:26,48:27,49:43,50:86,51:39,52:40,53:41,54:51,55:52,56:53,57:58,70:99,71:70,72:119,73:110,74:102,75:104,76:111,77:107,78:109,79:106,80:105,81:108,82:103,83:69,84:98,85:55,86:74,87:78,88:96,89:79,90:80,91:81,92:75,93:76,94:77,95:71,96:72,97:73,98:82,99:83,100:86,101:127,103:117})
HID.update(dict(zip(range(58,70),[59,60,61,62,63,64,65,66,67,68,87,88])))
HID.update(dict(zip(range(104,116),range(183,195))))
HID.update(dict(zip(range(224,232),[29,42,56,125,97,54,100,126])))

class InputBridge:
    voxtype=None       # VoxtypeHotkey (voxtype_hotkey.py); None in tests and where the module is not installed
    # The streamed output within the layout (virtual_display.pointer_region): the virtual pointer's absolute
    # coordinates span all outputs, the client's span the picture.
    region=(0.0,0.0,1.0,1.0)
    REPORT_SECONDS=60  # INPUT_LEASE stats at most once a minute (and once at the end), counts only
    stats=None; reported=0.0
    def __init__(self):
        env=os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR','/run/user/%d'%os.getuid())
        for line in subprocess.check_output(['systemctl','--user','show-environment'],text=True).splitlines():
            name,sep,value=line.partition('=')
            if name in ('WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE') and sep: env[name]=value
        def option(name):
            data=json.loads(subprocess.check_output(['hyprctl','getoption','input:'+name,'-j'],env=env,text=True))
            return data.get('str','')
        self.env=env
        self.process=subprocess.Popen([os.path.expanduser('~/.local/bin/omarchy-input-helper'),option('kb_layout') or 'us',option('kb_variant'),option('kb_options')],stdin=subprocess.PIPE,env=env)
        self.sequence=-1;self.armed=False;self.notice=False;self.last=0;self.window=time.monotonic();self.count=0
        try:
            from voxtype_hotkey import VoxtypeHotkey
            self.voxtype=VoxtypeHotkey()
        except ImportError:pass
    def write(self,line):
        self.process.stdin.write((line+'\n').encode('ascii'));self.process.stdin.flush()
    def expire(self):
        if self.armed and time.monotonic()-self.last>1:
            self.write('release');self.armed=False;self.notice=True;self.tally('lease_lapsed');return True
        return False
    def tally(self,name):
        if self.stats is None: self.stats={}
        self.stats[name]=self.stats.get(name,0)+1
    def report(self,now,final=False):
        # Counts since the session started (heartbeats by typing mode, lease lapses); never key names.
        if not self.stats or (not final and now-self.reported<self.REPORT_SECONDS): return
        self.reported=now
        print('INPUT_LEASE stats '+' '.join('%s=%d'%(k,v) for k,v in sorted(self.stats.items())),file=sys.stderr,flush=True)
    def lapsed(self):
        """True once after the lease ran out, whichever call noticed it: receive() calls expire() too, and a sender short on CPU
        saw the gap there first, so the 100 ms check never told the client (enabled=false) and its input was ignored for good."""
        notice,self.notice=self.notice,False
        return notice
    def receive(self,text):
        if len(text)>4096: raise ValueError('Input message too large')
        msg=json.loads(text)
        if not isinstance(msg,dict) or msg.get('v')!=1: raise ValueError('Invalid input envelope')
        seq=msg.get('seq');kind=msg.get('type')
        if type(seq)!=int or seq<0: raise ValueError('Invalid sequence')
        if seq<=self.sequence: return
        self.sequence=seq
        now=time.monotonic()
        self.report(now)
        if now-self.window>=1:self.window=now;self.count=0
        self.count+=1
        if self.count>2000: raise ValueError('Input rate exceeded')
        def integer(name,low,high):
            value=msg.get(name)
            if type(value)!=int or not low<=value<=high:raise ValueError('Invalid '+name)
            return value
        def number(name):
            import math
            value=msg.get(name)
            if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>8192:raise ValueError('Invalid '+name)
            return value
        if kind=='capture':
            enabled=msg.get('enabled')
            if type(enabled)!=bool:raise ValueError('Invalid capture')
            self.write('capture %d'%enabled);self.armed=enabled;self.last=now;return
        if kind=='release':self.write('release');self.armed=False;return
        self.expire()
        # After a lapse nothing reaches the helper until the client enables capture again: the lapse already
        # wrote 'release', which lets go of every held key and button, and the helper exits on any input command while it is
        # not armed, which would end the session's input for good. A late key-up therefore has nothing left to release.
        if not self.armed:return
        if kind=='heartbeat':self.write('heartbeat');self.last=now;self.tally('hb_bundled' if msg.get('bundled') is True else 'hb_plain');return
        if kind=='key':
            usage=integer('usage',0,255);down=integer('down',0,1)
            self.last=now   # a key press is a sign of life too: typing keeps the lease
            if usage not in HID:raise ValueError('Unsupported HID usage')
            self.write('key %d %d'%(HID[usage],down))
            if self.voxtype:self.voxtype.key(HID[usage],down)
        elif kind=='button':self.write('button %d %d'%(integer('button',272,274),integer('down',0,1)))
        elif kind=='absolute':
            x,y,w,h=self.region
            fx,fy=integer('x',0,65535)/65535,integer('y',0,65535)/65535
            self.write('absolute %d %d'%(min(65535,max(0,round((x+fx*w)*65535))),min(65535,max(0,round((y+fy*h)*65535)))))
        elif kind in ('relative','scroll'):self.write('%s %f %f'%(kind,number('x'),number('y')))
        elif kind=='text':self.type_text(msg.get('text'))
        else:raise ValueError('Unsupported input')
    MAX_TEXT=256
    def type_text(self,text):
        """Characters from an on-screen keyboard, independent of the guest layout: wtype sends them
        through its own virtual keyboard. Bounded, printable text only, no shell; never logged."""
        if not isinstance(text,str) or not 1<=len(text)<=self.MAX_TEXT: raise ValueError('Invalid text')
        if any(ord(c)<32 and c not in '\t\n' or 0x7f<=ord(c)<0xa0 for c in text): raise ValueError('Invalid text')
        try: subprocess.run(['wtype','--',text],env=getattr(self,'env',None),timeout=3,check=False,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except (OSError,subprocess.TimeoutExpired): pass
    def close(self):
        self.report(time.monotonic(),final=True)
        try:self.write('release');self.process.stdin.close()
        except (BrokenPipeError,OSError):pass
        try:self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=2)
