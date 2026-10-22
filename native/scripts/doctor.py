#!/usr/bin/env python3
"""The installer's doctor: turns the facts install.sh collected into lines (OK, FIX with its command, INFO) or JSON, and
keeps ~/.local/state/omarchy-remote/install.json (what the last install did).

  doctor.py --fact key=value ... [--json]          report (exit 1 when something non-optional needs a FIX)
  doctor.py --fact key=value ... --write-state STEPS_JSON   record an install

Facts (all strings; an empty value means none): system, compositor, missing_packages, missing_files, stale_files, helper
(ok|missing|stale), service (active|inactive|absent), version, tailscale (absent|nohost|published|unpublished),
remote_host (set|unset), widget (installed|absent|na), picker (installed|absent|na), sudo_approval (enabled|off|na),
audio (output|silent|none|unknown), checkout, self (install.sh), web (the service's directory).
"""
import json, os, sys, time

STATE = os.path.join(os.path.expanduser('~'), '.local/state/omarchy-remote/install.json')


def items(f):
    """[(id, status, title, detail, fix, root, optional)] from the facts."""
    out = []
    fix_all = f.get('self', 'native/install.sh') + ' --fix'

    def add(id, status, title, detail='', fix='', root=False, optional=False):
        out.append({'id': id, 'status': status, 'title': title, 'detail': detail, 'fix': fix, 'root': root, 'optional': optional})

    missing = f.get('missing_packages', '').split()
    if missing:
        add('packages', 'FIX', 'Packages are missing', ', '.join(missing), fix_all, root=True)
    else:
        add('packages', 'OK', 'Packages are complete')

    helper = f.get('helper', 'missing')
    files = f.get('missing_files', '').split() + f.get('stale_files', '').split()
    if helper != 'ok' or files:
        parts = (['input helper ' + helper] if helper != 'ok' else []) + files
        stale_only = helper != 'missing' and not f.get('missing_files', '').split()
        shown = ', '.join(parts[:4]) + (' and %d more' % (len(parts) - 4) if len(parts) > 4 else '')
        add('files', 'FIX', 'Installed sender is %s' % ('out of date' if stale_only else 'incomplete'), shown, fix_all)
    else:
        add('files', 'OK', 'Sender and input helper match this checkout')

    service = f.get('service', 'absent')
    if service == 'active':
        add('service', 'OK', 'Connection service is running')
    elif service == 'inactive':
        add('service', 'FIX', 'Connection service is not running', '', 'systemctl --user restart omarchy-remote.service')
    elif service == 'absent':
        add('service', 'FIX', 'Connection service is not installed', '', fix_all)

    tailscale = f.get('tailscale', 'absent')
    host = f.get('remote_host', 'unset')
    if tailscale == 'published':
        add('serve', 'OK', 'Published to the tailnet with Tailscale Serve')
    elif tailscale == 'unpublished':
        add('serve', 'FIX', 'Not published to the tailnet (devices cannot reach it)', '',
            'sudo tailscale serve --bg --yes http://127.0.0.1:8099', root=True)
    elif tailscale == 'nohost':
        add('serve', 'INFO', 'Tailscale is installed but not connected', 'the service is reachable on this machine only')
    elif tailscale == 'absent':
        add('serve', 'INFO', 'Tailscale is not installed', 'the service is reachable on this machine only')
    if tailscale in ('published', 'unpublished') and host != 'set':
        add('remote_host', 'FIX', 'REMOTE_HOST is not set in ~/.config/omarchy-remote/environment', '', fix_all)

    widget = f.get('widget', 'na')
    if widget == 'installed':
        add('widget', 'OK', 'Bar widget is installed')
    elif widget == 'absent':
        add('widget', 'FIX', 'Bar widget is not installed', '', 'omarchy plugin enable omarchy-remote.status')

    picker = f.get('picker', 'na')
    if picker == 'installed':
        add('picker', 'INFO', 'Picker broker is installed', 'screen sharing starts without a dialog on this machine')
    elif picker == 'absent':
        add('picker', 'INFO', 'Picker broker is not installed', 'screen sharing asks on this machine\'s screen the first time')

    approval = f.get('sudo_approval', 'na')
    if approval == 'enabled':
        add('sudo-approval', 'OK', 'sudo can be approved with Face ID / Touch ID')
    elif approval == 'off':
        add('sudo-approval', 'FIX', 'Face ID / Touch ID for sudo is off', 'sudo asks for the password',
            'sudo %s/scripts/omarchy-remote-sudo-approval --enable' % f.get('web', 'web'), root=True, optional=True)

    audio = f.get('audio', 'unknown')
    if audio == 'silent':
        add('audio', 'INFO', 'No audio output on this machine', 'the silent output of the virtual microphone is used')
    elif audio == 'none':
        add('audio', 'INFO', 'No audio device found')
    elif audio == 'output':
        add('audio', 'OK', 'An audio output exists')

    state = read_state()
    if state:
        add('install', 'INFO', 'Last install: %s (%s)' % (state.get('date', '?'), state.get('version', '?')),
            'steps: ' + ', '.join('%s=%s' % kv for kv in sorted((state.get('steps') or {}).items())))
    else:
        add('install', 'INFO', 'No record of an install yet', 'written when install.sh finishes')
    return out


def read_state():
    try:
        with open(STATE) as handle: return json.load(handle)
    except (OSError, ValueError): return None


def write_state(f, steps, complete):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    state = {'version': f.get('version', 'unknown'), 'date': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'checkout': f.get('checkout', ''),
             'status': 'complete' if complete else 'incomplete', 'steps': steps}
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as handle: json.dump(state, handle, indent=2, sort_keys=True); handle.write('\n')
    os.replace(tmp, STATE)


def render(entries):
    width = max(len(e['status']) for e in entries)
    lines = []
    for e in entries:
        line = '%-*s  %s' % (width, e['status'], e['title'])
        if e['detail']: line += ' (%s)' % e['detail']
        lines.append(line)
        if e['status'] == 'FIX' and e['fix']:
            lines.append('%s  -> %s%s' % (' ' * width, e['fix'], '   [optional]' if e['optional'] else ''))
    return '\n'.join(lines)


def main(argv):
    facts, as_json, write = {}, False, None
    args = iter(argv)
    for arg in args:
        if arg == '--fact':
            key, _, value = next(args).partition('=')
            facts[key] = value
        elif arg == '--json': as_json = True
        elif arg == '--write-state': write = next(args)
        else: sys.exit('usage: doctor.py --fact key=value ... [--json | --write-state STEPS_JSON]')
    if write is not None:
        steps = json.loads(write)
        write_state(facts, steps, complete=not any(v in ('failed', 'declined', 'pending') for v in steps.values()))
        return 0
    entries = items(facts)
    needs = [e for e in entries if e['status'] == 'FIX' and not e['optional']]
    if as_json:
        print(json.dumps({'v': 1, 'ok': not needs, 'version': facts.get('version', 'unknown'), 'checkout': facts.get('checkout', ''),
                          'system': facts.get('system', ''), 'installed': read_state(), 'items': entries}, indent=2))
    else:
        print(render(entries))
    return 1 if needs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
