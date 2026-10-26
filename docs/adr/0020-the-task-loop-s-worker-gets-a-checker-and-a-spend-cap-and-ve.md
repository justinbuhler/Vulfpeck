# 0020. The task loop's worker gets a checker and a spend cap, and verification runs the tier plan

Status: proposed
Date: 2026-10-26
Components: scripts/task_loop.py, scripts/harness_config.py, harness.toml

## Context

A loop needs four things: a goal with a clear done, a checker, a stop rule and a budget. ADR 0018 gave the loop its stop rules but not the other three:

- the sandboxed worker's done-when was only "the issue's change is complete; the runner verifies it outside the sandbox";
- verification ran only `commands.lint` from the base commit, so tests first ran in CI after the push;
- the worker was a bare `claude -p` with a 60-minute timeout and no dollar cap.

The Claude Code CLI pinned in the Containerfile (2.1.310) takes `--max-budget-usd <amount>` in print mode.

Mutation testing cannot be part of either check. At High, `plan_tests.py` plans `python3 mutants/runner.py --only <ids>`, which runs the whole suite once per mutant; its time grows with the number of mutants and has no bound, so it would exceed the worker's 60-minute timeout and the 60-minute verification timeout.

## Decision

**Checker.** The brief's DONE WHEN names a command the worker can run:

- sandboxed: `python3 scripts/plan_tests.py --base HEAD --no-mutation --run`. In the box, `/work` is a private git repository whose HEAD is the baseline of the tree the worker was given.
- unsandboxed: `python3 scripts/plan_tests.py --base <pinned base sha> --no-mutation --run`.

`--no-mutation` leaves the mutation command out of the plan and records `mutation: skipped by --no-mutation (review owns it)` in its `skipped` list. The brief says the worker must not commit, and that the runner re-runs the same tier's checks afterwards.

**Verification.** After the worker finishes, the runner:

1. reads `harness.toml` from the pinned base commit (`git show <sha>:harness.toml`), because the worktree's copy is the worker's to write;
2. takes the tier as `jev.floor_tier(changed paths, base [tiers])`; Jev is never asked, so a model answer cannot move it;
3. builds the commands with `plan_tests.plan(..., mutation=False)`, so the plan leaves mutation out and says so, with no filtering of commands by their text;
4. runs them, lint first, as one `bash -c` with `set -e`, each check in its own `bash -c`, in the same keyless box, or on the host when unsandboxed.

Mutation belongs to review: both the worker's checker and verification leave it out. The `verified` reason records the tier, the number of checks run and the plan's skipped-mutation line.

**Budget.** `[loop] max_usd` is a number >= 0, default 10.0, where 0 is off; bool, negative, nan and inf are refused.

- For `claude`, the worker argv is the configured command, then `--max-budget-usd <max_usd>`, then the brief.
- A `loop.workers.claude` command that already contains `--max-budget-usd` is a ConfigError, so `max_usd` is the one place to set the cap.
- Codex and gemini have no known cap flag, and none is invented. The loop prints one stderr notice per task that the cap is not enforced for that runtime.

## Consequences

- A change under `scripts/` now runs the full suite in the box before it is queued, rather than first in CI. A docs change (Low) still runs lint only.
- A High or Critical change reaches the pull request without its mutants having run. The review that High and Critical require runs them (`python3 mutants/runner.py --only <ids>`), and a task that touches `scripts/task_loop.py` is Critical and is held for review in any case (it is outside the unattended-merge allowlist).
- The unattended-merge allowlist and the review hold are unchanged.
- The full suite has not been run in the real image. The first live run in a container decides whether High tasks can pass verification there.
- The cap is per worker run and covers claude only, so a dedicated, spend-limited key remains the control for codex and gemini.
- `plan_tests.referencing_tests` reads the worktree with `rg`, which a worker-written `.ignore` can narrow at Medium. The full-suite fallback and the review hold still apply.
