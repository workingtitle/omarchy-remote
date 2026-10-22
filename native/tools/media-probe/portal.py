"""Small ScreenCast portal experiment. Keep session and PipeWire FD alive."""
import os, secrets, sys
from pathlib import Path
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

CALL_TIMEOUT_S = 8

class ScreenCast:
    def __init__(self, auto_consent=False, persist=True, output=None):
        # persist=False: no restore token is used or saved (the remote display mode captures a fresh
        # headless output; the token would restore the physical one).
        self.persist = persist
        # auto_consent arms the omarchy-remote-picker broker (remote_picker.py) for exactly this
        # SelectSources call, so a fresh install or a reboot does not wait on a dialog nobody sees.
        self.auto_consent = auto_consent
        self.output = output   # the output the armed picker chooses (remote display / extra screen); None = focused
        os.environ.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/%d/bus' % os.getuid())
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        self.api = dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop'), 'org.freedesktop.portal.ScreenCast')
        self.session = None
        self.fd = None
        self.matches = []
        self._requests = {}
        self.token_path = Path.home()/'.local/state/omarchy-remote/portal-restore-token'
    def request(self, method, args, options, callback):
        token = 'or_' + secrets.token_hex(8)
        options['handle_token'] = token
        sender = self.bus.get_unique_name()[1:].replace('.', '_')
        path = '/org/freedesktop/portal/desktop/request/%s/%s' % (sender, token)
        def response(code, results):
            # A response and close() can race during shutdown.  Only the
            # first one owns the match and may dispatch the callback.
            if not self._finish_request(path):
                return
            print('Portal %s response: %s' % (method, code), file=sys.stderr)
            if int(code) != 0:
                self.on_error('Portal %s rejected: %s' % (method, code)); return
            try: callback(results)
            except Exception as error: self.on_error(error)
        match = self.bus.add_signal_receiver(response, signal_name='Response', dbus_interface='org.freedesktop.portal.Request', path=path)
        self.matches.append(match)
        self._requests[path] = match
        try:
            # The answer comes as a Response signal; the call itself returns at once. A portal that does not even take the call
            # (its backend spins) would hold the main loop for dbus-python's default 25 s: bound it, the guard handles the failure.
            getattr(self.api, method)(*args, dbus.Dictionary(options, signature='sv'), timeout=CALL_TIMEOUT_S)
        except Exception:
            self._finish_request(path)
            raise

    def _finish_request(self, path):
        match = self._requests.pop(path, None)
        if match is None:
            return False
        try:
            match.remove()
        except Exception:
            pass
        try:
            self.matches.remove(match)
        except ValueError:
            pass
        return True

    def _close_requests(self):
        # Request.Close is required for outstanding user-consent dialogs;
        # removing our signal match alone does not dismiss them.
        for path in list(self._requests):
            # Detach first: a bus implementation may deliver a reentrant
            # Response while Request.Close is being called.
            self._finish_request(path)
            try:
                request = dbus.Interface(
                    self.bus.get_object('org.freedesktop.portal.Desktop', path),
                    'org.freedesktop.portal.Request',
                )
                request.Close()
            except Exception:
                pass
    def _disarm(self):
        if self.auto_consent:
            import remote_picker
            remote_picker.disarm()

    def start(self, callback, on_error):
        def failed(error):
            self._disarm()
            on_error(error)
        self.on_error = failed
        def created(results):
            self.session = results['session_handle']
            options = {'types': dbus.UInt32(1), 'multiple': False, 'cursor_mode': dbus.UInt32(2), 'persist_mode': dbus.UInt32(2)}
            if self.persist and self.token_path.exists():
                with self.token_path.open() as file:
                    raw_token = file.read(4097)
                if len(raw_token) <= 4096:
                    token = raw_token.strip()
                    if token: options['restore_token'] = token
            if self.auto_consent:
                import remote_picker
                remote_picker.arm(output=self.output)
            self.request('SelectSources', [self.session], options, selected)
        def selected(_):
            self._disarm()
            self.request('Start', [self.session, ''], {}, started)
        def started(results):
            streams = results['streams']
            token = results.get('restore_token')
            if self.persist and token and len(str(token)) <= 4096:
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.token_path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as file: file.write(str(token))
            if len(streams) != 1: raise RuntimeError('Expected one selected display')
            self.fd = self.api.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature='sv')).take()
            callback(int(streams[0][0]), self.fd, dict(streams[0][1]))
        self.request('CreateSession', [], {'session_handle_token':'session_'+secrets.token_hex(8)}, created)
    def close(self):
        self._disarm()
        self._close_requests()
        if self.session:
            try:
                dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', self.session), 'org.freedesktop.portal.Session').Close()
            except dbus.DBusException: pass
            self.session = None
        if self.fd is not None:
            os.close(self.fd); self.fd = None
