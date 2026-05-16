#!/usr/bin/env bash
# Murano dev helper: kill anything on port 3000 then start the server.
# Matches the user rule: always run on port 3000 and kill prior tasks first.
#
# macOS / Linux. On Windows, run `murano serve --restart` directly instead.

set -euo pipefail
PORT="${MURANO_PORT:-3000}"

echo "[dev] Killing anything bound to port ${PORT}..."
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti ":${PORT}" || true)"
  if [ -n "${pids}" ]; then
    echo "${pids}" | xargs kill -9 || true
  fi
fi
if command -v pkill >/dev/null 2>&1; then
  pkill -f 'murano serve' 2>/dev/null || true
fi

sleep 0.3
echo "[dev] Starting murano serve on port ${PORT}..."
exec murano serve --port "${PORT}" "$@"
