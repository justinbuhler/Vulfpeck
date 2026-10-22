#!/usr/bin/env bash
# The default test command (harness.toml commands.test): run every standalone
# test script and total their checks.
#
# Tests are standalone scripts, not pytest (tests/AGENTS.md). Each prints one line
# per check and ends with "<n> passed"; a file that exits 0 without a pass count
# is FAILED, because a green line from a run that checked nothing is the one
# failure mode a gate must never have. The closing "<n> checks total" is what the
# default tests.count_pattern reads.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$PWD:$PWD/scripts${PYTHONPATH:+:$PYTHONPATH}"
PY="uv run --no-sync python3"
# Tests run real hooks from inside this checkout; keep their decisions out of
# the real record (scripts/harness_events.py). A test that checks logging sets
# its own path.
events_dir=$(mktemp -d "${TMPDIR:-/tmp}/harness-test-events.XXXXXX")
trap 'rm -rf "$events_dir"' EXIT
export HARNESS_EVENTS_LOG="${HARNESS_EVENTS_LOG:-$events_dir/events.jsonl}"

fail=0
checks=0
for t in tests/test_*.py */scripts/tests/test_*.py; do
  [ -f "$t" ] || continue
  if out=$($PY "$t" 2>&1); then
    n=$(echo "$out" | grep -oE '[0-9]+ passed' | tail -1 | grep -oE '[0-9]+')
    if [ -z "$n" ]; then
      printf '  %-50s FAILED (no pass count)\n' "$t"; fail=1
    else
      printf '  %-50s %s passed\n' "$t" "$n"
      checks=$((checks + n))
    fi
  else
    printf '  %-50s FAILED\n' "$t"; echo "$out" | tail -5; fail=1
  fi
done
echo "  ${checks} checks total"
exit $fail
