"""Opt-in text clipboard sync between client and guest (Wayland, wl-clipboard).

Messages on the `clipboard` data channel (UTF-8 JSON, bounded):
  {"v":1,"type":"clipboard-enable","enabled":true|false}   client -> guest
  {"v":1,"type":"clipboard","rev":N,"text":"..."}           both directions
Clipboard content is never logged. Echoes are suppressed by content digest, so text we
just applied is not sent back when wl-paste --watch reports the change.
"""
import hashlib
import json
import os
import selectors
import subprocess
import threading
import time

MAX_TEXT_BYTES = 200 * 1024      # an SCTP message must stay well below libwebrtc's 256 KiB limit
MAX_MESSAGE_BYTES = 256 * 1024


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def encode(rev, text):
    data = json.dumps({'v': 1, 'type': 'clipboard', 'rev': rev, 'text': text}, separators=(',', ':'))
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES or len(data.encode('utf-8')) > MAX_MESSAGE_BYTES:
        raise ValueError('clipboard text too large')
    return data


def parse(raw):
    """Validate one message; returns the dict or raises ValueError (never echoes content)."""
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
        raise ValueError('invalid clipboard message size')
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get('v') != 1:
        raise ValueError('invalid clipboard envelope')
    kind = message.get('type')
    if kind == 'clipboard-enable':
        if type(message.get('enabled')) is not bool:
            raise ValueError('invalid clipboard-enable')
    elif kind == 'clipboard':
        rev, text = message.get('rev'), message.get('text')
        if type(rev) is not int or rev < 0 or not isinstance(text, str) or len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            raise ValueError('invalid clipboard text')
    else:
        raise ValueError('unsupported clipboard message')
    return message


def _bounded(argv, env, data=None, timeout=2.0, limit=MAX_TEXT_BYTES + 1):
    """Run a command with wall-time and output bounds; returns stdout bytes or None."""
    try:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        output, _ = process.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    if process.returncode != 0 or len(output) > limit:
        return None
    return output


class ClipboardSync:
    """Guest side. `send` is called with an encoded message (hand it to the GLib loop)."""
    def __init__(self, send, env=None, copy=None, paste=None, watch=True, sensitive=None):
        self._send = send
        self._env = env if env is not None else os.environ.copy()
        self._copy = copy or self._wl_copy
        self._paste = paste or self._wl_paste
        self._sensitive = sensitive or self._wl_sensitive
        self._use_watch = watch
        self._lock = threading.Lock()
        self.enabled = False
        self.rev = 0
        self._applied = None     # digest of text last written from the client
        self._sent = None        # digest of text last sent to the client
        self._watcher = None
        self._stop = threading.Event()
        # Remote text is applied on a worker so the data-channel thread never waits on wl-copy;
        # only the newest pending text is kept.
        self._pending = None
        self._wake = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, name='clipboard-write', daemon=True)
        self._writer.start()

    def _write_loop(self):
        while not self._stop.is_set():
            if not self._wake.wait(0.5):
                continue
            self._wake.clear()
            with self._lock:
                text, self._pending = self._pending, None
            if text is not None:
                self._note('CLIPBOARD_APPLIED ok=%s' % bool(self._copy(text)))

    @staticmethod
    def _note(line):
        # Fixed markers only; never clipboard content.
        import sys
        print(line, file=sys.stderr, flush=True)

    def _wl_copy(self, text):
        # wl-copy forks a server that keeps serving the selection; never wait on its output
        # pipes (that blocks until the selection is replaced), only on the short-lived parent.
        try:
            process = subprocess.Popen(['wl-copy'], env=self._env, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        try:
            process.stdin.write(text.encode('utf-8')); process.stdin.close()
            return process.wait(timeout=2) == 0
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            return False

    def _wl_sensitive(self):
        # Password managers mark what they copy (KDE's hint, also set by Omarchy's tools): that never leaves the machine.
        output = _bounded(['wl-paste', '--list-types'], self._env, limit=4096)
        return output is not None and b'x-kde-passwordManagerHint' in output.split()

    def _wl_paste(self):
        output = _bounded(['wl-paste', '--no-newline', '--type', 'text'], self._env)
        if output is None:
            return None
        try:
            return output.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def handle(self, raw):
        message = parse(raw)
        if message['type'] == 'clipboard-enable':
            self._set_enabled(message['enabled'])
            self._note('CLIPBOARD_ENABLED ' + ('on' if message['enabled'] else 'off'))
            return
        with self._lock:
            value = digest(message['text'])
            # Skip text we already hold (own echo or a second bridge re-publishing it).
            if not self.enabled or value in (self._applied, self._sent):
                return
            self._applied = value
            self._pending = message['text']
        self._wake.set()

    def local_changed(self):
        """Called when the guest clipboard changed; sends it unless it is our own echo."""
        with self._lock:
            if not self.enabled:
                return False
        if self._sensitive():
            return False
        text = self._paste()
        if text is None or len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            return False
        value = digest(text)
        with self._lock:
            if not self.enabled or value in (self._applied, self._sent):
                return False
            self._sent = value
            self.rev += 1
            rev = self.rev
        self._send(encode(rev, text))
        self._note('CLIPBOARD_SENT')
        return True

    def _set_enabled(self, enabled):
        with self._lock:
            changed = enabled != self.enabled
            self.enabled = enabled
        if changed and enabled and self._use_watch:
            self._watch_stop = threading.Event()
            self._watcher = threading.Thread(target=self._watch, name='clipboard-watch', daemon=True)
            self._watcher.start()
        elif changed and not enabled and self._watcher:
            self._watch_stop.set()

    def _watch(self):
        # One notification line per change; content is then read with a bounded wl-paste.
        try:
            process = subprocess.Popen(['wl-paste', '--watch', 'echo', 'changed'], env=self._env,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            stop = self._watch_stop
            while not stop.is_set() and not self._stop.is_set():
                if not selector.select(0.5):
                    continue
                line = process.stdout.readline(64)
                if not line:
                    break
                time.sleep(0.05)  # let the new owner publish its offer
                self.local_changed()
        finally:
            selector.close()
            process.kill()
            process.wait(timeout=2)

    def close(self):
        self._stop.set(); self._wake.set()
        if self._watcher and self._watcher.is_alive():
            self._watcher.join(timeout=3)
        self._writer.join(timeout=3)
