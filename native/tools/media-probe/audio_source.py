"""Which GStreamer element captures the machine's sound, decided once per session.

The sender captures the default sink's monitor with pipewiresrc. gst-plugin-pipewire 1.6.9 answered "target not found" for every
sink on a machine without a sound card (PipeWire's own `pw-record` and PulseAudio's `pulsesrc` still worked), and the sender ended the
whole session on that error although the picture was fine. So the source is tried for a moment first: pipewiresrc, then pulsesrc
(through pipewire-pulse), and silence if neither starts. No gi import here: the probe itself lives in the sender."""

KINDS = ('pipewire', 'pulse')
SILENCE = 'none'


def monitor_device(sink):
    """pulsesrc names a sink's monitor `<sink>.monitor`."""
    return sink + '.monitor'


def choose(sink, probe):
    """First kind whose `probe(kind, sink)` says it started; SILENCE when none does (the session goes on without sound)."""
    for kind in KINDS:
        try:
            if probe(kind, sink):
                return kind
        except Exception:
            continue
    return SILENCE


def description(kind, arrival):
    """The `audio` element of the pipeline (always named audio); `arrival` = timestamps taken on arrival."""
    flag = str(bool(arrival)).lower()
    if kind == 'pipewire':
        return 'pipewiresrc name=audio do-timestamp=%s provide-clock=false' % flag
    if kind == 'pulse':
        return 'pulsesrc name=audio do-timestamp=%s provide-clock=false' % flag
    return 'audiotestsrc name=audio is-live=true wave=silence'


def apply(kind, element, sink):
    """Point the created element at `sink`; returns the property that was set (None for silence)."""
    if kind == 'pipewire':
        element.set_property('target-object', sink)
        return 'target-object'
    if kind == 'pulse':
        element.set_property('device', monitor_device(sink))
        return 'device'
    return None


PROBE_SECONDS = 1.0


def probe_arguments(kind, sink):
    """A short gst-launch run of the real source (a subprocess: a GStreamer pipeline in the sender blocks up to 30 s on this error)."""
    element = 'pipewiresrc' if kind == 'pipewire' else 'pulsesrc'
    properties = (['target-object=' + sink, 'stream-properties=props,stream.capture.sink=(boolean)true'] if kind == 'pipewire'
                  else ['device=' + monitor_device(sink)])
    return ['gst-launch-1.0', '-q', element] + properties + ['num-buffers=5', '!', 'fakesink']


def probe_started(returncode, stderr):
    """Only an error counts as failure; `returncode` None = still running when the time was up (the source works, nothing flowed yet)."""
    if 'ERROR' in (stderr or ''):
        return False
    return returncode in (None, 0)
