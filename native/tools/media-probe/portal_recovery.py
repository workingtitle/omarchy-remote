"""A bounded wait for the screen-cast portal, with one recovery: restart its services and ask again.

xdg-desktop-portal-hyprland leaks a shared-memory buffer per capture session and eventually spins at 100% CPU: CreateSession or
Start then never answers and no session can start. The sender notices the silence, restarts the two
portal services once and begins the portal conversation again; a second silence is reported as 'portal-stuck'.
"""
import subprocess

PORTAL_SERVICES = ('xdg-desktop-portal-hyprland', 'xdg-desktop-portal')


class PortalGuard:
    """begin(arrived): starts a portal conversation that calls arrived(*result) when the stream is ready.
    restart(done): restarts the portal services, then calls done(). schedule(seconds, callback): callback returns
    False when it does not want to run again (the GLib timeout convention)."""
    def __init__(self, begin, started, stuck, restart, schedule, timeout_s=6.0, log=lambda text: None):
        self.begin, self.started, self.stuck, self.restart = begin, started, stuck, restart
        self.schedule, self.timeout_s, self.log = schedule, timeout_s, log
        self.attempt = 0
        self.arrived = False

    def run(self):
        self.attempt += 1
        attempt = self.attempt
        try:
            self.begin(self._arrive)
        except Exception as error:
            # The portal did not take the very first call (NoReply after its bounded wait): as silent as one that never answers.
            self.log('PORTAL_STUCK the portal call failed: ' + type(error).__name__)
            self._check(attempt)
            return
        self.schedule(self.timeout_s, lambda: self._check(attempt))

    def _arrive(self, *result):
        if self.arrived:
            return
        self.arrived = True
        self.started(*result)

    def _check(self, attempt):
        if self.arrived or attempt != self.attempt:
            return False
        if attempt >= 2:
            self.log('PORTAL_STUCK no answer after the restart')
            self.stuck()
            return False
        self.log('PORTAL_STUCK restarting the portal services')
        self.restart(self.run)
        return False


def restart_portal_services(run=subprocess.run):
    """Blocking (call it off the main loop): the backend first, then the frontend that talks to it."""
    try:
        run(['systemctl', '--user', 'restart', *PORTAL_SERVICES], timeout=20, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
