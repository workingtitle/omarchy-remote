#!/usr/bin/env python3
"""Taking the machine back at its own keyboard. In remote display mode the physical screens are off for the session
(black); a key or a click on a physical keyboard or trackpad of the machine then ends the session, so the screens come
back. Only real devices count (a bus path in `phys`): virtual ones (RustDesk, the sender's own) have none. Moving a
mouse or brushing a trackpad does nothing; a key press or a button press does."""
import os

KEY_A, BTN_LEFT, BTN_MIDDLE = 30, 272, 274
EV_KEY = 1
NOT_PHYSICAL = ('ALSA', 'isa', 'LNX', 'PNP', 'LNXVIDEO')   # speaker, audio jacks, power/lid/video buttons


def physical(phys, keys):
    """A real keyboard or pointer: it has a bus path that is not one of the platform's own and keys of its kind."""
    if not phys or phys.startswith(NOT_PHYSICAL):
        return False
    return KEY_A in keys or BTN_LEFT in keys


def is_wake(event_type, code, value, keys):
    """A key pressed (not released or repeated) on a keyboard, or a button pressed on a pointer."""
    if event_type != EV_KEY or value != 1:
        return False
    if KEY_A in keys:
        return 1 <= code <= 248
    return BTN_LEFT <= code <= BTN_MIDDLE


class LocalReclaim:
    """Watches the physical devices and calls `on_reclaim()` once, after `grace` seconds have passed since start()."""
    def __init__(self, on_reclaim, grace=4.0, clock=None, devices=None):
        import time
        self._on = on_reclaim
        self._grace = grace
        self._clock = clock or time.monotonic
        self._devices = devices
        self._armed_at = None
        self._done = False
        self._sources = []
        self._open = []

    def start(self):
        self._armed_at = self._clock() + self._grace
        if self._devices is not None and not self._devices:
            return 0
        from gi.repository import GLib
        import evdev
        for path in (self._devices if self._devices is not None else evdev.list_devices()):
            try:
                device = evdev.InputDevice(path)
                keys = set(device.capabilities().get(EV_KEY, []))
                if not physical(device.phys, keys):
                    device.close()
                    continue
                os.set_blocking(device.fd, False)
            except (OSError, ValueError):
                continue
            self._open.append(device)
            self._sources.append(GLib.io_add_watch(device.fd, GLib.IO_IN, self._readable, device, keys))
        return len(self._open)

    def _readable(self, _fd, _condition, device, keys):
        try:
            for event in device.read():
                if self.event(event.type, event.code, event.value, keys):
                    return False
        except (OSError, BlockingIOError):
            pass
        return not self._done

    def event(self, event_type, code, value, keys):
        """True when this event made the reclaim happen."""
        if self._done or self._armed_at is None or self._clock() < self._armed_at or not is_wake(event_type, code, value, keys):
            return False
        self._done = True
        self._on()
        return True

    def stop(self):
        from gi.repository import GLib
        for source in self._sources:
            GLib.source_remove(source)
        for device in self._open:
            try:
                device.close()
            except OSError:
                pass
        self._sources, self._open = [], []
