"""One bounded screencopy request to bootstrap an idle portal monitor.

Discard pixels; the portal remains the only video source. Never wake/unlock,
retry, capture multiple outputs, or run this on the media/input event loop.
"""
import json
import os
import subprocess
import threading
from guest_state import _bounded_output, session_locked


def selected_output(monitors, metadata):
    if (not isinstance(monitors,list) or not all(isinstance(m,dict) for m in monitors)
            or session_locked(monitors) is not False):
        return None
    candidates=monitors
    if len(candidates)!=1:
        position=metadata.get('position'); size=metadata.get('size')
        if not position or not size:return None
        candidates=[m for m in candidates if
                    [m.get('x'),m.get('y')]==list(position) and
                    [m.get('width'),m.get('height')]==list(size)]
    if len(candidates)!=1:return None
    monitor=candidates[0];name=monitor.get('name')
    if session_locked([monitor]) is not False:return None
    if monitor.get('dpmsStatus') is not True:return None
    if not isinstance(name,str) or not 1<=len(name)<=255 or '\0' in name:return None
    return name


class CaptureBootstrap:
    def __init__(self, metadata, needed):
        self.metadata=metadata or {};self.needed=needed
        self.cancelled=threading.Event();self.thread=None

    def start(self):
        if self.thread is not None or self.cancelled.is_set():return
        self.thread=threading.Thread(target=self._run,name='capture-bootstrap',daemon=True)
        self.thread.start()

    def _run(self):
        try:
            env=os.environ.copy()
            session=_bounded_output(['systemctl','--user','show-environment'],env,2,65536)
            for line in session.splitlines():
                key,sep,value=line.partition('=')
                if sep and key in ('WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE'):
                    env[key]=value
            monitors=json.loads(_bounded_output(['hyprctl','-j','monitors'],env,2,65536))
            name=selected_output(monitors,self.metadata)
            if name is None or self.cancelled.is_set() or not self.needed():return
            subprocess.run(['grim','-o',name,'-'],env=env,stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,timeout=2,check=False)
        except (OSError,subprocess.SubprocessError,ValueError,TypeError):
            pass  # Existing startup watchdog reports a fixed, classified failure.

    def close(self):
        self.cancelled.set()
        if self.thread is not None:self.thread.join(timeout=7)
