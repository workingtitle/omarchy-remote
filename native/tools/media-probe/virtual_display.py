#!/usr/bin/env python3
"""Remote display mode: for one session the machine gets a headless output sized for the client, and its
physical outputs are switched off (extend=True: they stay on and the headless output sits to their right,
or left with side='left', as an extra screen), so every window and workspace moves onto the one display the client sees
(and nobody at the machine sees what the remote user does). When the session ends the physical outputs come
back with their saved mode, position, scale and transform, and the headless output is removed.

Safety: the original state is written before anything changes. The service restores it a few seconds after
every sender exit (unless another remote session takes the output over), before any mirroring session, and
at its own start, so a crashed session never leaves the machine's screen dark. Every Lua command is built from validated values only.
Dynamic resolution (display_mode.py) then sizes the headless output like a VM's virtual output."""
import fcntl
import json
import os
import re
import sys
import time

# Headless outputs are named OmarchyRemote-<n>, a new n each time one is created: Quickshell 0.3.1 crashed a
# few seconds after an output was removed and a new one with the same name appeared (its per-output state and
# screencast events are keyed by name).
NAME = 'OmarchyRemote'
_HEADLESS = re.compile(r'^OmarchyRemote(-[0-9]{1,6})?$')
_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')


def is_headless(name):
    return isinstance(name, str) and bool(_HEADLESS.match(name))


def new_name():
    return '%s-%d' % (NAME, int.from_bytes(os.urandom(3), 'big') % 1000000)
STATE_FILE = 'virtual-display.json'


def state_path(state_dir=None):
    return os.path.join(state_dir or os.path.expanduser('~/.local/state/omarchy-remote'), STATE_FILE)


def snapshot(monitor):
    """What is needed to bring a physical output back exactly as it was."""
    return {'name': monitor['name'], 'width': int(monitor['width']), 'height': int(monitor['height']),
            'refresh': round(float(monitor['refreshRate']), 3), 'x': int(monitor['x']), 'y': int(monitor['y']),
            'scale': float(monitor['scale']), 'transform': int(monitor.get('transform') or 0)}


def _checked(state):
    name = state['name']
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError('invalid output')
    width, height, x, y, transform = (int(state[k]) for k in ('width', 'height', 'x', 'y', 'transform'))
    refresh, scale = float(state['refresh']), float(state['scale'])
    if not (1 <= width <= 16384 and 1 <= height <= 16384 and -65536 <= x <= 65536 and -65536 <= y <= 65536
            and 1 <= refresh <= 500 and 0.5 <= scale <= 4 and 0 <= transform <= 7):
        raise ValueError('invalid mode')
    return name, width, height, x, y, transform, refresh, scale


def lua_restore(state):
    name, width, height, x, y, transform, refresh, scale = _checked(state)
    # disabled = false is required: a rule without it leaves a disabled output off (seen on Hyprland 0.56).
    return ('hl.monitor({ output = "%s", disabled = false, mode = "%dx%d@%s", position = "%dx%d", scale = %s, transform = %d })'
            % (name, width, height, ('%.3f' % refresh).rstrip('0').rstrip('.'), x, y, repr(scale), transform))


