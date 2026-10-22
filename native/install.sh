#!/usr/bin/env bash
# Omarchy Remote guest installer. Run on the Linux machine you want to control, from a checkout:
#   native/install.sh             install or update (packages only when something is missing)
#   native/install.sh --fix       do only what is missing or out of date (what --check lists as FIX)
#   native/install.sh --check     the doctor: OK / FIX (with its command) / INFO per item, changes nothing;
#                                 --check --json for the same as JSON (exit 1 when something non-optional needs a FIX)
#   native/install.sh --uninstall remove the installed files and the picker broker (packages stay)
# Options: --no-packages (never call the package manager), --no-picker (keep the portal's own dialog),
#          --no-service (skip the connection service), --no-bar (skip the Omarchy bar widget),
#          --sudo-approval=on|off|ask (Face ID / Touch ID for sudo; ask is the default), --yes (take the defaults, no questions).
# Everything that needs administrator rights is listed and asked once ("root phase"), then run after one `sudo -v`. Without a
# terminal (ssh without -t, a script) nothing is asked: the rest is done, the one command that finishes it is printed, exit status 3.
# The connection service (web/) is installed as a user unit for this checkout, published to the tailnet
# with Tailscale Serve (tailnet only, never Funnel), and gets a bar widget on Omarchy 4's shell.
#
# Supported: Arch-based systems with Hyprland (Omarchy is the reference). Debian/Ubuntu and Fedora
# package lists are provided but untested. Other wlroots compositors may work (warning); GNOME and
# KDE lack the virtual keyboard/pointer protocols the input helper uses and are rejected.
# Installs into ~/.local/bin and ~/.local/src/omarchy-remote-input. Uses sudo only for the root phase (packages, Tailscale
# Serve, the optional sudo approval), never starts a stream and never opens a network listener.
set -euo pipefail
cd "$(dirname "$0")"

mode=install packages=yes picker=yes service=yes bar=yes json=no
SUDO_APPROVAL=ask ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --fix) mode=fix ;;
    --json) json=yes ;;
    --yes|-y) ASSUME_YES=1 ;;
    --sudo-approval=on|--sudo-approval=off|--sudo-approval=ask) SUDO_APPROVAL=${arg#--sudo-approval=} ;;
    --uninstall) mode=uninstall ;;
    --no-packages) packages=no ;;
    --no-picker) picker=no ;;
    --no-service) service=no ;;
    --no-bar) bar=no ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --check --json: only the JSON goes to stdout; everything else the installer says goes to stderr.
if [[ $json == yes ]]; then exec 3>&1 1>&2; fi
bin="$HOME/.local/bin"
web="$(cd .. && pwd)/web"
plugin_dir="$HOME/.config/omarchy/plugins/omarchy-remote.status"
src="$HOME/.local/src/omarchy-remote-input"
. scripts/install-lib.sh   # say, the root phase
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

runtime_files() { grep -v '^#' scripts/guest-files.txt | grep -v '^$'; }

# Over SSH the graphical session's variables are missing; take them from the user manager.
for name in WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE OMARCHY_PATH; do
  [[ -n ${!name:-} ]] || export "$name=$(systemctl --user show-environment 2>/dev/null | sed -n "s/^$name=//p")"
done
[[ -n ${OMARCHY_PATH:-} || ! -d /usr/share/omarchy ]] || export OMARCHY_PATH=/usr/share/omarchy

if [[ $mode == uninstall ]]; then
  if [[ -x "$bin/omarchy-remote-picker" || -f "$HOME/.config/omarchy-remote/xdph.conf.backup" || -f "$HOME/.config/omarchy-remote/xdph.conf.backup.absent" ]]; then
    python3 scripts/picker_setup.py remove
  fi
  while IFS= read -r file; do rm -f "$bin/$(basename "$file")"; done < <(runtime_files)
  rm -f "$bin/omarchy-input-helper"; rm -rf "$src"
  if [[ -d $plugin_dir ]]; then
    command -v omarchy >/dev/null && omarchy plugin disable omarchy-remote.status >/dev/null 2>&1 || true
    rm -rf "$plugin_dir"
  fi
  rm -f "$bin/omarchy-remote-pair" "$bin/omarchy-remote-status"
  if systemctl --user cat omarchy-remote.service >/dev/null 2>&1; then
    systemctl --user disable --now omarchy-remote.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/omarchy-remote.service"; systemctl --user daemon-reload
  fi
  say "Tailscale Serve was left as is; remove it with: tailscale serve reset"
  say "Omarchy Remote removed. Packages were left installed."
  exit 0
