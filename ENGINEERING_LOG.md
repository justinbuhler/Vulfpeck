# Engineering log

Dated failures, measurements, limitations, and implementation history (AGENTS.md ownership pointers).
Newest first. Each entry: date, what happened, how it was measured, what changed.

## 2026-10-26 - consequence floor scored against 27 labelled pull requests

**Set.** `evals/consequence/` holds every merged pull request up to #37: 27 cases with the inputs `plan_tests.change()` would see.

**Labels.**
- Two model labellers rated each case: `lead`, and `second-opus`, which worked blind in a worktree from before any label existed.
- They agreed on 21 of 27 (77.8%), Cohen's kappa 0.69. Disagreements: pr-1, pr-2, pr-10, pr-21, pr-25, pr-37.
- The lead adjudicated those into `gold`, each with a reason, siding with `second-opus` on five of six.
- Gold: 6 Low, 4 Medium, 11 High, 6 Critical. The labels are model-made and not yet reviewed by the owner.

**Floor results** (`scripts/eval_consequence.py`, offline; no Jev key exists here, so Jev answered nothing):

| Floor | Exact | Under | Over |
|---|---|---|---|
| Shipped `[tiers]` | 11 of 27 | 6 | 10 |
| Shipped plus `critical = ["scripts/task_loop.py"]` (simulated) | 15 of 27 | 0 | 12 |

- **Under** cases are gold above the floor. All six are Critical and touch the task loop's unattended-merge path; `critical` was empty.
- **Over** cases are floor above gold: test-only, CI-bump and instruction-text changes rated High.

**What this means.**
- Jev can only raise a tier, so its headroom on this set is the under count: 6 with the shipped floor, 0 with one glob.
- The over cases are a cost Jev cannot remove by design.
- 27 cases are a sample. They show where the floor fails; they do not bound how often it fails.

## 2026-10-26 - verify_commit_landed blocks a heredoc that only mentions a commit

During the five-layer work a Bash call ran `python3 - <<'EOF'` whose body held a quoted Python string containing `&& git commit -m sync && git push` (bait text for the injection eval case). PostToolUse blocked it: "Commit not verified ... no commit was created". No commit was attempted. Reproduced in a scratch repository by driving the hook directly. A heredoc body with that string is blocked; `echo 'run git commit later'` is not; a real `git commit` that creates nothing is blocked, as it should be. The parser reads heredoc bodies as commands. The block is in the decision log (`verify_commit_landed block not-landed`). Not fixed on this branch (out of scope); filed as #39.

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
