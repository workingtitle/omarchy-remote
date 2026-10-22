# The root phase of install.sh: everything that needs administrator rights is collected first, shown as one list and
# asked about once, then run after a single `sudo -v`. Sourced by install.sh.
#
# Inputs (set by the caller): INTERACTIVE (1 when stdin is a terminal), ASSUME_YES (--yes), SELF (path of install.sh, for
# the repair command), HOST (name to reach this machine over ssh, for the no-terminal message).
# Steps are added with root_add; the outcome of each is kept in ROOT_STATE (done|failed|declined|pending).
ROOT_ID=() ROOT_REQ=() ROOT_TITLE=() ROOT_CMD=() ROOT_STATE=()
say() { printf '%s\n' "$*"; }

root_reset() { ROOT_ID=() ROOT_REQ=() ROOT_TITLE=() ROOT_CMD=() ROOT_STATE=(); }
# root_add ID required(yes|no) "what it does, for the list" "command (run by bash after sudo -v)"
root_add() { ROOT_ID+=("$1"); ROOT_REQ+=("$2"); ROOT_TITLE+=("$3"); ROOT_CMD+=("$4"); ROOT_STATE+=(pending); }

# Whether step ID is in the list.
root_has() { local id; for id in "${ROOT_ID[@]:-}"; do [[ $id == "$1" ]] && return 0; done; return 1; }
# How many required steps did not end up done.
root_required_open() {
  local i n=0
  for i in "${!ROOT_ID[@]}"; do [[ ${ROOT_REQ[$i]} == yes && ${ROOT_STATE[$i]} != done ]] && n=$((n + 1)); done
  echo "$n"
}
# The state of step ID (done|failed|declined|pending), empty when it is not in the list.
root_state_of() { local i; for i in "${!ROOT_ID[@]}"; do [[ ${ROOT_ID[$i]} == "$1" ]] && { echo "${ROOT_STATE[$i]}"; return; }; done; }
root_list() {
  local i
  for i in "${!ROOT_ID[@]}"; do
    printf '  %d. %s%s\n' $((i + 1)) "${ROOT_TITLE[$i]}" "$([[ ${ROOT_REQ[$i]} == no ]] && printf ' [optional]')"
  done
}

# ask "question" default(Y|N): the answer as the exit status (0 = yes). No terminal: the default.
ask() {
  local answer
  if [[ ${ASSUME_YES:-0} == 1 || ${INTERACTIVE:-0} != 1 ]]; then [[ $2 == Y ]]; return; fi
  read -r -p "$1 [$([[ $2 == Y ]] && echo Y/n || echo y/N)] " answer || answer=""
  case $answer in [yY]|[yY][eE][sS]) return 0 ;; [nN]|[nN][oO]) return 1 ;; *) [[ $2 == Y ]] ;; esac
}

# The optional step "approve sudo with Face ID / Touch ID" per --sudo-approval=on|off|ask. off never mentions or touches PAM;
# ask without a terminal adds nothing (the repair command asks later). Needs PAM_COMMAND, the enable command.
plan_sudo_approval() {
  local setting=${SUDO_APPROVAL:-ask}
  [[ $setting != off ]] || return 0
  local title="approve sudo with Face ID / Touch ID: adds one line to /etc/pam.d/sudo. sudo still asks for the password whenever no paired device answers, so this cannot lock you out. Undo: omarchy-remote-sudo-approval --disable"
  case $setting in
    on) root_add sudo-approval no "$title" "$PAM_COMMAND" ;;
    ask)
      if [[ ${ASSUME_YES:-0} == 1 ]]; then root_add sudo-approval no "$title" "$PAM_COMMAND"
      elif [[ ${INTERACTIVE:-0} == 1 ]]; then
        say "Optional: $title"
        if ask "Approve sudo with your paired device?" Y; then root_add sudo-approval no "$title" "$PAM_COMMAND"; fi
      fi ;;
  esac
}

# Which steps are needed, from what the caller found: PKG_MISSING (what is missing, empty = nothing), PKG_LIST, PKG_CMD,
# SERVICE (yes|no), F_SERVE (published|unpublished|...), serve_without_root (a function: tries it as the user, 0 = worked),
# F_APPROVAL (enabled|off|na), and plan_sudo_approval's inputs. Nothing missing, nothing in the list.
plan_root() {
  root_reset
  if [[ -n ${PKG_MISSING:-} ]]; then root_add packages yes "install packages (missing: ${PKG_MISSING}; the package manager skips what is installed)" "$PKG_CMD"; fi
  if [[ ${SERVICE:-yes} == yes && ${F_SERVE:-} == unpublished ]]; then
    # Without a password when the user may (tailscale set --operator); the list only when it needs root.
    if serve_without_root; then F_SERVE=published
    else root_add serve yes "publish the service to your tailnet with Tailscale Serve (tailnet only, never Funnel)" \
           "sudo tailscale serve --bg --yes http://127.0.0.1:8099 >/dev/null"; fi
  fi
  if [[ ${F_APPROVAL:-} == off ]]; then plan_sudo_approval; fi
}

# No terminal (over ssh without -t, from a script): nothing to ask or type. One command that does it all, in a terminal.
root_no_terminal() {
  say "Setup is incomplete: this needs your administrator password, and there is no terminal to ask for it:"
  root_list
  say "Run this once, in a terminal on this machine or from your computer:"
  say "  ssh -t ${HOST:-this-machine} '${SELF:-native/install.sh} --fix'"
}

# Runs the list. Returns 0 when all required steps are done, 3 (setup incomplete) otherwise.
root_phase() {
  (( ${#ROOT_ID[@]} )) || return 0
  local i can=0
  if [[ ${INTERACTIVE:-0} == 1 ]] || sudo -n true 2>/dev/null; then can=1; fi
  if (( ! can )); then
    root_no_terminal
    (( $(root_required_open) == 0 )) && return 0 || return 3
  fi
  say "The following needs your password, once:"
  root_list
  if ! ask "Continue?" Y; then
    for i in "${!ROOT_ID[@]}"; do ROOT_STATE[$i]=declined; done
    say "Skipped. Run ${SELF:-native/install.sh} --fix when you are ready."
    (( $(root_required_open) == 0 )) && return 0 || return 3
  fi
  if ! sudo -v; then
    for i in "${!ROOT_ID[@]}"; do ROOT_STATE[$i]=failed; done
    say "sudo did not accept the password."
    return 3
  fi
  for i in "${!ROOT_ID[@]}"; do
    if bash -c "${ROOT_CMD[$i]}"; then ROOT_STATE[$i]=done; else ROOT_STATE[$i]=failed; say "Step failed: ${ROOT_TITLE[$i]}"; fi
  done
  (( $(root_required_open) == 0 )) && return 0 || return 3
}