fi

# --- Environment -------------------------------------------------------------------------------
[[ $(uname -s) == Linux ]] || fail "Run this on the Linux machine you want to control."
[[ $EUID -ne 0 ]] || fail "Run as your desktop user, not root."
. /etc/os-release 2>/dev/null || true
family=unknown
case " ${ID:-} ${ID_LIKE:-} " in
  *" arch "*) family=arch ;;
  *" debian "*|*" ubuntu "*) family=debian ;;
  *" fedora "*) family=fedora ;;
esac

compositor=unknown
if [[ -n ${HYPRLAND_INSTANCE_SIGNATURE:-} ]] || ls "${XDG_RUNTIME_DIR:-/nonexistent}/hypr/" >/dev/null 2>&1; then compositor=hyprland
elif [[ ${XDG_CURRENT_DESKTOP:-} =~ (GNOME|KDE) ]]; then compositor=${BASH_REMATCH[1],,}
elif [[ ${XDG_CURRENT_DESKTOP:-} =~ ^(sway|river|niri|wayfire|labwc)$ ]]; then compositor=wlroots
elif command -v Hyprland >/dev/null; then compositor=hyprland
fi
case $compositor in
  hyprland) ;;
  gnome|kde) fail "$compositor is not supported yet: its compositor has no wlroots virtual keyboard/pointer protocols." ;;
  *) say "WARNING: compositor '$compositor' is untested. Capture uses the ScreenCast portal; input needs wlroots virtual keyboard/pointer support." ;;
esac
say "System: ${PRETTY_NAME:-unknown} (family $family), compositor $compositor"

# --- Packages ----------------------------------------------------------------------------------
elements=(pipewiresrc webrtcbin x264enc opusenc rtph264pay h264parse videoconvert)
missing_runtime() {
  local missing=()
  command -v gst-inspect-1.0 >/dev/null || { echo gst-inspect-1.0; return; }
  for element in "${elements[@]}"; do gst-inspect-1.0 "$element" >/dev/null 2>&1 || missing+=("$element"); done
  python3 -c 'import gi, dbus, cairo; gi.require_version("Gst", "1.0"); gi.require_version("GstWebRTC", "1.0"); from gi.repository import Gst, GstWebRTC' 2>/dev/null || missing+=(python-bindings)
  python3 -c 'import evdev' 2>/dev/null || missing+=(python-evdev)   # taking the machine back at its own keyboard (local_reclaim.py)
  command -v wayland-scanner >/dev/null || missing+=(wayland-scanner)
  command -v gcc >/dev/null || missing+=(gcc)
  command -v wtype >/dev/null || missing+=(wtype)
  [[ $service == no ]] || { command -v node >/dev/null && command -v npm >/dev/null; } || missing+=(nodejs)
  [[ $service == no ]] || command -v jq >/dev/null || missing+=(jq)
  pkg-config --exists wayland-client xkbcommon 2>/dev/null || missing+=(wayland-headers)
  [[ $compositor != hyprland ]] || systemctl --user cat xdg-desktop-portal-hyprland.service >/dev/null 2>&1 || missing+=(xdg-desktop-portal-hyprland)
  echo "${missing[@]:-}"
}

