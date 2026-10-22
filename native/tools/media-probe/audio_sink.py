"""Bounded lookup and change detection for PipeWire's current default sink.

This module does not route streams itself. Call the change callback on the
GLib thread and retarget/restart the audio source there. Sink names are kept
in memory only and must not be written to ordinary telemetry.
"""
import re
import selectors
import subprocess
import time
import os

_NODE_NAME = re.compile(r'^\s*(?:\*\s+)?node\.name\s*=\s*"([A-Za-z0-9_.:-]{1,255})"\s*$', re.MULTILINE)
_MAX_OUTPUT = 16 * 1024


class SinkLookupError(RuntimeError):
    """The default sink could not be resolved within the configured bounds."""


def _bounded_wpctl_inspect(timeout=2.0):
    """Run wpctl with a wall-time and output cap; never include output in errors."""
    try:
        process = subprocess.Popen(
            ['wpctl', 'inspect', '@DEFAULT_AUDIO_SINK@'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise SinkLookupError('wpctl unavailable') from error

    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SinkLookupError('wpctl timed out')
            ready = selector.select(remaining)
            if not ready:
                raise SinkLookupError('wpctl timed out')
            try:
                chunk = os.read(process.stdout.fileno(), min(4096, _MAX_OUTPUT + 1 - len(output)))
            except BlockingIOError:
                continue
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_OUTPUT:
                raise SinkLookupError('wpctl output exceeded limit')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SinkLookupError('wpctl timed out')
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise SinkLookupError('wpctl timed out') from error
        if status != 0:
            raise SinkLookupError('wpctl failed')
        return output.decode('utf-8', errors='strict')
    except (UnicodeError, OSError) as error:
        raise SinkLookupError('invalid wpctl output') from error
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        if process.stdout:
            process.stdout.close()


def default_sink_name(inspect=_bounded_wpctl_inspect):
    """Return the default sink's node.name, rejecting ambiguous or unsafe text."""
    details = inspect()
    if not isinstance(details, str) or len(details.encode('utf-8')) > _MAX_OUTPUT:
        raise SinkLookupError('invalid wpctl output')
    matches = _NODE_NAME.findall(details)
    if len(matches) != 1:
        raise SinkLookupError('default sink name unavailable')
    return matches[0]


class DefaultSinkFollower:
    """Poll a bounded resolver and report each observed default-sink change."""
    def __init__(self, on_change, interval_seconds=2, inspect=_bounded_wpctl_inspect):
        if not callable(on_change):
            raise TypeError('on_change must be callable')
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 30:
            raise ValueError('interval_seconds must be 1..30')
        self._on_change = on_change
        self._inspect = inspect
        self.interval_seconds = interval_seconds
        self.current = None

    def poll(self):
        """Observe once. Returns True on a change, False if unchanged, None on lookup failure."""
        try:
            name = default_sink_name(self._inspect)
        except (SinkLookupError, OSError, UnicodeError):
            return None
        if name == self.current:
            return False
        self.current = name
        self._on_change(name)
        return True


class DefaultSinkWatcher:
    """Run DefaultSinkFollower.poll on a daemon thread, so the bounded wpctl lookup never
    blocks the media/input loop. on_change runs on that thread; callers must hand the
    actual retarget to their own loop (GLib.idle_add in the sender)."""
    def __init__(self, on_change, current, interval_seconds=2, inspect=_bounded_wpctl_inspect):
        import threading
        self.follower = DefaultSinkFollower(on_change, interval_seconds, inspect)
        self.follower.current = current
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='default-sink-watcher', daemon=True)

    def _run(self):
        while not self._stop.wait(self.follower.interval_seconds):
            self.follower.poll()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
