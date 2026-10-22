#!/usr/bin/env python3
"""A virtual microphone on the machine for the controlling device's microphone ("Omarchy Remote Mic").
PulseAudio/PipeWire recipe (measured on a two-core Intel machine): a null sink plus a remap source on its monitor; the
sender plays the decoded microphone audio into the sink (pulsesink device=SINK) and applications see the remap
source as a normal microphone. Created when the controlling device offers a microphone stream, removed at the
end of the session; the module ids go to a marker file first, so `virtual_display.py --restore` (the service,
after every session) and the next session remove what a crash left behind."""
import json
import os
import re
import subprocess

SINK = 'omarchy_remote_mic_sink'
SOURCE = 'omarchy_remote_mic'
OUTPUT = 'omarchy_remote_output'   # a silent output for a machine with no sound card (see _ensure_output)
FEED = 'Omarchy-Remote-Mic-Feed'   # the stream's name (the sender's pulsesink title)
STATE_FILE = 'virtual-mic.json'
DEFAULT_FILE = 'virtual-mic-default.json'   # the input that was the default before this microphone took over
_ID = re.compile(r'^[0-9]{1,12}$')


def _pactl(*args):
    return subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=3, check=True).stdout.strip()


class VirtualMic:
    def __init__(self, state_dir=None, pactl=_pactl):
        directory = state_dir or os.path.expanduser('~/.local/state/omarchy-remote')
        self._path = os.path.join(directory, STATE_FILE)
        self._default_path = os.path.join(directory, DEFAULT_FILE)
        self._pactl = pactl

    sink = SINK

    def start(self):
        """Creates the microphone. True when it exists afterwards."""
        if os.path.exists(self._path):
            self.stop()
        ids = []
        try:
            self._ensure_output(ids)
            ids.append(self._pactl('load-module', 'module-null-sink', 'sink_name=' + SINK,
                                   'sink_properties=device.description=Omarchy-Remote-Mic-Sink'))
            self._save(ids)
            ids.append(self._pactl('load-module', 'module-remap-source', 'master=%s.monitor' % SINK, 'source_name=' + SOURCE,
                                   'source_properties=device.description=Omarchy-Remote-Mic'))
            self._save(ids)
            self._take_default()
            self._default_output()
            return all(_ID.match(i) for i in ids)
        except (OSError, subprocess.SubprocessError):
            self.stop()
            return False

    def stop(self):
        """Removes the microphone. True when nothing is left."""
        try:
            with open(self._path) as handle:
                ids = json.loads(handle.read(1024))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            ids = []
        self._give_back_default()
        ok = True
        for module in reversed([i for i in ids if isinstance(i, str) and _ID.match(i)]):
            try:
                self._pactl('unload-module', module)
            except (OSError, subprocess.SubprocessError):
                ok = False
        if ok:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass
        return ok

    def _outputs(self):
        """Names of the machine's sinks, one per line of `pactl list short sinks`."""
        return [line.split('\t')[1] for line in self._pactl('list', 'short', 'sinks').splitlines() if '\t' in line]

    def _ensure_output(self, ids):
        """A machine with no sound card (a virtual machine) has only PipeWire's dummy output, which disappears as soon as another
        sink exists: this microphone's own sink then became the default output, the sender captured the machine's sound from it
        (the microphone's own voice) and played it back on the controlling device: a feedback loop. A silent output of our own
        keeps the machine's sound separate."""
        if any(name not in (SINK, OUTPUT, 'auto_null') for name in self._outputs()):
            return
        ids.append(self._pactl('load-module', 'module-null-sink', 'sink_name=' + OUTPUT,
                               'sink_properties=device.description=Omarchy-Remote-Output'))
        self._save(ids)

    def _default_output(self):
        """Makes our silent output the default output (when we made one), and keeps the microphone's sink from being it."""
        try:
            names = self._outputs()
            if OUTPUT in names and self._pactl('get-default-sink').strip() != OUTPUT:
                self._pactl('set-default-sink', OUTPUT)
        except (OSError, subprocess.SubprocessError):
            pass

    def keep_feed(self):
        """Puts the sender's feed stream back into the microphone's sink when something moved it (the audio panel's output
        choice moves every playing stream to the chosen speaker: the voice then came out of the speakers, was captured
        into the stream and fed back). Returns how many streams were moved."""
        self._default_output()
        try:
            sinks = {line.split('\t')[1]: line.split('\t')[0] for line in self._pactl('list', 'short', 'sinks').splitlines() if '\t' in line}
            target = sinks.get(SINK)
            if not target:
                return 0
            moved = 0
            for stream in json.loads(self._pactl('--format=json', 'list', 'sink-inputs') or '[]'):
                if stream.get('properties', {}).get('media.name') == FEED and str(stream.get('sink')) != target:
                    self._pactl('move-sink-input', str(stream['index']), target)
                    moved += 1
            return moved
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0

    def _take_default(self):
        """Programs on the machine use whatever input is the default: while this microphone lives it is the default, so a
        call or a recording hears the controlling device without choosing it by hand. The previous default is kept."""
        try:
            previous = self._pactl('get-default-source').strip()
            if previous and previous != SOURCE:
                os.makedirs(os.path.dirname(self._default_path), mode=0o700, exist_ok=True)
                with open(self._default_path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                    handle.write(json.dumps(previous))
            self._pactl('set-default-source', SOURCE)
        except (OSError, subprocess.SubprocessError):
            pass   # the microphone works without being the default

    def _give_back_default(self):
        """The previous default again, if this microphone is still the default and the previous input still exists."""
        try:
            with open(self._default_path) as handle:
                previous = json.loads(handle.read(512))
        except (OSError, ValueError):
            return
        try:
            if isinstance(previous, str) and re.match(r'^[A-Za-z0-9_.:-]{1,200}$', previous) \
                    and self._pactl('get-default-source').strip() == SOURCE \
                    and any(line.split('\t')[1:2] == [previous] for line in self._pactl('list', 'short', 'sources').splitlines()):
                self._pactl('set-default-source', previous)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.remove(self._default_path)
        except FileNotFoundError:
            pass

    def _save(self, ids):
        os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
        with open(self._path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            handle.write(json.dumps(ids))
