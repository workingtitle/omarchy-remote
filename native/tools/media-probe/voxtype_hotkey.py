"""Voxtype's hotkey from a paired device: Voxtype reads its key from the machine's input devices (evdev), which the
virtual keyboard of input_bridge.py is not one of. A remote press of that key therefore runs `voxtype record`
instead (toggle mode: toggle on each press; push to talk: start on press, stop on release). Nothing runs without
a Voxtype config that enables a plain key."""
import os, re, subprocess, threading

# Voxtype names keys as evdev does, without the KEY_ prefix; used when python-evdev is not importable.
FALLBACK = {'LEFTCTRL': 29, 'LEFTSHIFT': 42, 'LEFTALT': 56, 'LEFTMETA': 125, 'RIGHTCTRL': 97, 'RIGHTSHIFT': 54,
            'RIGHTALT': 100, 'RIGHTMETA': 126, 'CAPSLOCK': 58, 'SCROLLLOCK': 70, 'PAUSE': 119, 'INSERT': 110,
            'SYSRQ': 99, 'COMPOSE': 127, 'MENU': 127}
FALLBACK.update({'F%d' % n: code for n, code in zip(range(1, 11), range(59, 69))})
FALLBACK.update({'F11': 87, 'F12': 88}); FALLBACK.update({'F%d' % n: 183 + n - 13 for n in range(13, 25)})

def keycode(name):
    name = str(name).upper()
    if name.startswith('KEY_'): name = name[4:]
    try:
        import evdev
        code = evdev.ecodes.ecodes.get('KEY_' + name)
        if isinstance(code, int): return code
    except ImportError: pass
    return FALLBACK.get(name)

def config_path():
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'voxtype', 'config.toml')

def read_hotkey(path):
    """The [hotkey] table of Voxtype's config. tomllib (Python 3.11+), else a reader for the plain `name = value` lines."""
    with open(path, 'rb') as handle: data = handle.read()
    try:
        import tomllib
        return tomllib.loads(data.decode()).get('hotkey', {})
    except ImportError: pass
    table, inside = {}, False
    for line in data.decode(errors='replace').splitlines():
        line = line.split('#', 1)[0].strip()
        if line.startswith('['): inside = line == '[hotkey]'; continue
        match = re.fullmatch(r'(\w+)\s*=\s*(.+)', line)
        if not inside or not match: continue
        name, value = match.group(1), match.group(2).strip()
        if value in ('true', 'false'): table[name] = value == 'true'
        elif value.startswith('['): table[name] = re.findall(r'"([^"]*)"', value)
        else: table[name] = value.strip('"\'')
    return table

class VoxtypeHotkey:
    def __init__(self, path=None, run=None):
        self.path = path or config_path()
        self.run = run or self._run
        self.stamp = None
        self.code = None          # evdev code of the hotkey; None when Voxtype has none (or is not set up)
        self.toggle = True
        self.held = False

    @staticmethod
    def _run(action):
        def go():
            try: subprocess.run(['voxtype', 'record', action], timeout=3, check=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired): pass
        threading.Thread(target=go, daemon=True).start()

    def reload(self):
        """Re-reads the config when it changed (the user edits it while the service runs)."""
        try: stamp = os.stat(self.path).st_mtime_ns
        except OSError: stamp = None
        if stamp == self.stamp: return
        self.stamp = stamp; self.code = None; self.toggle = True
        if stamp is None: return
        try: hotkey = read_hotkey(self.path)
        except (OSError, ValueError): return
        if not isinstance(hotkey, dict) or hotkey.get('enabled', True) is False or hotkey.get('modifiers'): return
        key = hotkey.get('key')
        self.code = keycode(key) if isinstance(key, str) else None
        self.toggle = hotkey.get('mode', 'toggle') != 'push_to_talk'

    def key(self, code, down):
        """The evdev code of a key a device pressed (down) or released."""
        self.reload()
        if self.code is None or code != self.code: return
        if down:
            if self.held: return              # the machine's own repeat of a held key
            self.held = True
            self.run('toggle' if self.toggle else 'start')
        else:
            self.held = False
            if not self.toggle: self.run('stop')
