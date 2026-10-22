"""Pure oracle for the focus-release guest acknowledgement sequence."""
class FocusReleaseProbe:
    sequence = ((50, True), (50, False), (133, True), (133, False))
    def __init__(self, max_hold_ns=1_000_000_000):
        self.max_hold_ns=max_hold_ns; self.index=0; self.down_at={}; self.first_error=None
    def _fail(self, reason):
        if self.first_error is None: self.first_error=reason
        return False
    def observe(self, code, down, now_ns):
        if self.first_error is not None or self.index >= len(self.sequence) or (code,down)!=self.sequence[self.index]:
            return self._fail("unexpected-transition")
        if down: self.down_at[code]=now_ns
        else:
            started=self.down_at.pop(code,None)
            if started is None: return self._fail("unmatched-keyup")
            if now_ns < started or now_ns-started > self.max_hold_ns: return self._fail("hold-exceeded-1s")
        self.index+=1; return True
    @property
    def complete(self): return self.index == len(self.sequence) and not self.down_at and self.first_error is None
