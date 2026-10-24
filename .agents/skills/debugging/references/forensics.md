# Forensics: failed or stuck agent sessions

Tool errors (a lint failure) are harness level. Forensics looks for workflow-level patterns, such as an agent editing one file five times without progress.

## Gather

1. Target: explicit branch, else current branch.
2. Plan or task file, if any. Three of the five detectors work without one.
3. `git log main..HEAD --name-only --format="COMMIT %H %ai %s"` (latest 50 commits if long). Note file change frequency, retry or fix wording, and how many commit messages repeat.
4. `git status --short` and `git worktree list --porcelain` (look for `prunable`).

## Detect

Run every detector; anomalies correlate. Give each finding a confidence (High, Medium, Low).

| Detector | Signal | Confidence cue |
|---|---|---|
| Stuck loop | Same file in 3+ commits, repeated "fix" messages, same error across attempts | High at 4+ with near-identical messages |
| Missing artifacts | Planned output file or report absent | High if the phase is marked done |
| Abandoned work | Dirty tree or unmerged branch with no recent commits | Medium without a plan |
| Scope drift | Changed files outside the task's declared files | High when unrelated directories changed |
| Crash or interruption | Orphaned worktree, lock files, uncommitted partial edits | High with a stale worktree |

## Report

Scrub secrets (`sk-`, `ghp_`, `token=`, `password=`, bearer strings) and replace home paths with `~/`. Give an anomaly table (High first), a root-cause chain that is specific and testable, and advisory remediation:

| Anomaly | Typical fix |
|---|---|
| Stuck loop | Find the loop's root cause, fix by hand, resume from the last good phase |
| Missing artifacts | Re-run the failed phase |
| Abandoned work | Resume from the last completed phase |
| Scope drift | Review and revert unrelated changes |
| Crash | Preserve uncommitted changes, report the orphaned worktree, do not clean it |

No git history means "insufficient evidence".
