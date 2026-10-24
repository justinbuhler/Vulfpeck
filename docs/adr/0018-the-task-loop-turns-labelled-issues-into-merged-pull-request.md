# 0018. The task loop turns labelled issues into merged pull requests

Status: proposed
Date: 2026-10-23
Components: scripts/task_loop.py

## Context

`land.sh` stops at local `main` and nothing reads GitHub issues or publishes (HARNESS.md open gap 5), so every task needs a human to pick it, brief a worker, push and merge. An unattended loop that merges is the highest-consequence component of the harness: it must pick work only from a source the owner controls, never lower a test tier, stop on repeated failure, and merge only on green CI. Codex 0.164 loads project hooks only from the main checkout's `.codex/hooks.json`, so a Codex worker in a worktree can run with no gates.

## Decision

`scripts/task_loop.py` (High tier) runs intake, state, dispatch, verify, land, publish, reset and next. Its queue is the open GitHub issues carrying the `[loop] label` of `harness.toml`; the harness's own recommendations enter only as issues filed with that label (`file-followup`). Each issue has a state file under `<git common dir>/harness/tasks/` that moves only `queued -> running -> verified -> landed -> published`, or to `blocked` or `failed`; any other move raises, and only the owner's `reset` goes back.

The next issue is the highest `[loop] priority` label (none ranks last); a tie goes to a Jev choice over titles and labels at probability 0.6 or more, else the lowest issue number. A worker is the `[loop.workers]` command of the runtime, started in a worktree from `agent_harness.py provision` with a brief stating scope, forbidden actions, done-when and the report line `STATUS: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED`. `NEEDS_CONTEXT` and `BLOCKED` end as `blocked` and are not picked again. No `--dangerously-*` flag is ever passed: `sh()` raises on one and `harness_config` rejects one in a worker command. A Codex task whose main checkout has no fresh `.codex/hooks.json` ends `blocked` (`harness_doctor.codex_main_hooks`).

Verification is `plan_tests.py --run`, whose tier Jev may raise and never lower. The worker commits nothing: the runner queues the changed files and the harness lander commits them on the task branch. `land.sh` is not used, because it moves local `main` and `origin/main` moves only by merging a pull request (ORCH-12). The runner pushes the branch, opens the pull request (`Closes #N`, no attribution), runs `gh pr checks --watch --fail-fast`, and runs `gh pr merge --merge --delete-branch` only when every check passed. There is no `--auto`. A red check ends `failed`, comments the failing check names and leaves the pull request open.

Between tasks `loop_reset.py` runs as code and is recorded. `run` stops after 2 consecutive tasks end failed or blocked, or when one issue has failed twice. `--dry-run` prints the commands of every mutating subcommand and runs none; every external call goes through `sh()`.

## Consequences

Issues labelled `ready-for-agent` can reach `main` without a person in the loop, gated by the pull request's checks. This does not change repository settings: auto-merge stays off and `main` stays unprotected, so a green check is the only gate; branch protection (open gap 1) is the stronger control. The reset clears the `task-loop` session only: the worker's own session id is not known to the runner, so a worker's loop and budget counters are not cleared. A Codex task needs main's hooks synced first; a standalone `git clone --shared` worker is not built. The merged task's worktree and local branch are left for the owner to remove. A repository with no checks reports `gh pr checks` failure and every task ends `failed`. The loop is not run against the real repository by this change.
