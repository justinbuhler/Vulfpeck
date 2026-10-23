---
name: worktree-agent
description: Use when you are an agent dispatched into a git worktree to implement one task, or when you are setting up an isolated worktree for an agent - covers the checks that keep edits and commits out of the main checkout.
---

# Worktree agent rules

Rules for any agent working in a task worktree. This repo's worktrees live under `tmp/worktrees/<name>` and are provisioned with `.claude/harness/agent_harness.py provision`; use that when the lead has not already created one.

1. **Verify the directory.** Run `git rev-parse --show-toplevel` and `git worktree list`. The toplevel must be your task worktree, not the main checkout. If it is the main checkout, stop and report. If `git config --get core.bare` prints `true`, stop and report.
2. **Branch first.** Work on a named feature branch, never the detached or default branch. If the name is taken by another worktree, add a unique suffix. To update a branch held by another worktree, work detached from `origin/<branch>` and push with `git push origin HEAD:<branch>`.
3. **Stay inside.** Write only under your worktree. Build paths from `git rev-parse --show-toplevel`, not absolute paths from the main repo. Never switch branches in the main checkout.
4. **Stage by name.** `git add <path>` for each intended file, then `git diff --cached --stat`. Never `git add .` or `-A`.
5. **One bounded outcome.** One question, one non-overlapping file set, commands that finish in minutes, and a written artifact (status, commit sha, findings). Do not start a plan file; keep the plan inline.
6. **Use the message you are given.** Add no attribution or session-URL trailer.
7. **Check before reporting.** Run `./scripts/gate.sh` (lint and tests) for code changes. A report is not evidence; the lead re-runs the check.
8. **Read-only roles.** Reviewers read the candidate with `git diff` or `git show` and never change repository or git state.

After the merge the lead removes the worktree: `git worktree remove <path>`, then `./scripts/worktree_status.sh --prune`.
