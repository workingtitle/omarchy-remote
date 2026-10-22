#!/usr/bin/env python3
"""Compact Omarchy bar for narrow remote displays (an iPhone in portrait). Omarchy's bar has one layout for all
screens, and on a phone-wide display the right side (tray, plugin buttons, status icons) runs off the edge. While
such a display is the machine's screen (remote display mode, physical outputs off), the bar layout in
~/.config/omarchy/shell.json is swapped for a short one (menu and workspaces left, indicators and weather right; no clock, no status icons); the
shell reloads the file by itself. The original layout is written to the state file first and put back when the
display is wide again or by `virtual_display.py --restore`, which the service runs after every session; a
layout the user changed in the meantime is left alone."""
import copy
import json
import os
import time

# Left stays (menu, workspaces); everything on the right is gone, and the indicators (they open on hover) and
# the weather move to the right edge. No clock: the phone shows its own time.
KEEP_LEFT = {'omarchy.menu', 'omarchy.workspaces', 'omarchy.spacer'}
MOVE_RIGHT = ('omarchy.indicators', 'omarchy.weather')
NARROW = 700           # logical pixels: below this the full bar does not fit
CONFIG = '~/.config/omarchy/shell.json'
STATE_FILE = 'compact-bar.json'
LOG_FILE = 'compact-bar.log'   # when shell.json was written (every write reloads the shell and recreates the bar), last 40 lines


MARGIN_SIZE = 28   # logical pixels when the client does not say how much it needs
MARGIN = {'id': 'omarchy.spacer', 'size': MARGIN_SIZE, 'omarchyRemoteMic': True}   # the iPad's orange microphone dot sits over the bar's right end


def without_margin(layout):
    """The layout without the spacer this module added for the microphone dot."""
    return {name: ([item for item in value if not (isinstance(item, dict) and item.get('omarchyRemoteMic'))] if isinstance(value, list) else value)
            for name, value in layout.items()}


def has_fullscreen(active_workspace_json):
    """Whether the active workspace has a full-screen window (`hyprctl -j activeworkspace`): the bar is hidden behind it, and a
    write of the shell config (which makes the shell recreate the bar) shows it over the window. False when it cannot be told."""
    try:
        return bool(json.loads(active_workspace_json).get('hasfullscreen'))
    except (ValueError, AttributeError, TypeError):
        return False


def is_narrow(width, scale):
    try:
        return float(scale) > 0 and float(width) / float(scale) < NARROW
    except (TypeError, ValueError):
        return False


def compact(layout):
    """Menu and workspaces left, nothing in the middle, indicators and weather right."""
    def items(section):
        value = layout.get(section)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    everything = [item for section in layout for item in items(section)]
    result = {section: copy.deepcopy(value) for section, value in layout.items() if not isinstance(value, list)}
    result['left'] = [copy.deepcopy(item) for item in items('left') if item.get('id') in KEEP_LEFT]
    result['center'] = []
    result['right'] = [copy.deepcopy(item) for wanted in MOVE_RIGHT for item in everything if item.get('id') == wanted][:len(MOVE_RIGHT)]
    return result


def only_additions(short, current):
    """True when `current` is the short layout plus extra widgets and nothing else changed (the installer enabling the
    bar widget); a layout the user edited otherwise stays as the user made it."""
    if not isinstance(current, dict) or not isinstance(short, dict) or set(current) != set(short):
        return False
    known = {item.get('id') for section in short.values() if isinstance(section, list) for item in section if isinstance(item, dict)}
    stripped = {name: ([item for item in value if not (isinstance(item, dict) and item.get('id') not in known)] if isinstance(value, list) else value)
                for name, value in current.items()}
    return stripped == short


def with_additions(original, short, current):
    """`original` plus the widgets that `current` has and the short layout `short` did not (appended on the right)."""
    def ids(layout):
        return {item.get('id') for section in layout.values() if isinstance(section, list) for item in section if isinstance(item, dict)}
    known = ids(short) | ids(original)
    added = [copy.deepcopy(item) for section in current.values() if isinstance(section, list)
             for item in section if isinstance(item, dict) and item.get('id') not in known]
    result = copy.deepcopy(original)
    if added:
        right = result.get('right') if isinstance(result.get('right'), list) else []
        result['right'] = right + added
    return result


