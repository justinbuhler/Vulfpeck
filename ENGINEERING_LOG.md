# Engineering log

Dated failures, measurements, limitations, and implementation history (AGENTS.md ownership pointers).
Newest first. Each entry: date, what happened, how it was measured, what changed.

## 2026-10-22 - mutation runner timeout hid a kill

M028 (`land.sh --expect`) was recorded as a crash-only kill, then as a survivor on a rerun, although the mutant fails three named checks when `tests/test_land.py` runs alone (98 s). Under the runner's 12 parallel workers the file exceeded the 120 s per-script timeout. A baseline timeout counted as "red at baseline", which quarantined the file, so its kills disappeared. The default timeout is now 600 s, and a baseline timeout stops the runner with the timeout to raise. The uncommitted survivor row from the misconfigured rerun was discarded; the committed crash-only row (6da00dc) stays as history, superseded by the rerun after the fix.

## 2026-10-22 - flaky gate test

`tests/test_codegraph_sync.py` failed once inside `./scripts/gate.sh` while adding the hook self-check, then passed the next full gate and 8 of 8 standalone runs. Its failing check was not captured (the gate prints only the last five lines of a failing file). It drives background `codegraph sync` processes and a lock directory with timing, which is the likely source. Unresolved; rerun the file alone before treating a failure as real, and capture its full output when it recurs.

Resolved the same day. It recurred standalone (1 run in 3) on `codegraph sync exiting 7: the failure is recorded in the log, not lost`. `wait_for` returned as soon as the background sync's log was non-empty, which can be before the hook appends its `exit 7` line. It now waits for the expected text.

## 2026-10-21 - harness made project-agnostic

`harness.toml` now defines setup, lint and test commands, untracked lander inputs, watched controls and mapped tests; `scripts/gate.sh` is the one gate for the land, CI and pre-commit.
The lander's hard-coded input provisioning was replaced by `land.copy_untracked`, and the mutation runner's `--verify-specs` mode was removed because the spec harness it needs was never part of this repository.

## 2026-10-21 - harness audit

Fixed: the mutation runner crashed with `ZeroDivisionError` on an empty corpus, wrote its ledger at the repository root while `CLAUDE.md` named `mutants/`, and missed indented `N passed` lines (199 of 488 checks counted). A `CLAUDE_VERIFY_ON_STOP` setting had no consumer and was removed.
Measured: suite and lint pass in a clean environment with no git identity and no `codegraph`; `land.sh` landed a branch end to end in a scratch clone; the runner killed M001.