def lua_disable(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError('invalid output')
    return 'hl.monitor({ output = "%s", disabled = true })' % name


def lua_headless(name, width, height, scale, x=0, y=0):
    return lua_restore({'name': name, 'width': width, 'height': height, 'refresh': 60, 'x': x, 'y': y,
                        'scale': scale, 'transform': 0})


def active(monitors_json):
    data = json.loads(monitors_json) if isinstance(monitors_json, str) else monitors_json
    return [m for m in data if isinstance(m, dict) and not m.get('disabled')]


class _Lock:
    """One display change at a time across processes: the service's restore after a session and the next
    session's start used to run in the same second (the iPhone came back exactly when the grace period ended) and
    removed and created outputs over each other, which Quickshell did not survive."""
    def __init__(self, path, wait=20.0):
        self._path = path
        self._wait = wait
        self._fd = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + self._wait
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break   # never block a session forever
                    time.sleep(0.1)
        except OSError:
            self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
        return False


# Quickshell (GTK) crashes when outputs come and go in quick succession (omacom/omarchy#14193), most often when a
# session starts right after the previous one's screens were restored. Every change of the outputs waits until the
# last one is this old.
COOLDOWN = 12.0


class VirtualDisplay:
    """`hyprctl(args)` returns hyprctl's output (raises on failure), `evaluate(lua)` returns True on success."""
    def __init__(self, hyprctl, evaluate, path=None, sleep=time.sleep):
        self._hyprctl = hyprctl
        self._evaluate = evaluate
        self._path = path or state_path()
        self._sleep = sleep
        self.name = None   # the headless output of this session
        self.left_of = None   # extending to the left: the x its right edge keeps

    def _cool_down(self):
        """Waits until the outputs were last changed COOLDOWN seconds ago (one sleep: the stamp is a file's mtime)."""
        try:
            remaining = COOLDOWN - (time.time() - os.path.getmtime(self._path + '.changed'))
        except OSError:
            return
        if remaining > 0:
            print('VIRTUAL_DISPLAY cooldown %.1fs' % remaining, file=sys.stderr, flush=True)
            self._sleep(min(remaining, COOLDOWN))

    def _stamp(self):
        try:
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            with open(self._path + '.changed', 'w'):
                pass
        except OSError:
            pass

    def _monitors(self, all_outputs=False):
        return self._hyprctl(['-j', 'monitors'] + (['all'] if all_outputs else []))

    def start(self, *args, **kwargs):
        with _Lock(self._path + '.lock'):
            return self._start(*args, **kwargs)

    def restore(self):
        """Physical outputs back as saved, headless output removed. True when everything is as before."""
        with _Lock(self._path + '.lock'):
            return self._restore()

    def reassert_isolation(self):
        """The physical outputs are off for the session; a reload of the compositor's configuration (Omarchy's theme change)
        switches them on again. Switches them off again, only while this session's state is on disk (a restore removes it) and
        not when the physical outputs were left on (extending). Returns the names switched off."""
        try:
            with open(self._path) as handle:
                state = json.loads(handle.read(65536))
        except (OSError, ValueError):
            return []
        if state.get('extend') or not self.name or state.get('headless') != self.name:
            return []
        switched = []
        for monitor in active(self._monitors()):
            name = monitor.get('name')
            if not is_headless(name) and os.path.exists(self._path) and self._evaluate(lua_disable(name)):
                switched.append(name)
        return switched

    def _start(self, width=1280, height=800, scale=1.0, extend=False, side='right'):
        """Headless output on, physical outputs off (or, extending, left on). Raises (after restoring) if the result is not exactly
        the one headless output. Returns 'reused' when the previous session's headless output is still up
        (the service keeps it for a few seconds after a session ends, so a reconnect or a hand-over to another
        device does not add and remove outputs: Quickshell 0.x crashed tearing down a vanished output's
        windows), else 'created'."""
        if os.path.exists(self._path):
            try:
                with open(self._path) as handle:
                    previous = json.loads(handle.read(65536))
                saved = previous.get('headless')
                names = [m.get('name') for m in active(self._monitors())]
                physical_names = [p.get('name') for p in previous.get('physical', [])]
                expected = sorted(physical_names + [saved]) if extend else [saved]
                if (is_headless(saved) and bool(previous.get('extend')) == extend
                        and previous.get('side', 'right') == side
                        and sorted(n for n in names if isinstance(n, str)) == expected):
                    self._hyprctl(['dispatch', 'hl.dsp.focus({ monitor = "%s" })' % saved])
                    self.name = saved
                    if extend and side == 'left':
                        self.left_of = min(int(p['x']) for p in previous.get('physical', []))
                    return 'reused'
            except Exception:
                pass
        self._cool_down()
        self._restore()
        physical = [snapshot(m) for m in active(self._monitors()) if not is_headless(m.get('name'))]
        if not physical:
            raise RuntimeError('no physical output')
        for state in physical:
            _checked(state)
        os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
        temporary = self._path + '.tmp'
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
            name = new_name()
            handle.write(json.dumps({'v': 1, 'headless': name, 'physical': physical, 'extend': extend, 'side': side}))
        os.replace(temporary, self._path)
        self.name = name
        try:
            self._hyprctl(['output', 'create', 'headless', name])
            # Right of the physical outputs until they are off: at 0,0 it overlapped them for a moment and
            # Hyprland showed a "monitor layout set up incorrectly" warning.
            right = max(int(s['x'] + (s['height'] if s['transform'] % 2 else s['width']) / s['scale']) for s in physical)
            # Extending to the left: its right edge meets the leftmost physical output (display_mode.py keeps
            # it there when the size changes, through left_of).
            self.left_of = min(int(s['x']) for s in physical) if extend and side == 'left' else None
            x = self.left_of - int(round(width / scale)) if self.left_of is not None else right
            # Top edge level with the physical outputs' top: at y=0 an output that sits lower (a laptop panel at y=788)
            # shared only a strip of its height with the extra screen, and the cursor could cross only inside that strip.
            y = min(int(s['y']) for s in physical) if extend else 0
            if not self._evaluate(lua_headless(name, width, height, scale, x=x, y=y)):
                raise RuntimeError('headless mode rejected')
            if not extend:
                for state in physical:
                    if not self._evaluate(lua_disable(state['name'])):
                        raise RuntimeError('output not disabled')
            # Hyprland can switch a panel on again a moment after the rule (seen on a laptop: the session fell back to the
            # machine's own screen, so the device's size was never applied): look again, switch back off what came back.
            wanted = sorted([s['name'] for s in physical] + [name]) if extend else [name]
            for attempt in range(6):
                names = sorted(str(m.get('name')) for m in active(self._monitors()))
                if names == wanted: break
                if attempt == 5: raise RuntimeError('unexpected outputs %s, wanted %s' % (names, wanted))
                if not extend:
                    for state in physical:
                        if state['name'] in names: self._evaluate(lua_disable(state['name']))
                self._sleep(0.4)
            self._hyprctl(['dispatch', 'hl.dsp.focus({ monitor = "%s" })' % name])
        except Exception:
            self._restore()
            raise
        self._stamp()
        return 'created'

    def _restore(self):
        """Physical outputs back as saved, headless output removed. True when everything is as before."""
        ok = True
        try:
            with open(self._path) as handle:
                state = json.loads(handle.read(65536))
        except FileNotFoundError:
            state = None
        except (OSError, ValueError):
            state, ok = None, False
        saved_outputs = (state or {}).get('physical', [])
        if saved_outputs:
            self._cool_down()
        for saved in saved_outputs:
            try:
                ok = self._evaluate(lua_restore(saved)) and ok
            except (ValueError, KeyError, TypeError):
                ok = False
        try:
            # Hyprland turns the outputs back on a moment after the rule; removing the headless output before
            # that leaves the compositor without any output, and Quickshell then rebuilt its panels on a
            # placeholder screen and later crashed ("There are no outputs"). Wait for them (up to 2 s).
            wanted = {saved.get('name') for saved in saved_outputs}
            for _ in range(20):
                if wanted <= {m.get('name') for m in active(self._monitors())}:
                    break
                self._sleep(0.1)
            leftovers = [m.get('name') for m in json.loads(self._monitors(all_outputs=True)) if is_headless(m.get('name'))]
            if leftovers:
                if wanted and not wanted <= {m.get('name') for m in active(self._monitors())}:
                    raise RuntimeError('outputs not back yet')   # keep the headless output; retried later
                for name in leftovers:
                    self._hyprctl(['output', 'remove', name])
            # Only a check of the result counts: every saved output must be on again (else the state stays
            # for the service's next attempt).
            enabled = {m.get('name') for m in active(self._monitors())}
            if any(saved.get('name') not in enabled for saved in saved_outputs):
                ok = False
        except Exception:
            ok = False
        if ok:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass
            if saved_outputs:
                self._stamp()
        return ok


def pointer_region(monitors_json, output):
    """(x, y, width, height) of `output` as fractions of the layout's bounding box, which is what the wlr
    virtual pointer's absolute coordinates span; None when the output is unknown. A client's absolute
    position within the picture maps to x + fx * width, y + fy * height."""
    boxes = {}
    for monitor in active(monitors_json):
        scale = float(monitor.get('scale') or 1)
        width, height = monitor['width'] / scale, monitor['height'] / scale
        if int(monitor.get('transform') or 0) % 2:
            width, height = height, width
        boxes[monitor.get('name')] = (monitor['x'], monitor['y'], width, height)
    if output not in boxes:
        return None
    left = min(b[0] for b in boxes.values()); top = min(b[1] for b in boxes.values())
    right = max(b[0] + b[2] for b in boxes.values()); bottom = max(b[1] + b[3] for b in boxes.values())
    x, y, width, height = boxes[output]
    span_x, span_y = right - left, bottom - top
    return ((x - left) / span_x, (y - top) / span_y, width / span_x, height / span_y)


if __name__ == '__main__':
    if sys.argv[1:] != ['--restore']:
        raise SystemExit('usage: virtual_display.py --restore')
    from guest_state import hyprland_env, bounded_command
    env = hyprland_env()
    def hyprctl(args):
        return bounded_command(['hyprctl'] + args, env)
    def evaluate(lua):
        try:
            return 'error' not in hyprctl(['eval', lua]).lower()
        except Exception:
            return False
    from compact_bar import CompactBar
    from local_mute import LocalMute
    from virtual_mic import VirtualMic
    from power_profile import PowerProfile
    PowerProfile().restore()   # power-saver back after a session that ran on balanced
    VirtualMic().stop()   # a virtual microphone left by a crashed sender
    LocalMute().restore()   # the machine's speakers, muted for a remote display session
    CompactBar().restore()   # the short bar of a phone-wide session, whatever happened to the display
    if not os.path.exists(state_path()):
        raise SystemExit(0)
    restored = VirtualDisplay(hyprctl, evaluate).restore()
    print('VIRTUAL_DISPLAY restored=%s' % restored, file=sys.stderr)
    raise SystemExit(0 if restored else 1)
