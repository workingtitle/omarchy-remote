#!/usr/bin/env python3
"""Attended GTK target for the native focus-release acceptance test."""
import argparse, json, os, select, signal, subprocess, sys, time, importlib.util
from pathlib import Path

def command_output(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fd = process.stdout.fileno(); os.set_blocking(fd, False); data = bytearray(); deadline = time.monotonic() + 2
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise RuntimeError("bounded command timed out")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready: raise RuntimeError("bounded command timed out")
            chunk = os.read(fd, 65536)
            if not chunk: break
            data.extend(chunk)
            if len(data) > 65536: raise RuntimeError("bounded command output exceeded 64 KiB")
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0: raise RuntimeError("bounded command failed")
    except (RuntimeError, subprocess.TimeoutExpired):
        process.kill(); process.wait(); raise
    return bytes(data).decode(errors="strict")

parser = argparse.ArgumentParser(); parser.add_argument("--seconds", type=int, default=20); args = parser.parse_args()
if not 5 <= args.seconds <= 30: parser.error("--seconds must be between 5 and 30")
for line in command_output(["systemctl", "--user", "show-environment"]).splitlines():
    key, separator, value = line.partition("=")
    if separator and key in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"): os.environ[key] = value
os.environ["GDK_BACKEND"] = "wayland"
from guest_state import focus_state as compositor_focus
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
probe_path = Path(__file__).with_name("focus_release_probe.py")
spec = importlib.util.spec_from_file_location("focus_release_probe", probe_path)
probe_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe_module)

ALLOWED = (50, 133); held = set(); errors = []; probe = probe_module.FocusReleaseProbe(); down_stamps = {}
down_count = up_count = 0; started = time.monotonic_ns(); stop_requested = False
focus_state = None; target_ready = False

def fail(reason):
    if not errors:
        errors.append(reason)
        print("REJECT " + reason, flush=True)
def on_key_press(_widget, event):
    global down_count
    code = int(event.hardware_keycode); now = time.monotonic_ns()
    if code not in ALLOWED: fail("unexpected-keycode")
    elif code in held: fail("repeated-keydown")
    elif not probe.observe(code, True, now): fail(probe.first_error or "unexpected-transition")
    else:
        down_stamps[code] = now; held.add(code); down_count += 1
        print("GUEST KEY_DOWN code=%d uptime_ns=%d" % (code, now), flush=True)
    return True
def on_key_release(_widget, event):
    global up_count
    code = int(event.hardware_keycode); now = time.monotonic_ns()
    if code not in ALLOWED: fail("unexpected-keycode")
    elif not probe.observe(code, False, now): fail(probe.first_error or "unexpected-transition")
    else:
        held.remove(code); up_count += 1
        print("GUEST KEY_UP code=%d uptime_ns=%d hold_ms=%d" % (code, now, (now - down_stamps.pop(code, now)) // 1_000_000), flush=True)
    return True
def current_focus():
    try: return compositor_focus(command_output(["hyprctl", "activewindow", "-j"]), command_output(["hyprctl", "-j", "monitors"]), os.getpid())
    except (OSError, RuntimeError): return "unknown"

window = Gtk.Window(title="Omarchy Remote Focus Release Target"); window.set_default_size(720, 480)
area = Gtk.DrawingArea(); window.add(area); window.fullscreen()
window.connect("key-press-event", on_key_press); window.connect("key-release-event", on_key_release)
window.connect("destroy", lambda *_: Gtk.main_quit())
keepalive_tick = 0
def draw(_w, cr):
    global keepalive_tick
    keepalive_tick += 1; shade = 0.08 if keepalive_tick % 2 else 0.0
    cr.set_source_rgb(0, 0, 0); cr.paint(); cr.set_source_rgb(shade, shade, shade); cr.rectangle(0, 0, 16, 16); cr.fill()
    return False
area.connect("draw", draw)
def focus_tick():
    global focus_state, target_ready
    state = current_focus()
    if state != focus_state:
        focus_state = state
        print("FOCUS " + state, flush=True)
        if state == "ok":
            if not target_ready: target_ready = True; print("TARGET_READY", flush=True)
    return not stop_requested
def keepalive(): area.queue_draw(); return not stop_requested
def stop(_signum=None, _frame=None):
    global stop_requested
    stop_requested = True; window.destroy()
signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
window.show_all(); window.present(); GLib.timeout_add(1000, focus_tick); GLib.timeout_add(33, keepalive)
GLib.timeout_add_seconds(args.seconds, stop); Gtk.main()
passed = target_ready and probe.complete and not errors and not held and down_count == 2 and up_count == 2
print("FOCUS_RELEASE_TARGET %s down=%d up=%d held=%d errors=%d" % ("PASS" if passed else "FAIL", down_count, up_count, len(held), len(errors)), flush=True)

sys.exit(0 if passed else 1)
