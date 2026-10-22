#!/usr/bin/env python3
"""PTY for one remote terminal session (web/backend/terminal.mjs). Starts the user's login shell on a
pseudo-terminal and relays bytes. Input on stdin is framed: 1 byte type + 4 bytes big-endian length +
payload; type 0 = keyboard bytes, type 1 = resize as two big-endian uint16 (cols, rows). Terminal output goes
to stdout unframed. Exits when the shell exits or stdin closes. Never logs content.
Fd 3, when open, gets one line per change of the password-prompt state: b'S1\n' when the terminal reads a line
without echo (sudo, ssh, passwd, `read -s`), b'S0\n' when that ends. Full-screen programs switch off echo and
line mode together, so they do not count."""
import fcntl, os, pty, pwd, select, signal, struct, sys, termios

MAX_FRAME = 65536
cols, rows = (int(v) for v in (sys.argv[1:3] if len(sys.argv) >= 3 else (80, 24)))
shell = pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'
pid, master = pty.fork()
if pid == 0:
    # Optional start directory (the focused terminal's, chosen by terminal.mjs); home otherwise.
    try: os.chdir(sys.argv[3] if len(sys.argv) >= 4 else os.path.expanduser('~'))
    except OSError: os.chdir(os.path.expanduser('~'))
    env = {k: v for k, v in os.environ.items() if k not in ('PORT', 'REMOTE_HOST', 'ALLOWED_LOGIN', 'PAIRING_REQUIRED')}
    env.update(TERM='xterm-256color', COLORTERM='truecolor')
    os.execvpe(shell, [os.path.basename(shell), '-l'], env)

def resize(c, r):
    if 1 <= c <= 1000 and 1 <= r <= 1000:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', r, c, 0, 0))
resize(cols, rows)
stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
try: os.fstat(3); control = 3
except OSError: control = None
secret = False

def check_secret():
    global secret
    if control is None: return
    try: lflag = termios.tcgetattr(master)[3]
    except termios.error: return
    now = not (lflag & termios.ECHO) and bool(lflag & termios.ICANON)
    if now != secret:
        secret = now
        try: os.write(control, b'S1\n' if now else b'S0\n')
        except OSError: pass

buffer = b''
try:
    while True:
        # The timeout also catches a prompt whose echo is switched off after its text was written.
        ready, _, _ = select.select([master, stdin], [], [], 0.25)
        if not ready: check_secret(); continue
        if master in ready:
            try: data = os.read(master, 65536)
            except OSError: break
            if not data: break
            os.write(stdout, data)
            check_secret()
        if stdin in ready:
            chunk = os.read(stdin, 65536)
            if not chunk: break
            buffer += chunk
            while len(buffer) >= 5:
                kind, length = buffer[0], struct.unpack('>I', buffer[1:5])[0]
                if length > MAX_FRAME: raise SystemExit(2)
                if len(buffer) < 5 + length: break
                payload, buffer = buffer[5:5 + length], buffer[5 + length:]
                if kind == 0: os.write(master, payload)
                elif kind == 1 and length == 4: resize(*struct.unpack('>HH', payload))
finally:
    # Hang up like a closed terminal; force after 2 s so the session always ends.
    import time
    try: os.kill(pid, signal.SIGHUP)
    except ProcessLookupError: pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0]: break
        except ChildProcessError: break
        time.sleep(0.05)
    else:
        try: os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError): pass
