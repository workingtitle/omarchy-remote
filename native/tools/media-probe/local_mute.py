#!/usr/bin/env python3
"""Silent machine while a remote display session is on: in remote display mode the machine's own screen is off
and nobody sits at it, so its speakers are muted and the sound plays on the controlling device only (the stream
taps the sink before its mute, web/backend/audio.mjs). Only a mute this module set is ever
undone: the sink's name goes to a marker file first, and the service's post-session restore
(virtual_display.py --restore) and the next session undo a mute left by a crash. A sink the user muted
already is left alone."""
import os
import re
import subprocess

STATE_FILE = 'speakers-muted.json'
_SINK = re.compile(r'^[A-Za-z0-9_.:-]{1,255}$')


def _pactl(*args):
    return subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=2, check=True).stdout


class LocalMute:
    def __init__(self, state_dir=None, pactl=_pactl):
        self._path = os.path.join(state_dir or os.path.expanduser('~/.local/state/omarchy-remote'), STATE_FILE)
        self._pactl = pactl

    def apply(self, sink):
        """Mutes `sink`. True when it is muted by us now."""
        if not isinstance(sink, str) or not _SINK.match(sink):
            return False
        if os.path.exists(self._path):
            self.restore()
        try:
            if 'yes' in self._pactl('get-sink-mute', sink).lower():
                return False   # muted by the user: leave it alone
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            with open(self._path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                handle.write(sink)
            self._pactl('set-sink-mute', sink, '1')
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def restore(self):
        """Undoes this module's mute. True when nothing is left muted by it."""
        try:
            with open(self._path) as handle:
                sink = handle.read(300).strip()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if sink and _SINK.match(sink):
            try:
                self._pactl('set-sink-mute', sink, '0')
            except (OSError, subprocess.SubprocessError):
                return False
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        return True
