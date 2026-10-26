---
name: delegate
description: Use when you are the lead delegating work to an agent or harvesting a delegated result - covers write leases, the brief, worker statuses, reviewer environments and verified completion.
---

# Delegate

The LEAD owns all delegation (ORCH-11). No prose can mechanically route the interactive session; the tracked workflow and hook controls are listed in `CLAUDE.md` and `HARNESS.md`.

The LEAD never approves its own consequential work. It edits directly only for trivially mechanical changes where delegation creates more concurrency risk, then gets an independent delta review.

## Before dispatch

1. **Baseline first (ORCH-04).** Freeze the before-state from git before granting a write lease: `.claude/harness/agent_harness.py provision`.
2. **One writer per file (ORCH-01).** At most one agent holds write authority over a file at a time.
3. **At most two agents, never nested (ORCH-11).** A worker never spawns agents or worktrees unless its brief allows it.
4. **Name the model.** Mechanical discovery and counting: the cheapest tier. Bounded implementation: the mid tier. Review, planning and re-derivation: the top tier. Authoring and approval occur in fresh contexts.
5. **Keep waits in the parent (ORCH-08).** Long suites, corpora, full-population counts and near-ceiling commands stay in the persistent parent session.

## The brief

One bounded outcome (ORCH-07): one question, one non-overlapping file set, commands that finish in minutes, one durable artifact. A brief states:

- **Scope:** the files the worker may touch, and the task as read_first, action, acceptance criteria and a verification command.
- **Forbidden:** what the worker must not do, such as commit, push, spawn agents or worktrees, or edit outside the file set.
- **Done when:** a command the worker can run and its expected result.
- **Report:** the fields to return, and the artifact the worker writes before returning (ORCH-09): task, status, candidate commit, findings, completeness.

The task loop builds this brief itself (`brief()` in `scripts/task_loop.py`) and reads the worker's final `STATUS:` line.

## Worker status

A worker returns one of:

| Status | Lead's action |
|---|---|
| `DONE` | Verify it (below). |
| `DONE_WITH_CONCERNS` | Read the concerns, then verify. |
| `NEEDS_CONTEXT` | Re-dispatch with the missing context. |
| `BLOCKED` | Split the task or escalate. Never retry it as-is. |

## Verify completion

Task state moves `RUNNING -> REPORTED_COMPLETE -> VERIFIED_COMPLETE` (ORCH-05); only the last releases dependent work. State is advisory (ORCH-10): a worker's report is not evidence (ORCH-02).

1. Harvest the worker's artifact.
2. Re-run the relevant check yourself and read its exit status.
3. Re-derive any reported count independently (`.claude/agents/dual-derive.md`).

A review without a verdict is invalid, and no findings is not approval.

## Reviewer environment

The LEAD owns it (ORCH-06): create a throwaway worktree, wait for the reviewer to terminate, then remove it. Commit the candidate before adversarial review and land exactly the reviewed commit (ORCH-03; see `review` and `land`).
