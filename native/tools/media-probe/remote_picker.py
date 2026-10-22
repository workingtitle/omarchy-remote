#!/usr/bin/env python3
"""ScreenCast picker broker for xdg-desktop-portal-hyprland (installed as ~/.local/bin/omarchy-remote-picker).

xdph runs `custom_picker_binary` whenever an application asks to share the screen and no restore token
applies; the picker prints its choice as `[SELECTION]<flags>/screen:<output>`. On a fresh install or after a
reboot that dialog appears on the guest's own screen, where a remote user cannot answer it.

This broker answers only a request that the Omarchy Remote sender armed a moment earlier: a one-shot file in
$XDG_RUNTIME_DIR/omarchy-remote, owned by this user, mode 0600, at most MAX_AGE seconds old, consumed on use.
Every other request goes unchanged to the previous picker (Omarchy's preview picker by default). Processes of
the same user could arm it too, but they can already capture the screen through wlr-screencopy without a
dialog, so no new capability is granted; access control stays with the Remote service's authentication.
"""
import json
import os
import re
import stat
import sys
import time

MAX_AGE = 30.0   # a session that starts slowly (several devices replacing each other) must not lose its answer: the real picker dialog would open
DEFAULT_FALLBACK = 'hyprland-preview-share-picker'
_OUTPUT = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_COMMAND = re.compile(r'^(/[A-Za-z0-9_./-]{1,255}|[A-Za-z0-9_.-]{1,64})$')


def request_path(runtime_dir=None):
    runtime_dir = runtime_dir or os.environ.get('XDG_RUNTIME_DIR') or '/run/user/%d' % os.getuid()
    return os.path.join(runtime_dir, 'omarchy-remote', 'picker-request')


def arm(path=None, output=None):
    """Called by the sender right before it asks the portal to select a source; `output` names the one to
    share (the remote display's headless output), else the picker takes the focused one."""
    path = path or request_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = path + '.%d' % os.getpid()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(json.dumps({'v': 1, 'pid': os.getpid(), **({'output': output} if output else {})}))
    os.replace(temporary, path)


def disarm(path=None):
    """Called by the sender once the portal answered; a request nobody consumed must not linger."""
    try:
        os.remove(path or request_path())
    except FileNotFoundError:
        pass


def consume(path=None, now=None):
    """The request (a dict, truthy) exactly once for a fresh, private request file owned by this user, else
    None; the file is removed."""
    path = path or request_path()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        raw = os.read(descriptor, 1024) if stat.S_ISREG(info.st_mode) else b''
    finally:
        os.close(descriptor)
    try:
        os.remove(path)          # consume before deciding: a request is never used twice
    except OSError:
        return None
    now = time.time() if now is None else now
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
            and -1.0 <= now - info.st_mtime <= MAX_AGE):
        return None
    try:
        request = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        request = None
    return request if isinstance(request, dict) else {'v': 1}


def choose_output(monitors, wanted=None):
    """`wanted` when it is enabled, else the single enabled monitor, else the focused one, else the first;
    None if nothing usable."""
    try:
        data = json.loads(monitors) if isinstance(monitors, str) else monitors
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    active = [m for m in data if isinstance(m, dict) and not m.get('disabled')
              and isinstance(m.get('name'), str) and _OUTPUT.match(m['name'])]
    if not active:
        return None
    if isinstance(wanted, str) and any(m['name'] == wanted for m in active):
        return wanted
    if len(active) == 1:
        return active[0]['name']
    focused = [m for m in active if m.get('focused') is True]
    return (focused or active)[0]['name']


_HEADLESS = re.compile(r'^OmarchyRemote(-[0-9]{1,6})?$')


def sender_running(proc='/proc'):
    """True while one of our WebRTC senders runs (its command line names webrtc_sender.py)."""
    try:
        entries = os.listdir(proc)
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('%s/%s/cmdline' % (proc, entry), 'rb') as handle:
                if b'webrtc_sender.py' in handle.read(2048):
                    return True
        except OSError:
            continue
    return False


def unarmed_output(monitors, running):
    """The headless output of a remote session whose answer was lost: two sessions of one device replacing
    each other within a second consume or miss the one-shot arm, and the real picker dialog then opened on a
    screen nobody sees and blocked every later session. With a sender running and exactly one
    headless output, a request without an arm is that session's."""
    if not running:
        return None
    try:
        data = json.loads(monitors) if isinstance(monitors, str) else monitors
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    names = [m['name'] for m in data if isinstance(m, dict) and not m.get('disabled')
             and isinstance(m.get('name'), str) and _HEADLESS.match(m['name'])]
    return names[0] if len(names) == 1 else None


def sender_output(monitors, running):
    """A request that neither carries an arm nor has exactly one headless output, while one of our senders runs, is
    still that session's: mirror mode (the machine locked, or another device just took over) has no headless output.
    The real picker dialog, forwarded to instead, opens on a screen nobody sees and blocks the portal for every later
    session (once no device could connect for over an hour)."""
    return choose_output(monitors, None) if running else None


def selection(output, allow_restore=True):
    if not _OUTPUT.match(output):
        raise ValueError('invalid output')
    return '[SELECTION]%s/screen:%s' % ('r' if allow_restore else '', output)


def fallback_command(config_path=None):
    config_path = config_path or os.path.expanduser('~/.config/omarchy-remote/picker-fallback')
    try:
        with open(config_path) as handle:
            value = handle.read(300).strip()
    except OSError:
        return DEFAULT_FALLBACK
    if not _COMMAND.match(value) or os.path.basename(value) in ('omarchy-remote-picker', 'remote_picker.py'):
        return DEFAULT_FALLBACK
    return value


def note(line, path=None):
    """Keep the last 20 decisions in a private log for diagnostics; fixed wording, no request contents."""
    path = path or os.path.expanduser('~/.local/state/omarchy-remote/picker.log')
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            with open(path) as handle:
                lines = handle.read(8192).splitlines()[-19:]
        except OSError:
            lines = []
        lines.append(time.strftime('%Y-%m-%dT%H:%M:%S ') + line)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write('\n'.join(lines) + '\n')
    except OSError:
        pass


def main(argv, read_monitors=None, execvp=os.execvp, out=sys.stdout, log=note, running=sender_running):
    request = consume()
    if read_monitors is None:
        from guest_state import bounded_command, hyprland_env
        read_monitors = lambda: bounded_command(['hyprctl', '-j', 'monitors'], hyprland_env())
    output, how = None, 'armed'
    if request:
        try:
            output = choose_output(read_monitors(), request.get('output'))
        except Exception:
            output = None
    if not output:
        try:
            output, how = unarmed_output(read_monitors(), running()), 'unarmed'
        except Exception:
            output = None
    if not output:
        try:
            output, how = sender_output(read_monitors(), running()), 'unarmed'
        except Exception:
            output = None
    if output:
        # stderr lands in the portal's journal; fixed wording, no request contents.
        print('omarchy-remote-picker: answered %s request for %s' % (how, output), file=sys.stderr)
        log('answered %s%s' % (output, '' if how == 'armed' else ' (unarmed)'))
        out.write(selection(output) + '\n')
        out.flush()
        return 0
    command = fallback_command()
    print('omarchy-remote-picker: forwarding to %s' % command, file=sys.stderr)
    log('forwarded to %s' % os.path.basename(command))
    execvp(command, [command] + list(argv[1:]))
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
