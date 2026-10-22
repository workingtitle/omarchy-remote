"""Session liveness, independent of the one-second input lease and SSH TCP state."""
import time

class SessionWatchdog:
    HEARTBEAT_TIMEOUT = 15.0
    DISCONNECTED_GRACE = 10.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last_heartbeat = clock()
        self.last_sequence = -1
        self.disconnected_since = None
        self.reason = None

    def heartbeat(self, message):
        sequence = message.get('seq')
        if (message.get('type') != 'heartbeat' or type(message.get('v')) is not int or
                message['v'] != 1 or type(sequence) is not int or
                not 0 <= sequence <= 9007199254740991):
            raise ValueError('Invalid session heartbeat')
        if sequence > self.last_sequence and self.reason is None:
            self.last_sequence = sequence
            self.last_heartbeat = self.clock()

    def alive(self):
        """Proof of life from another channel (the input data channel): the client reaches us and its app runs,
        whatever the signaling socket does."""
        if self.reason is None:
            self.last_heartbeat = self.clock()

    def check(self, connection='new', ice='new'):
        if self.reason is not None:
            return self.reason
        now = self.clock()
        if connection in ('failed', 'closed') or ice in ('failed', 'closed'):
            self.reason = 'peer-failed'
        elif now - self.last_heartbeat >= self.HEARTBEAT_TIMEOUT:
            self.reason = 'signaling-heartbeat-timeout'
        else:
            if 'disconnected' in (connection, ice):
                if self.disconnected_since is None:
                    self.disconnected_since = now
                if now - self.disconnected_since >= self.DISCONNECTED_GRACE:
                    self.reason = 'peer-disconnected-timeout'
            else:
                self.disconnected_since = None
        return self.reason
