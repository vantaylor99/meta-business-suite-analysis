#!/usr/bin/env bash
#
# run-tess.sh — run the tess ticket runner with the Mac kept awake for the whole run.
#
# WHY: a long unattended tess run spawns agents one ticket at a time. If the Mac idle-sleeps
# mid-ticket it suspends the runner and drops the agent's API connection (= lost work, the
# "Connection closed mid-response" failures we hit before). `caffeinate <command>` runs the
# runner as its child and holds a no-sleep assertion for EXACTLY the runner's lifetime — it
# releases automatically when the runner exits (clean finish, error, or kill), so there's
# never a leftover caffeinate process to track or clean up.
#
# CAVEAT: this prevents IDLE sleep — laptop open, lid up, you walk away. It does NOT prevent
# lid-CLOSE (clamshell) sleep; that's a separate mechanism. To run with the lid shut, plug in
# and `sudo pmset -a disablesleep 1` (then `sudo pmset -a disablesleep 0` when done).
#
# USAGE: ./scripts/run-tess.sh [any tess run.mjs args]
#   ./scripts/run-tess.sh --strategy chase
#   ./scripts/run-tess.sh --strategy chase --stages backlog,plan,implement,review
#   ./scripts/run-tess.sh --dry-run
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$REPO_ROOT/tess/scripts/run.mjs"

# The runner spawns the `claude` CLI per ticket; make sure it's reachable.
command -v claude >/dev/null 2>&1 || export PATH="$HOME/bin:$PATH"

if command -v caffeinate >/dev/null 2>&1; then
  echo "[run-tess] caffeinate engaged — Mac stays awake (idle sleep) until the runner exits."
  exec caffeinate -dimsu node "$RUNNER" "$@"
else
  echo "[run-tess] caffeinate not found (non-macOS?) — running without sleep prevention."
  exec node "$RUNNER" "$@"
fi
