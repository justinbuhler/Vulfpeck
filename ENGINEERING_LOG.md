# Engineering log

Dated failures, measurements, limitations, and implementation history (AGENTS.md ownership pointers).
Newest first. Each entry: date, what happened, how it was measured, what changed.

## 2026-10-22 - native Codex trust and linked-worktree activation

The native policy probe using the real `~/.codex` did not load the untrusted project layer.
An invocation-only project-trust override also did not activate bootstrap trust.
An isolated `CODEX_HOME` with the primary project trusted and the existing Smartsheet header-helper string inherited proved the native read-tool policy, with literal resource-guide and workspace-list request/response artifacts at `tmp/json/native-isolated-policy-proof.json`.
No authentication files or token values were copied, and production configuration was unchanged.

The isolated probe initially returned an empty `hooks/list` despite loading the worktree's ordinary project config.
Review identified the scope difference: Codex 0.163.0 loads linked-worktree hook declarations from the primary checkout, where this branch's registry is not yet installed.
A standalone disposable clone of the candidate is required to prove its hook registration before integration; branch-local configuration alone does not activate the registry in this linked worktree.

## 2026-10-22 - CodeGraph preparation and land receipt regressions

Independent review reproduced a land that integrated its candidate, then failed during the final CodeGraph refresh without printing its successful receipt.
Pre-gate preparation remains strict; only the post-integration refresh now reports a pending refresh while retaining the successful land status and receipt.
The regression fixture fails refresh only after the candidate is actually reachable from `main`.

The initial fake let `init --yes` overwrite metadata in an already initialized index, although installed CodeGraph 1.6.2 returns early in that state.
The fake now preserves that behavior.
Existing indexes with missing initialization metadata, foreign ownership or no timestamp are retained for explicit manual recovery; genuinely absent local indexes still initialize after the repository opted in, and verified local stale indexes still sync.
Focused RED/GREEN artifacts are retained under `tmp/log/land-refresh-*.log` and `tmp/log/codegraph-ownership-*.log`.

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
