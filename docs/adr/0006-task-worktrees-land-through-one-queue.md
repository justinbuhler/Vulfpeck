# 0006. Task worktrees land through one serialized queue

Status: proposed
Date: 2026-10-22
Components: .claude/harness/agent_harness.py, .claude/hooks/queue_guard.py

## Context

Parallel task agents committing in their own worktrees race on refs and each test a different tree. A task agent that commits directly skips the gate.

## Decision

`agent_harness.py` provisions one worktree per task from an explicit baseline, records a manifest, and lands queued requests one at a time through the gate. Task names are restricted to a safe character set. The lander refuses a request whose worktree, branch or baseline differ from the manifest, or whose HEAD is not the last commit landed for that task, and refuses a worktree whose index holds a staged path the request does not name; it commits only the queued paths, as literal pathspecs (`git --literal-pathspecs commit --only`); `enqueue` refuses glob patterns, magic pathspecs and directories, and the lander checks the request again, and staged renames are compared with `--no-renames` so their source is seen. `queue_guard.py` gives the early message: it denies `git commit`, `commit-tree` and `update-ref` inside a task worktree, following `-C`, `--git-dir` and `--work-tree`.

## Consequences

A direct commit is caught at land by any route, and early for the common route. `queue_guard.py` is advisory parsing; `sh -c` gets past it, and the lander's HEAD check is what enforces.
