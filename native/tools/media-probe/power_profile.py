#!/usr/bin/env python3
"""Performance for a remote session: Omarchy's power-saver profile keeps the CPU of a laptop at its lowest
frequency (a two-core Intel machine: 1.4 of 3.2 GHz) even on the mains, and the sender, encoder and a browser then starve each
other (load 10 on 4 threads: input leases that expire, heartbeats that time out, audio gaps, 5 s session
starts). While a session runs on the mains, power-saver becomes balanced; the previous profile goes
to a marker file first and comes back after the session (`virtual_display.py --restore`, which the service runs
a few seconds after the last session), a profile the user changed meanwhile is left alone."""
import glob
import json
import os
import subprocess

STATE_FILE = 'power-profile.json'
SAVER = 'power-saver'
BOOST = 'balanced'


def _ctl(*args):
    return subprocess.run(['powerprofilesctl', *args], capture_output=True, text=True, timeout=3, check=True).stdout.strip()


def on_mains(root='/sys/class/power_supply'):
    """True when a mains supply is online (False without one or when unknown)."""
    for supply in glob.glob(os.path.join(root, '*')):
        try:
            with open(os.path.join(supply, 'type')) as handle:
                if handle.read(16).strip() != 'Mains':
                    continue
            with open(os.path.join(supply, 'online')) as handle:
                if handle.read(4).strip() == '1':
                    return True
        except OSError:
            continue
    return False


class PowerProfile:
    def __init__(self, state_dir=None, ctl=_ctl, mains=on_mains):
        self._path = os.path.join(state_dir or os.path.expanduser('~/.local/state/omarchy-remote'), STATE_FILE)
        self._ctl = ctl
        self._mains = mains

    def apply(self):
        """power-saver -> balanced on the mains. True when the profile is as wanted (or nothing to do)."""
        if os.path.exists(self._path):
            return True
        try:
            if not self._mains() or self._ctl('get') != SAVER:
                return True
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            with open(self._path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                handle.write(json.dumps({'previous': SAVER, 'set': BOOST}))
            self._ctl('set', BOOST)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def restore(self):
        """Back to what it was, unless it was changed meanwhile. True when nothing is left to restore."""
        try:
            with open(self._path) as handle:
                state = json.loads(handle.read(256))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            state = {}
        try:
            if state.get('previous') == SAVER and self._ctl('get') == state.get('set'):
                self._ctl('set', SAVER)
        except (OSError, subprocess.SubprocessError):
            return False
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        return True