class CompactBar:
    def __init__(self, config_path=None, state_dir=None):
        self._config = os.path.expanduser(config_path or CONFIG)
        directory = state_dir or os.path.expanduser('~/.local/state/omarchy-remote')
        self._state = os.path.join(directory, STATE_FILE)
        self._log = os.path.join(directory, LOG_FILE)

    def update(self, width, scale):
        """Compact for a narrow display (a phone upright), the original bar otherwise (a phone sideways is wide enough for it;
        a display that is wide from the start leaves the bar alone). True when the bar is as wanted."""
        if is_narrow(width, scale):
            return self.apply()
        return self.restore() if os.path.exists(self._state) else True

    def apply(self):
        if os.path.exists(self._state):
            try:
                state = self._read(self._state)
                if state['compact'] == compact(state['original']):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if not self.restore():   # a layout made by an older version: back to the original, then again
                return False
        try:
            config = self._read(self._config)
            layout = config['bar']['layout']
            if not isinstance(layout, dict):
                return False
            layout = without_margin(layout)
            short = compact(layout)
            if short == layout:
                return False
            anchor = config['bar'].get('centerAnchor')
            self._write(self._state, {'original': layout, 'compact': short, 'anchor': anchor}, mode=0o600)
            config['bar']['layout'] = short
            if anchor in ('omarchy.clock',):
                config['bar']['centerAnchor'] = ''   # the pinned centre widget is gone
            self._write(self._config, config)
            self._note('wrote the short bar')
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def restore(self):
        try:
            state = self._read(self._state)
        except FileNotFoundError:
            return self.margin(False)
        except (OSError, ValueError):
            self._discard()
            return False
        try:
            config = self._read(self._config)
            current = without_margin(config['bar']['layout'])
            if current == state['compact'] or only_additions(state['compact'], current):
                # The original again. If the short layout was edited meanwhile (the installer enabling the bar widget while a
                # phone was connected did), what was added stays: leaving the short bar for good was the old outcome
                # (seen once: a short bar on every display, the original lost).
                layout = state['original'] if current == state['compact'] else with_additions(state['original'], state['compact'], current)
                config['bar']['layout'] = layout
                if 'anchor' in state:   # states of the first version did not touch it
                    if state['anchor'] is None:
                        config['bar'].pop('centerAnchor', None)
                    else:
                        config['bar']['centerAnchor'] = state['anchor']
                self._write(self._config, config)
                self._note('put the original bar back' + ('' if current == state['compact'] else ' (with additions)'))
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self._discard()
        return True

    def margin(self, on, size=None):
        """A little blank space at the right end of the bar while the iPad's orange microphone dot is showing there, none
        otherwise. `size` in logical pixels (the client's geometry); a change of 2 px or less is not worth a write (every write
        makes the shell recreate the bar). True when as wanted."""
        try:
            size = max(8, min(80, int(round(size)))) if size else MARGIN_SIZE
            config = self._read(self._config)
            layout = config['bar']['layout']
            if not isinstance(layout, dict):
                return False
            wanted = without_margin(layout)
            if on:
                right = wanted.get('right') if isinstance(wanted.get('right'), list) else []
                wanted['right'] = right + [dict(MARGIN, size=size)]
            current = [item for section in layout.values() if isinstance(section, list) for item in section
                       if isinstance(item, dict) and item.get('omarchyRemoteMic')]
            if on and len(current) == 1 and isinstance(current[0].get('size'), (int, float)) and abs(current[0]['size'] - size) <= 2 \
                    and without_margin(layout) == without_margin(wanted):
                return True
            if wanted == layout:
                return True
            config['bar']['layout'] = wanted
            self._write(self._config, config)
            self._note(('space for the microphone dot (%d px)' % size) if on else 'space for the microphone dot removed')
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _note(self, text):
        """A line in a private log: the shell reloads its plugins and recreates the bar at every write of its config, which can
        show the bar over a fullscreen window; this tells when that was. Never raises."""
        try:
            try:
                with open(self._log) as handle:
                    lines = handle.read(8192).splitlines()[-39:]
            except OSError:
                lines = []
            lines.append(time.strftime('%Y-%m-%dT%H:%M:%S ') + text)
            os.makedirs(os.path.dirname(self._log), mode=0o700, exist_ok=True)
            descriptor = os.open(self._log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write('\n'.join(lines) + '\n')
        except OSError:
            pass

    @staticmethod
    def _read(path):
        with open(path) as handle:
            value = json.loads(handle.read(1 << 20))
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value

    @staticmethod
    def _write(path, value, mode=None):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        temporary = path + '.tmp'
        if mode is None:
            try:
                mode = os.stat(path).st_mode & 0o777
            except FileNotFoundError:
                mode = 0o600
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(json.dumps(value, indent=2) + '\n')
        os.replace(temporary, path)

    def _discard(self):
        try:
            os.remove(self._state)
        except FileNotFoundError:
            pass


class BarSync:
    """Keeps the bar as the session wants it: short for a narrow display, space at the right end for the iPad's microphone dot.
    Everything is requested here and written in one pass after a short pause, never while a full-screen window hides the bar
    (a write recreates the bar, which then showed over the window): it waits until the window is left. `schedule(ms, fn)` runs
    `fn` once after `ms` and returns a handle for `cancel`."""
    def __init__(self, bar, fullscreen, schedule, cancel, debounce=1000, poll=1000):
        self._bar, self._fullscreen, self._schedule, self._cancel = bar, fullscreen, schedule, cancel
        self._debounce, self._poll = debounce, poll
        self._display = None          # (width, scale) of the display, or None before the first one
        self._dot = (False, None)     # (on, size)
        self._timer = None

    def display(self, width, scale):
        self._display = (width, scale)
        self._request(self._debounce)

    def dot(self, on, size=None):
        if on == self._dot[0] and (not on or (size is not None and self._dot[1] is not None and abs(size - self._dot[1]) <= 2)
                                   or size == self._dot[1]):
            return
        self._dot = (on, size)
        self._request(300)

    def _request(self, delay):
        if self._timer is not None:
            self._cancel(self._timer)
        self._timer = self._schedule(delay, self._run)

    def _run(self):
        self._timer = None
        if self._fullscreen():
            self._timer = self._schedule(self._poll, self._run)   # try again when the window is left
            return
        if self._display is not None:
            self._bar.update(*self._display)
        self._bar.margin(*self._dot)

    def end(self):
        """The session is over: whatever is pending is dropped (the service puts the original bar back)."""
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
