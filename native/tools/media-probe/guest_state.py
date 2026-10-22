"""Read-only compositor state for guest test targets.

An ext-session-lock (Omarchy locks after its idle timeout) holds keyboard focus, while
`hyprctl activewindow` still names the last focused window and focus requests return ok
without effect. Hyprland exposes the lock only as LOCK in a monitor's solitaryBlockedBy,
the same signal omarchy-hyprland-session-locked uses. Never unlock from here.
"""
import os
import subprocess
import json,select,time


CAPTURE_SESSION_LOCKED = 'session-locked'
CAPTURE_DISPLAY_OFF = 'display-off'
CAPTURE_START_TIMEOUT = 'capture-start-timeout'

def session_locked(monitors):
    """True/False from `hyprctl -j monitors` output; None when no monitor reports the field."""
    try:data=json.loads(monitors) if isinstance(monitors,str) else monitors
    except ValueError:return None
    if not isinstance(data,list):return None
    seen=False
    for monitor in data:
        blockers=monitor.get('solitaryBlockedBy') if isinstance(monitor,dict) else None
        if isinstance(blockers,list):
            seen=True
            if 'LOCK' in blockers:return True
    return False if seen else None

def focus_state(active,monitors,pid):
    """'locked', 'ok', 'other' or 'unknown'. The lock is checked first because activewindow is stale while locked."""
    if session_locked(monitors):return 'locked'
    try:data=json.loads(active) if isinstance(active,str) else active
    except ValueError:return 'unknown'
    if not isinstance(data,dict):return 'unknown'
    return 'ok' if data.get('pid')==pid else 'other'


def capture_failure_code(monitors):
    """Classify a no-first-buffer failure from monitor state without side effects."""
    try:data=json.loads(monitors) if isinstance(monitors,str) else monitors
    except (TypeError, ValueError):return CAPTURE_START_TIMEOUT
    if not isinstance(data,list) or not data:return CAPTURE_START_TIMEOUT
    if any(isinstance(m,dict) and isinstance(m.get('solitaryBlockedBy'),list)
           and 'LOCK' in m['solitaryBlockedBy'] for m in data):
        return CAPTURE_SESSION_LOCKED
    dpms=[m.get('dpmsStatus') for m in data if isinstance(m,dict)]
    if len(dpms)==len(data) and dpms and all(value is False for value in dpms):
        return CAPTURE_DISPLAY_OFF
    return CAPTURE_START_TIMEOUT


def hyprland_env(timeout_s=2.0, max_output_bytes=64 * 1024):
    """Minimal environment for hyprctl from the user session (SSH shells lack the Hyprland signature)."""
    allowed=('PATH','HOME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS',
             'WAYLAND_DISPLAY','HYPRLAND_SIGNATURE','HYPRLAND_INSTANCE_SIGNATURE')
    env={key:value for key in allowed if (value:=os.environ.get(key)) is not None}
    session=_bounded_output(['systemctl','--user','show-environment'],env,timeout_s,max_output_bytes)
    for line in session.splitlines():
        key,value=line.split('=',1) if '=' in line else ('','')
        if key in ('WAYLAND_DISPLAY','HYPRLAND_SIGNATURE','HYPRLAND_INSTANCE_SIGNATURE'):
            env[key]=value
    return env


def bounded_command(command, env, timeout_s=2.0, max_output_bytes=64 * 1024):
    """Run a command with wall-time and output bounds; raises OSError/SubprocessError on failure."""
    return _bounded_output(command,env,timeout_s,max_output_bytes)


def read_capture_failure_code(timeout_s=2.0, max_output_bytes=64 * 1024):
    """Read bounded monitor JSON via the user session, returning a safe fallback."""
    if type(timeout_s) not in (int,float) or timeout_s <= 0:
        return CAPTURE_START_TIMEOUT
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        return CAPTURE_START_TIMEOUT
    try:
        env=hyprland_env(timeout_s,max_output_bytes)
        result=_bounded_output(['hyprctl','-j','monitors'],env,timeout_s,max_output_bytes)
        return capture_failure_code(result)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return CAPTURE_START_TIMEOUT

MESSAGES={'ok':'FOCUS ok','locked':'FOCUS locked: the guest session is locked; unlock it on the guest first',
          'other':'FOCUS other: click the target window','unknown':'FOCUS unknown'}


def _bounded_output(command,env,timeout_s,limit):
    process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    data=bytearray();end=time.monotonic()+timeout_s
    try:
        while True:
            left=end-time.monotonic()
            if left<=0 or not select.select([process.stdout],[],[],left)[0]:raise subprocess.TimeoutExpired(command,timeout_s)
            chunk=os.read(process.stdout.fileno(),min(4096,limit+1-len(data)))
            if not chunk:break
            data.extend(chunk)
            if len(data)>limit:raise OSError('output limit')
        if process.wait(timeout=max(.01,end-time.monotonic()))!=0:raise OSError('command failed')
        return data.decode('utf-8')
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close()