case $family in
  arch) pkgs=(pipewire wireplumber xdg-desktop-portal gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad
              gst-plugins-ugly gst-plugin-pipewire libnice python-gobject python-dbus python-cairo python-evdev gcc pkgconf wayland libxkbcommon wtype jq qrencode)
        command -v node >/dev/null || pkgs+=(nodejs npm)
        [[ $compositor != hyprland ]] || pkgs+=(xdg-desktop-portal-hyprland) ;;
  debian) pkgs=(pipewire wireplumber xdg-desktop-portal gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
                gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-pipewire gstreamer1.0-nice gir1.2-gstreamer-1.0
                gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 python3-gi python3-gi-cairo python3-dbus python3-evdev
                gcc pkg-config libwayland-dev libwayland-bin libxkbcommon-dev wtype jq qrencode nodejs npm) ;;
  fedora) pkgs=(pipewire wireplumber xdg-desktop-portal gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free
                gstreamer1-plugins-ugly gstreamer1-plugin-libnice python3-gobject python3-dbus python3-cairo python3-evdev gcc pkgconf-pkg-config
                wayland-devel libxkbcommon-devel wtype jq qrencode nodejs npm)
          say "Note: x264enc (gstreamer1-plugins-ugly) comes from RPM Fusion on Fedora." ;;
  *) pkgs=() ;;
esac
missing=$(missing_runtime)

# --- Facts: what is installed, compared with this checkout ----------------------------------------
self="$PWD/install.sh"
checkout="$(cd .. && pwd)"
version=$(git -C .. rev-parse --short HEAD 2>/dev/null || echo unknown)
envfile="$HOME/.config/omarchy-remote/environment"
host=""
if command -v tailscale >/dev/null; then
  host=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))
except Exception: pass' 2>/dev/null || true)
fi
facts=()
gather_facts() {
  local file name stale="" absent="" helper=ok serve=absent widget=na picker=na approval=na audio=unknown svc="" sinks
  while IFS= read -r file; do
    name=$(basename "$file")
    if [[ ! -e "$bin/$name" ]]; then absent+="$name "
    elif ! cmp -s "$file" "$bin/$name"; then stale+="$name "; fi
  done < <(runtime_files)
  if [[ $service == yes ]]; then
    for name in omarchy-remote-pair omarchy-remote-status; do [[ -e "$bin/$name" ]] || absent+="$name "; done
    if systemctl --user is-active --quiet omarchy-remote.service; then svc=active
    elif systemctl --user cat omarchy-remote.service >/dev/null 2>&1; then svc=inactive
    else svc=absent; fi
  fi
  if [[ ! -x "$bin/omarchy-input-helper" ]]; then helper=missing
  elif ! cmp -s agent/native/input-helper.c "$src/input-helper.c" 2>/dev/null; then helper=stale; fi
  if [[ $service == yes ]] && command -v tailscale >/dev/null; then
    if [[ -z $host ]]; then serve=nohost
    elif tailscale serve status 2>/dev/null | grep -q '127.0.0.1:8099'; then serve=published
    else serve=unpublished; fi
  fi
  if [[ $service == yes && $bar == yes ]] && command -v omarchy >/dev/null && [[ -d $HOME/.config/omarchy ]]; then
    [[ -d $plugin_dir ]] && widget=installed || widget=absent
  fi
  if [[ $compositor == hyprland ]]; then
    [[ -e "$bin/omarchy-remote-picker" ]] && picker=installed || picker=absent
  fi
  if grep -qF '# omarchy-remote-sudo-approval' /etc/pam.d/sudo 2>/dev/null; then approval=enabled
  elif [[ -r /etc/pam.d/sudo && -f "$web/scripts/omarchy-remote-sudo-approval" ]] \
       && [[ -f /usr/lib/security/pam_exec.so || -f /usr/lib64/security/pam_exec.so || -f /lib/security/pam_exec.so ]]; then approval=off; fi
  if command -v pactl >/dev/null; then
    sinks=$(pactl list short sinks 2>/dev/null | awk '{print $2}' || true)
    if [[ -z $sinks ]]; then audio=none
    elif grep -qvE '^(auto_null|omarchy_remote_output)$' <<<"$sinks"; then audio=output
    else audio=silent; fi
  fi
  facts=(--fact "system=${PRETTY_NAME:-unknown}" --fact "compositor=$compositor" --fact "missing_packages=$missing"
         --fact "missing_files=${absent% }" --fact "stale_files=${stale% }" --fact "helper=$helper" --fact "service=$svc"
         --fact "version=$version" --fact "tailscale=$serve"
         --fact "remote_host=$(grep -q '^REMOTE_HOST=' "$envfile" 2>/dev/null && echo set || echo unset)"
         --fact "widget=$widget" --fact "picker=$picker" --fact "sudo_approval=$approval" --fact "audio=$audio"
         --fact "checkout=$checkout" --fact "self=$self" --fact "web=$web")
  F_FILES=$([[ -z $stale$absent && $helper == ok ]] && echo ok || echo todo)
  F_SERVICE=$svc F_WIDGET=$widget F_SERVE=$serve F_APPROVAL=$approval F_PICKER=$picker
}
gather_facts

