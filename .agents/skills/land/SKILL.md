---
name: land
description: Use when asked to commit, land, ship, or integrate work in this repo, including a bare "commit", "retry" or "b" after generating files - and whenever a commit must be trusted in this shared checkout, where peer sessions leave uncommitted work and branch refs and index locks collide.
---

# Land

Commit on a branch; reach `main` only through `scripts/land.sh`. This repo has one
sanctioned path to local `main` (root `AGENTS.md` ORCH-12). Do not open a second one.

## Preflight - run all of these and print the output

```bash
git rev-parse --show-toplevel
git worktree list
git config --get core.bare
git status --porcelain
env | grep -E '^GIT_(DIR|INDEX_FILE|WORK_TREE)='
```

Stop and report, do not work around: `core.bare` is `true`; any of those `GIT_*`
variables is set; `HEAD` is `main`.

Being the primary checkout is **not** a reason to abort. the primary checkout (`git rev-parse --show-toplevel`
in the main worktree) is where branch work is committed; the `~/.cache/vulfpeck-land/*` worktrees belong to
`land.sh`. Do check `git status --porcelain` for files you did not touch - a peer
session's uncommitted work lives in this tree.

If `git status --porcelain` is empty, do not decline flatly. Report the absolute path
of every artifact generated this session and say which fall outside the repo root.

## Commit

1. Stage by explicit path. Never `git add -A`, `git add .`, or `git commit -a`.
2. Read `git diff --cached` before committing.
3. Put `git commit` alone in the invocation. No trailing `echo`, no `&& ...`, no
   `| tail` - a later command in the same invocation masks git's exit code and has
   hidden an index.lock race.
   No `-q`/`--quiet` either: `.claude/hooks/verify_commit_landed.py` reads the
   `[branch sha]` line git prints, and a quiet commit that landed is reported as
   not landed.
4. `.githooks/pre-commit` runs lint only (decided 2026-09-05). The suite gates the
   land, on the merged tree with untracked inputs provisioned. `--no-verify` is no
   longer the norm here: a failure at commit is a lint failure, so fix it rather than
   bypass it. If a bypass is genuinely needed, ask first, then name it and its reason
   in the commit body.

## Verify - never claim a commit from the commit command's own output

```bash
git log -1 --format='%H %s'
git status --porcelain
git rev-parse --abbrev-ref HEAD
```

HEAD must carry the new SHA, on the branch you meant, and the paths you staged must
no longer appear.

## Land, and what "landed" means

`./scripts/land.sh` gates the merged tree and moves `refs/heads/main` by
compare-and-swap. Claim a land only from its exit status, never from narration.

After an adversarial review, land exactly the commit it approved:
`./scripts/land.sh --expect <reviewed sha>` refuses if the branch tip has moved
since (a later commit, or one a reviewer made).

It contains no `git push`, so:

```bash
git merge-base --is-ancestor <sha> origin/main; echo $?
```

`0` means published. `1` is the correct, expected answer for a fresh branch commit and
for a local land - this is proof of publication only, and is never a post-commit check.
Publication happens when a pull request is merged.

## Report

SHA, branch, files changed, any bypass used and why, and the verification commands
actually run.
