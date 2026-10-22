"""Dynamic guest resolution for virtual outputs (client window size -> Hyprland monitor mode).

Messages on the `display` data channel (UTF-8 JSON, bounded):
  {"v":1,"type":"display-size","width":W,"height":H[,"scale":S]}      client -> guest (device pixels)
  {"v":1,"type":"display-mode","width":W,"height":H,"scale":S,"applied":bool,"reason":R}   guest -> client

W and H are the client's video area in device pixels, landscape or portrait. The optional scale
(1-3, snapped to quarter steps) sets how large the guest desktop draws: pixels stay native, so a
larger scale means larger text, a smaller one more space. Without it the output keeps its scale.
A phone client sends its view's native pixels (portrait 1179x2556 on a 6.1" iPhone) and offers
scales around the device's own (3 = iOS size, 2 or 1.5 = more space).

Only a single virtual output (Virtual-*, e.g. virtio-gpu in a VM) is ever changed; a physical
panel keeps its mode. The original mode is saved to a private state file before the first change
and restored when the session ends, or at the next sender start after a crash. Requests are
coalesced: only the newest size is applied, at most once per second.
"""
import json
import os
import re
import time

# Bounds apply to the long and short side, so portrait sizes get the same range as landscape.
MIN_LONG, MAX_LONG = 640, 3840
MIN_SHORT, MAX_SHORT = 480, 2400
MIN_SCALE, MAX_SCALE = 1.0, 3.0
MAX_MESSAGE_BYTES = 1024
APPLY_INTERVAL_NS = 1_000_000_000
# Virtual-N: a VM's virtio-gpu output; OmarchyRemote: the headless output of the remote display mode
# (virtual_display.py).
_OUTPUT = re.compile(r'^(Virtual-[0-9]{1,3}|OmarchyRemote(-[0-9]{1,6})?)$')
REASONS = ('applied', 'unchanged', 'not-virtual', 'multiple-outputs', 'no-output', 'compositor-error', 'not-accepted')


