#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
wayland-scanner client-header protocols/virtual-keyboard-unstable-v1.xml build/virtual-keyboard.h
wayland-scanner private-code protocols/virtual-keyboard-unstable-v1.xml build/virtual-keyboard.c
wayland-scanner client-header protocols/wlr-virtual-pointer-unstable-v1.xml build/virtual-pointer.h
wayland-scanner private-code protocols/wlr-virtual-pointer-unstable-v1.xml build/virtual-pointer.c
gcc -O2 -Wall -Wextra -Werror -Ibuild input-helper.c build/virtual-keyboard.c build/virtual-pointer.c -lwayland-client -lxkbcommon -lm -o build/omarchy-input-helper
