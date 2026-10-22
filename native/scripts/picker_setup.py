#!/usr/bin/env python3
"""Install (or with `remove`) the ScreenCast picker broker for xdg-desktop-portal-hyprland.

Runs on the guest. Used by install.sh and, over SSH, by scripts/install-remote-picker.sh.
Changes only ~/.config/hypr/xdph.conf (backed up once to ~/.config/omarchy-remote/xdph.conf.backup)
and restarts xdg-desktop-portal-hyprland so it reads the new picker; running screen shares end.
Usage: picker_setup.py install|remove
"""
import os, re, shutil, subprocess, sys
mode = sys.argv[1] if len(sys.argv) > 1 else 'install'
if mode not in ('install', 'remove'):
    sys.exit('usage: picker_setup.py install|remove')
home = os.path.expanduser('~')
bin_dir = os.path.join(home, '.local/bin')
module = os.path.join(bin_dir, 'remote_picker.py')
wrapper = os.path.join(bin_dir, 'omarchy-remote-picker')
conf = os.path.join(home, '.config/hypr/xdph.conf')
state = os.path.join(home, '.config/omarchy-remote')
backup = os.path.join(state, 'xdph.conf.backup')
absent = backup + '.absent'
fallback = os.path.join(state, 'picker-fallback')

def restart_portal():
    subprocess.run(['systemctl', '--user', 'restart', 'xdg-desktop-portal-hyprland.service'], check=True, timeout=20)
    active = subprocess.run(['systemctl', '--user', 'is-active', 'xdg-desktop-portal-hyprland.service'],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    print('PORTAL', active)

if mode == 'remove':
    if os.path.exists(backup):
        shutil.copy2(backup, conf); os.remove(backup)
    elif os.path.exists(absent):
        if os.path.exists(conf): os.remove(conf)
    for path in (absent, fallback, wrapper):
        if os.path.lexists(path): os.remove(path)
    restart_portal(); print('REMOVED'); sys.exit(0)

if not os.path.exists(module):
    sys.exit('remote_picker.py missing: run install.sh or scripts/deploy-webrtc-experiment.sh first')
os.chmod(module, 0o755)
if os.path.lexists(wrapper): os.remove(wrapper)
os.symlink(module, wrapper)
os.makedirs(state, mode=0o700, exist_ok=True)
text = open(conf).read() if os.path.exists(conf) else ''
if not os.path.exists(backup) and not os.path.exists(absent):
    if text: shutil.copy2(conf, backup)
    else: open(absent, 'w').close()
line = re.compile(r'(?m)^(\s*)custom_picker_binary\s*=\s*(\S+)\s*$')
match = line.search(text)
previous = match.group(2) if match else 'hyprland-preview-share-picker'
if os.path.basename(previous) != 'omarchy-remote-picker':
    descriptor = os.open(fallback, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle: handle.write(previous + '\n')
if match:
    text = line.sub(lambda m: '%scustom_picker_binary = %s' % (m.group(1), wrapper), text, count=1)
elif re.search(r'(?m)^\s*screencopy\s*\{', text):
    text = re.sub(r'(?m)^(\s*screencopy\s*\{)', r'\1\n    custom_picker_binary = ' + wrapper, text, count=1)
else:
    text += '\nscreencopy {\n    allow_token_by_default = true\n    custom_picker_binary = %s\n}\n' % wrapper
os.makedirs(os.path.dirname(conf), exist_ok=True)
with open(conf, 'w') as handle: handle.write(text)
restart_portal()
print('INSTALLED fallback=%s' % open(fallback).read().strip())