def parse(raw):
    """Validate one display-size request; returns (width, height) or raises ValueError."""
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
        raise ValueError('invalid display message size')
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get('v') != 1 or message.get('type') != 'display-size':
        raise ValueError('unsupported display message')
    width, height = message.get('width'), message.get('height')
    if type(width) is not int or type(height) is not int:
        raise ValueError('invalid display size')
    if not (MIN_SHORT // 2 <= width <= 16384 and MIN_SHORT // 2 <= height <= 16384):
        raise ValueError('invalid display size')
    scale = message.get('scale')
    if scale is not None:
        if type(scale) not in (int, float) or not MIN_SCALE <= scale <= MAX_SCALE:
            raise ValueError('invalid display scale')
        scale = round(scale * 4) / 4
    return width, height, scale


def encode_result(width, height, scale, applied, reason):
    if reason not in REASONS:
        raise ValueError('unknown reason')
    return json.dumps({'v': 1, 'type': 'display-mode', 'width': width, 'height': height, 'scale': scale,
                       'applied': applied, 'reason': reason}, separators=(',', ':'))


def target_size(width, height, scale):
    """Clamp to the supported range and round down to multiples of 8 whose logical size
    (pixels / scale) is a whole number, so Hyprland accepts the scale unchanged."""
    portrait = height > width
    long_side, short_side = (height, width) if portrait else (width, height)
    long_side = max(MIN_LONG, min(MAX_LONG, long_side))
    short_side = max(MIN_SHORT, min(MAX_SHORT, short_side))
    if not isinstance(scale, (int, float)) or not 0.5 <= scale <= 4:
        scale = 1

    def fit(value, low):
        value -= value % 8
        while value >= low:
            logical = value / scale
            if abs(logical - round(logical)) < 1e-6:
                return value
            value -= 8
        return low
    long_side, short_side = fit(long_side, MIN_LONG), fit(short_side, MIN_SHORT)
    return (short_side, long_side) if portrait else (long_side, short_side)


def fit_capacity(width, height, scale, limit):
    """Keep a requested mode within what this machine can encode without downscaling (the sender's
    video limits; on two cores 1280x720). Larger modes cost capture and scaling time on weak guests
    (measured ~200 ms extra at 2048x1424 on two vCPUs). The scale shrinks by the same factor, so the
    desktop keeps about the requested logical size; the client then upscales the picture."""
    if not limit:
        return width, height, scale
    box_long, box_short = max(limit), min(limit)
    long_side, short_side = max(width, height), min(width, height)
    factor = min(1.0, box_long / long_side, box_short / short_side)
    if factor >= 1:
        return width, height, scale
    scale = None if scale is None else max(MIN_SCALE, round(scale * factor * 4) / 4)
    return int(width * factor), int(height * factor), scale


def virtual_output(monitors, output=None):
    """(monitor dict, None) for exactly one enabled virtual output, else (None, reason). With `output` (the
    extra screen's headless output beside the physical ones) that output is used whatever else is enabled."""
    try:
        data = json.loads(monitors) if isinstance(monitors, str) else monitors
    except ValueError:
        return None, 'compositor-error'
    if not isinstance(data, list):
        return None, 'compositor-error'
    active = [m for m in data if isinstance(m, dict) and not m.get('disabled')]
    if not active:
        return None, 'no-output'
    if output is not None:
        active = [m for m in active if m.get('name') == output]
        if not active:
            return None, 'no-output'
    if len(active) != 1:
        return None, 'multiple-outputs'
    monitor = active[0]
    if not isinstance(monitor.get('name'), str) or not _OUTPUT.match(monitor['name']):
        return None, 'not-virtual'
    for key in ('width', 'height', 'x', 'y'):
        if type(monitor.get(key)) is not int:
            return None, 'compositor-error'
    if not isinstance(monitor.get('scale'), (int, float)) or not isinstance(monitor.get('refreshRate'), (int, float)):
        return None, 'compositor-error'
    return monitor, None


def snapshot(monitor):
    return {'name': monitor['name'], 'width': monitor['width'], 'height': monitor['height'],
            'refresh': round(float(monitor['refreshRate']), 3), 'x': monitor['x'], 'y': monitor['y'],
            'scale': float(monitor['scale'])}


def lua_monitor(state):
    """Hyprland Lua for one monitor rule; every value is validated here, never taken raw."""
    name = state['name']
    if not isinstance(name, str) or not _OUTPUT.match(name):
        raise ValueError('invalid output')
    width, height, x, y = (int(state[k]) for k in ('width', 'height', 'x', 'y'))
    refresh, scale = float(state['refresh']), float(state['scale'])
    if not (1 <= width <= 16384 and 1 <= height <= 16384 and -65536 <= x <= 65536 and -65536 <= y <= 65536
            and 1 <= refresh <= 500 and 0.5 <= scale <= 4):
        raise ValueError('invalid mode')
    return ('hl.monitor({ output = "%s", mode = "%dx%d@%s", position = "%dx%d", scale = %s })'
            % (name, width, height, ('%.3f' % refresh).rstrip('0').rstrip('.'), x, y, repr(scale)))


class DisplayMode:
    """Guest side. `monitors()` returns hyprctl -j monitors text; `evaluate(lua)` returns True on success.
    `send` receives encoded results. The caller drives `tick(now_ns)` from its main loop."""
    def __init__(self, monitors, evaluate, send, state_path, sleep=time.sleep, limit=None):
        self._monitors = monitors
        self._evaluate = evaluate
        self._send = send
        self._state_path = state_path
        self._pending = None
        self._last_apply = None
        self._changed = False
        self._applied = None         # (width, height, scale) the compositor was last given and accepted
        self._reasserted = None      # when the last reassert was tried (ns)
        self._sleep = sleep
        self._limit = limit          # (max_width, max_height) the sender encodes; None = no cap
        self.output = None           # the extra screen's headless output, when others stay enabled
        self.on_applied = None       # called with (width, height, scale) after a resize took effect
        self.left_of = None          # extending to the left: the x its right edge keeps while resizing
        self.keep_mode = False       # mirror: the machine's own screen as it is, also a VM's virtual output (answered as 'not-virtual')

    def request(self, raw):
        self._pending = parse(raw)

    def tick(self, now_ns):
        if self._pending is None:
            return False
        if self._last_apply is not None and now_ns - self._last_apply < APPLY_INTERVAL_NS:
            return False
        width, height, scale = self._pending
        self._pending = None
        self._last_apply = now_ns
        result = self._apply(width, height, scale)
        self._send(encode_result(*result))
        if result[3] and self.on_applied:
            try:
                self.on_applied(result[0], result[1], result[2])
            except Exception:
                pass
        return True

    REASSERT_INTERVAL_NS = 3_000_000_000

    def reassert(self, now_ns):
        """The compositor forgot the mode (a reload of its configuration, which Omarchy's theme change does, puts every output
        back to its configured mode): the last accepted mode is applied again and the device told. Not while a request waits,
        and not more often than every few seconds (a mode the compositor keeps refusing). True when it applied again."""
        if self._applied is None or self._pending is not None:
            return False
        if self._reasserted is not None and now_ns - self._reasserted < self.REASSERT_INTERVAL_NS:
            return False
        monitor, _ = virtual_output(self._read(), self.output)
        if monitor is None:
            return False
        width, height, scale = self._applied
        if (monitor['width'], monitor['height']) == (width, height) and abs(float(monitor['scale']) - scale) < 1e-3:
            return False
        self._reasserted = now_ns
        result = self._apply(width, height, scale)
        self._send(encode_result(*result))
        if result[3] and self.on_applied:
            try:
                self.on_applied(result[0], result[1], result[2])
            except Exception:
                pass
        return result[3]

    def _apply(self, width, height, scale=None):
        if self.keep_mode:
            return width, height, scale, False, 'not-virtual'
        monitor, reason = virtual_output(self._read(), self.output)
        if monitor is None:
            return width, height, scale, False, reason
        scale = float(monitor['scale']) if scale is None else float(scale)
        width, height, scale = fit_capacity(width, height, scale, self._limit)
        width, height = target_size(width, height, scale)
        if (monitor['width'], monitor['height']) == (width, height) and abs(float(monitor['scale']) - scale) < 1e-3:
            self._applied = (width, height, scale)
            return width, height, scale, True, 'unchanged'
        if not os.path.exists(self._state_path):
            self._save(snapshot(monitor))
        wanted = dict(snapshot(monitor), width=width, height=height, refresh=60, scale=scale)
        if self.left_of is not None:
            wanted['x'] = self.left_of - int(round(width / scale))
        if not self._evaluate(lua_monitor(wanted)):
            return width, height, scale, False, 'compositor-error'
        self._changed = True
        # Hyprland applies the mode shortly after eval returns; re-read briefly (bounded, ~0.5 s).
        for attempt in range(6):
            now, _ = virtual_output(self._read(), self.output)
            if (now is not None and (now['width'], now['height']) == (width, height)
                    and abs(float(now['scale']) - scale) < 1e-3):
                self._applied = (width, height, scale)
                return width, height, scale, True, 'applied'
            if attempt < 5:
                self._sleep(0.1)
        return width, height, scale, False, 'not-accepted'

    def restore(self):
        """Restore the saved original mode; returns True when nothing was left to restore."""
        try:
            with open(self._state_path) as handle:
                state = json.loads(handle.read(4096))
            command = lua_monitor(state)
        except FileNotFoundError:
            return True
        except (OSError, ValueError, KeyError, TypeError):
            self._discard()
            return False
        ok = self._evaluate(command)
        if ok:
            self._discard()
        return ok

    def _read(self):
        try:
            return self._monitors()
        except Exception:
            return None

    def _save(self, state):
        directory = os.path.dirname(self._state_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        temporary = self._state_path + '.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(json.dumps(state))
        os.replace(temporary, self._state_path)

    def _discard(self):
        try:
            os.remove(self._state_path)
        except FileNotFoundError:
            pass
