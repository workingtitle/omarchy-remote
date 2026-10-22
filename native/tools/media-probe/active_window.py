#!/usr/bin/env python3
"""The machine's focused window for the clients' status strip (iOS, beside the Dynamic Island): its app
class and title, sent over the authenticated WebRTC display channel only when they change, never logged.

  {"v":1,"type":"window","app":A,"title":T}      guest -> client

A is Hyprland's window class (letters, digits, . _ -; at most 64 characters), T its title without control
characters (at most 120 characters); both are "" when no window has the focus."""
import json
import re

MAX_APP = 64
MAX_TITLE = 120
_APP = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f  ‎‏‪-‮⁦-⁩]')


def window_message(activewindow_json):
    """The message for `hyprctl -j activewindow` output; an empty window when there is none or it is unusable."""
    try:
        data = json.loads(activewindow_json) if isinstance(activewindow_json, str) else activewindow_json
    except ValueError:
        data = None
    app = title = ''
    if isinstance(data, dict):
        value = data.get('class')
        if isinstance(value, str) and _APP.match(value):
            app = value
        value = data.get('title')
        if isinstance(value, str):
            title = ' '.join(_CONTROL.sub(' ', value).split())[:MAX_TITLE]
    return json.dumps({'v': 1, 'type': 'window', 'app': app, 'title': title}, ensure_ascii=False)
