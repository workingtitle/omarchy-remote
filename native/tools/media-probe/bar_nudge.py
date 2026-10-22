"""The Omarchy shell recreates the bar about 0.4 s after every mode change of the streamed output, and the new bar shows over a
full-screen window (a phone turned in a full-screen app). Once the new bar is there, the window's full-screen state is renewed:
Hyprland's internal state goes off and on again, the client's state is left alone (an app showing a video in full screen
is not told). Best effort: it never raises and never leaves the window without its state."""
import json
import sys

BAR = 'omarchy-bar'
FULLSCREEN = 2


def bar_addresses(layers_json, output):
    """Addresses of the bar's layer surfaces on `output` (`hyprctl -j layers`)."""
    try:
        levels = json.loads(layers_json).get(output, {}).get('levels', {})
        return {layer['address'] for layer_list in levels.values() for layer in layer_list if layer.get('namespace') == BAR}
    except (ValueError, AttributeError, TypeError, KeyError):
        return set()


def fullscreen_window(monitors_json, clients_json, output):
    """The full-screen window on the active workspace of `output` as (address, client state), or None."""
    try:
        workspace = next(m['activeWorkspace']['id'] for m in json.loads(monitors_json) if m.get('name') == output)
        for client in json.loads(clients_json):
            if client.get('workspace', {}).get('id') == workspace and client.get('fullscreen') == FULLSCREEN:
                return client['address'], int(client.get('fullscreenClient', FULLSCREEN))
    except (StopIteration, ValueError, AttributeError, TypeError, KeyError):
        pass
    return None


class BarNudge:
    """`hyprctl(args)` returns hyprctl's output (raises on failure); `output()` is the streamed output's name; `schedule(ms, fn)`
    runs `fn` once after `ms` and returns a handle for `cancel`. Call `display_changed()` after every applied mode change."""
    def __init__(self, hyprctl, output, schedule, cancel, poll=200, watch=3000, settle=300, pause=250, log=None):
        self._hyprctl, self._output, self._schedule, self._cancel = hyprctl, output, schedule, cancel
        self._poll, self._polls, self._settle, self._pause = poll, max(1, watch // poll), settle, pause
        self._log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self._timer = None
        self._seen = set()
        self._left = 0
        self._window = None            # set while the internal state is off, until it is back on

    def display_changed(self):
        self._stop()
        self._seen = self._bars()
        self._left = self._polls
        self._timer = self._schedule(self._poll, self._watch)

    def end(self):
        self._stop()

    def _stop(self):
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
        if self._window:               # never leave the window without its state
            self._on()

    def _bars(self):
        try:
            return bar_addresses(self._hyprctl(['-j', 'layers']), self._output())
        except Exception:
            return set()

    def _watch(self):
        self._timer = None
        if self._bars() - self._seen:                  # a new bar surface: let it settle, then renew the state
            self._timer = self._schedule(self._settle, self._off)
            return
        self._left -= 1
        if self._left > 0:
            self._timer = self._schedule(self._poll, self._watch)

    def _off(self):
        self._timer = None
        try:
            found = fullscreen_window(self._hyprctl(['-j', 'monitors']), self._hyprctl(['-j', 'clients']), self._output())
            if not found:
                return
            address, client = found
            self._dispatch('hl.dsp.focus({ window = "address:%s" })' % address)
            active = json.loads(self._hyprctl(['-j', 'activewindow'])).get('address')
            if active != address:
                self._log('BAR_NUDGE skipped focus=%s' % ('lost' if active else 'none'))
                return
            self._window = found
            self._dispatch('hl.dsp.window.fullscreen_state({ internal = 0, client = %d })' % client)
            self._timer = self._schedule(self._pause, self._on)
        except Exception as error:
            self._window = None
            self._log('BAR_NUDGE failed ' + type(error).__name__)

    def _on(self):
        self._timer = None
        if not self._window:
            return
        address, client = self._window
        self._window = None
        try:
            self._dispatch('hl.dsp.window.fullscreen_state({ internal = %d, client = %d })' % (FULLSCREEN, client))
            self._log('BAR_NUDGE renewed')
        except Exception as error:
            self._log('BAR_NUDGE failed ' + type(error).__name__)
            try:
                self._dispatch('hl.dsp.window.fullscreen({ mode = "fullscreen" })')   # the window must not stay without it
            except Exception:
                pass

    def _dispatch(self, lua):
        output = self._hyprctl(['dispatch', lua])
        if 'ok' not in output.lower() or 'error' in output.lower():
            raise RuntimeError('dispatch')
