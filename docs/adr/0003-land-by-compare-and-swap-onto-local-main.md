# 0003. Land by compare-and-swap onto local main

Status: proposed
Date: 2026-10-22
Components: scripts/land.sh, scripts/worktree_status.sh, .githooks/pre-merge-commit, .githooks/post-merge, .claude/skills/land/SKILL.md, .claude/hooks/verify_commit_landed.py

## Context

Several sessions share one checkout. Peers leave uncommitted work, move branch refs and collide on index.lock. A merge that runs the suite on the branch, not on the merged tree, can land a combination nobody tested.

## Decision

`scripts/land.sh` builds the merged tree in a separate lander worktree, runs the gate on exactly that tree, and moves local `main` with a compare-and-swap `update-ref`, retrying when `main` moved meanwhile. Commits are refused on `main` (pre-commit) and merge commits on `main` are refused (pre-merge-commit). `verify_commit_landed.py` records HEAD and the HEAD reflog length before a `git commit` and blocks when neither moved; a printed `[branch sha]` must equal the new HEAD, a failure marker with no sha line blocks even when HEAD moved, and with no recorded baseline the output must hold a `[branch sha]` equal to HEAD. Only paths listed in `land.copy_untracked` are copied into the lander. `--expect <sha>` refuses to land unless the branch tip is exactly the reviewed commit.

## Consequences

Local `main` only ever holds trees that passed the gate. Landing to `origin/main` is a separate publish step.
