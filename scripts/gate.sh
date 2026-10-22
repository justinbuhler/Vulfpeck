#!/usr/bin/env bash
# The gate: setup, lint, then the suite, each from harness.toml.
#
#   ./scripts/gate.sh              setup && lint && ./scripts/run.sh --tests
#   ./scripts/gate.sh --lint-only  lint only (.githooks/pre-commit)
#
# One definition, three callers: scripts/land.sh runs it on the merged tree,
# .github/workflows/tests.yml runs it on every PR, and the pre-commit hook runs
# the lint half. They cannot drift apart because none of them spells a command.
# Each step's exit status is the gate's; a failing step stops the ones after it.
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
# The configured gate and direct suite share the same inherited OS mutex.
LOCK_TOOL="$(cd "$(dirname "$0")" && pwd)/harness_lock.py"
if ! python3 "$LOCK_TOOL" --root "$ROOT" --name verification --check-held; then
  exec python3 "$LOCK_TOOL" --root "$ROOT" --name verification -- bash "$0" "$@"
fi
. "$(dirname "$0")/harness_env.sh"
harness_load "$ROOT" || exit 1
cd "$ROOT" || exit 1

if [ "${1:-}" = --lint-only ]; then
  [ -n "$HARNESS_LINT" ] || exit 0
  exec bash -c "$HARNESS_LINT"
fi

if [ -n "$HARNESS_SETUP" ]; then bash -c "$HARNESS_SETUP" || exit; fi
if [ -n "$HARNESS_LINT" ]; then bash -c "$HARNESS_LINT" || exit; fi
HARNESS_SETUP_DONE=1 exec ./scripts/run.sh --tests