if [[ $mode == check ]]; then
  if [[ $json == yes ]]; then exec python3 scripts/doctor.py "${facts[@]}" --json >&3; fi
  exec python3 scripts/doctor.py "${facts[@]}"
fi

# --- Root phase: everything that needs your password, listed and asked once ------------------------
INTERACTIVE=0; [[ -t 0 ]] && INTERACTIVE=1
SELF=$self HOST=${host:-$(hostname)}
PAM_COMMAND="sudo '$web/scripts/omarchy-remote-sudo-approval' --enable"
PKG_MISSING=$missing PKG_LIST="${pkgs[*]}" PKG_CMD="" SERVICE=$service F_SERVE=$F_SERVE F_APPROVAL=$F_APPROVAL
if [[ -n $missing ]]; then
  [[ $packages == yes ]] || fail "Missing: $missing (and --no-packages was given)."
  (( ${#pkgs[@]} )) || fail "Missing: $missing. Unknown distribution: install GStreamer (base/good/bad/ugly, PipeWire, nice), PyGObject, dbus-python, pycairo, gcc and Wayland/xkbcommon headers yourself."
  say "Missing: $missing"
  yes_flag=""; [[ $ASSUME_YES == 0 ]] || yes_flag=" --noconfirm"
  case $family in
    arch) if command -v omarchy >/dev/null; then PKG_CMD="omarchy pkg add ${pkgs[*]}"   # keeps Omarchy's snapshots/migrations
          else PKG_CMD="sudo pacman -S --needed$yes_flag ${pkgs[*]}"; fi ;;
    debian) PKG_CMD="sudo apt-get install -y ${pkgs[*]}" ;;
    fedora) PKG_CMD="sudo dnf install -y ${pkgs[*]}" ;;
  esac
fi
serve_without_root() { tailscale serve --bg --yes http://127.0.0.1:8099 >/dev/null 2>&1; }
plan_root
root_phase && root_status=0 || root_status=$?

step_state() { root_has "$1" && root_state_of "$1" || echo ok; }
steps="{\"packages\":\"$(step_state packages)\",\"serve\":\"$(step_state serve)\",\"sudo_approval\":\"$([[ $F_APPROVAL == enabled ]] && echo ok || { root_has sudo-approval && root_state_of sudo-approval || echo off; })\"}"
finish() {   # the record of this run, the doctor's remedy list, the exit status
  python3 scripts/doctor.py "${facts[@]}" --write-state "$steps" 2>/dev/null || true
  (( root_status == 0 )) || { say "Setup is incomplete. See what is open: $self --check"; exit 3; }
}
if root_has packages; then
  if [[ $(root_state_of packages) != done ]]; then
    say "Packages are not installed yet: nothing else can be built. Finish with: $self --fix"
    finish
  fi
  missing=$(missing_runtime)
  [[ -z $missing ]] || fail "Still missing after installing packages: $missing"
fi
say "Packages: complete"

# What this run does: everything on install, only what is missing or out of date on --fix.
want() {
  [[ $mode == install ]] && return 0
  case $1 in
    files) [[ $F_FILES != ok ]] ;;
    service) [[ $F_SERVICE != active ]] ;;
    widget) [[ $F_WIDGET == absent ]] ;;
    picker) [[ $picker == yes && $F_PICKER == absent ]] ;;   # --fix sets the broker up where the compositor is Hyprland (--no-picker keeps the portal's dialog)
    *) return 1 ;;
  esac
}

