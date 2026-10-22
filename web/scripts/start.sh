#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -x /usr/bin/node ]]; then
  exec /usr/bin/node backend/server.mjs
elif command -v mise >/dev/null; then
  exec mise exec node -- node backend/server.mjs
else
  exec node backend/server.mjs
fi
