#!/usr/bin/env bash
# Run the repository suite. This is the last step of scripts/gate.sh, which
# scripts/land.sh runs on the merged tree.
#
#   ./scripts/run.sh --tests      tests only (the gate)
#   ./scripts/run.sh              tests, then commands.pipeline if one is set
#
# The test command comes from harness.toml (commands.test). Its exit status is
# the suite's, and when tests.count_pattern is set the run must also report a
# non-zero number of checks: a command that exits 0 having run nothing is the one
# failure mode a gate must never have.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1     # repository root
mkdir -p .omc
if [ "${HARNESS_REPOSITORY_LOCKED:-0}" != "1" ]; then
  # No system ships both: flock is util-linux, lockf is BSD/macOS.
  if command -v flock >/dev/null; then LOCK="flock"
  elif command -v lockf >/dev/null; then LOCK="lockf -k"
  else echo "flock or lockf is required to serialise repository verification"; exit 1
  fi
  # shellcheck disable=SC2086  # deliberate: LOCK carries lockf's -k as a word
  exec $LOCK .omc/repository-verification.lock \
    env HARNESS_REPOSITORY_LOCKED=1 "$0" "$@"
fi

. scripts/harness_env.sh
harness_load "$PWD" || exit 1

TESTS_ONLY=0
for a in "$@"; do
  [ "$a" = "--tests" ] && TESTS_ONLY=1
done

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

if [ -n "$HARNESS_SETUP" ] && [ "${HARNESS_SETUP_DONE:-0}" != 1 ]; then
  bash -c "$HARNESS_SETUP" || { echo "setup failed: $HARNESS_SETUP"; exit 1; }
fi

step "tests: $HARNESS_TEST"
[ -n "$HARNESS_TEST" ] || { echo "commands.test is empty; nothing would be checked"; exit 1; }
log=$(mktemp "${TMPDIR:-/tmp}/harness-tests.XXXXXX")
trap 'rm -f "$log"' EXIT
bash -c "$HARNESS_TEST" 2>&1 | tee "$log"
fail=${PIPESTATUS[0]}
if [ "$fail" = 0 ] && [ -n "$HARNESS_COUNT_PATTERN" ]; then
  "${HARNESS_PY[@]}" scripts/harness_config.py --root "$PWD" --count "$log" >/dev/null || fail=1
fi
[ "$TESTS_ONLY" = 1 ] && exit "$fail"
[ "$fail" = 0 ] || { echo "tests failed — stopping"; exit 1; }

if [ -n "$HARNESS_PIPELINE" ]; then
  step "pipeline: $HARNESS_PIPELINE"
  bash -c "$HARNESS_PIPELINE" || exit
fi
exit 0