# --- Files ------------------------------------------------------------------------------------
if want files; then
  mkdir -p "$bin" "$src/protocols"
  while IFS= read -r file; do install -m644 "$file" "$bin/"; done < <(runtime_files)
  chmod 755 "$bin/remote_picker.py"
  install -m644 agent/native/input-helper.c "$src/"; install -m755 agent/native/build.sh "$src/"
  install -m644 agent/native/protocols/*.xml "$src/protocols/"
  bash "$src/build.sh" >/dev/null
  install -m755 "$src/build/omarchy-input-helper" "$bin/omarchy-input-helper"
  say "Installed sender and input helper in $bin"
else
  say "Sender and input helper: up to date"
fi

# --- Picker broker ------------------------------------------------------------------------------
if [[ $mode == install ]] || want picker; then
  if [[ $picker == yes && $compositor == hyprland ]]; then
    python3 scripts/picker_setup.py install
  elif [[ -L "$bin/omarchy-remote-picker" ]]; then
    say "Picker broker left as installed (--no-picker)."
  else
    say "Picker broker not installed: screen sharing asks on this machine's screen the first time."
  fi
fi

# --- Connection service ---------------------------------------------------------------------------
if [[ $service == yes ]] && want service; then
  # A development checkout already has its dependencies (incl. build and test tools); leave them alone.
  [[ -d $web/node_modules/ws ]] || (cd "$web" && npm ci --omit=dev --no-audit --no-fund --no-update-notifier --loglevel=error)
  ln -sf "$web/scripts/omarchy-remote-pair" "$bin/omarchy-remote-pair"
  ln -sf "$web/scripts/omarchy-remote-status" "$bin/omarchy-remote-status"
  if [[ -n $host ]] && ! grep -q '^REMOTE_HOST=' "$envfile" 2>/dev/null; then
    mkdir -p "$(dirname "$envfile")"; umask 077; printf 'REMOTE_HOST=%s\n' "$host" >> "$envfile"
  fi
  bash "$web/scripts/install-service.sh" >/dev/null
  systemctl --user is-active --quiet omarchy-remote.service && say "Connection service running on 127.0.0.1:8099" || fail "The connection service did not start: journalctl --user -u omarchy-remote"
elif [[ $service == yes ]]; then
  say "Connection service: running"
fi
if [[ $service == yes ]]; then
  if [[ -z $host ]]; then
    say "Tailscale is not set up: the service is reachable on this machine only. Install Tailscale and run this installer again."
  elif [[ $F_SERVE == published ]] || { root_has serve && [[ $(root_state_of serve) == done ]]; } || tailscale serve status 2>/dev/null | grep -q '127.0.0.1:8099'; then
    say "Tailscale Serve: https://$host"
  fi
fi

# --- Bar widget (Omarchy 4 shell) ------------------------------------------------------------------
if [[ $service == yes && $bar == yes ]] && command -v omarchy >/dev/null && [[ -d $HOME/.config/omarchy ]] && want widget; then
  mkdir -p "$plugin_dir"
  install -m644 "$web/omarchy-plugin/manifest.json" "$web/omarchy-plugin/Widget.qml" "$plugin_dir/"
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
  if omarchy plugin enable omarchy-remote.status >/dev/null 2>&1; then say "Bar widget enabled (click it to pair a device)"
  else say "Bar widget copied; enable it with: omarchy plugin enable omarchy-remote.status"; fi
fi

# --- Summary -----------------------------------------------------------------------------------
python3 - "$bin" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import media_config
cores = media_config.physical_cores()
fps = media_config.auto_max_fps(cores)
width, height, workers = media_config.auto_video_defaults(cores)
print('Stream defaults for %d physical cores: up to %dx%d at %d FPS' % (cores, width, height, fps))
PY
if [[ -n $host ]]; then
  if [[ -f $web/frontend/index.html ]]; then say "READY. Pair a device: omarchy-remote-pair (or click the bar icon). Browser: https://$host/ · Mac client host: https://$host"
  else say "READY. Pair a device: omarchy-remote-pair (or click the bar icon). Mac app address: https://$host"; fi
else
  say "READY. Connect from the Mac client with this machine's SSH host name."
fi
finish
