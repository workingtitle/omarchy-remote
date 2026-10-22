"""Bounded first-video startup watchdog, independent of GLib/GStreamer."""
import time


class StartupWatchdog:
    def __init__(self, timeout_s, on_timeout, clock=time.monotonic):
        self._deadline = clock() + timeout_s
        self._clock = clock
        self._on_timeout = on_timeout
        self.capture_buffers = 0
        self.encoded_buffers = 0
        self._finished = False
        self._bootstrap_at = clock() + 2.0
        self._bootstrap_attempted = False

    def bootstrap_due(self):
        if (self._finished or self.capture_buffers or self._bootstrap_attempted or
                self._clock() < self._bootstrap_at):return False
        self._bootstrap_attempted=True
        return True

    def observe_capture(self):
        if not self._finished:
            self.capture_buffers += 1

    def observe_encoded(self):
        if not self._finished:
            self.encoded_buffers += 1

    def check(self, now=None):
        """Return False when the GLib timeout should be removed."""
        if self._finished:
            return False
        if (self._clock() if now is None else now) < self._deadline:
            return True
        self._finished = True
        phase = ('capture-no-buffers' if self.capture_buffers == 0 else
                 'encoding-no-buffers' if self.encoded_buffers == 0 else
                 'encoded-no-offer')
        self._on_timeout(phase)
        return False

    def cancel(self):
        self._finished = True


def startup_error_code(error):
    """Map a sender startup exception to a fixed signaling code; never forward its text."""
    text = str(error)
    if text.startswith('No usable encoder'):
        return 'encoder-unavailable'
    if 'no element' in text:
        # GStreamer parse error for a plugin that is not installed (e.g. webrtcbin, x264enc).
        return 'gstreamer-plugin-missing'
    if text.startswith('Portal not answering'):
        return 'portal-stuck'
    if text.startswith('Portal '):
        return 'capture-permission-denied'
    return 'sender-start-failed'
