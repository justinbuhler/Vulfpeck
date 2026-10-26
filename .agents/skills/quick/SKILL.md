---
name: quick
description: Use for a small contained change in one place - a typo, a flag, a one-function fix, a few edits across a few files - that needs no plan document and no delegation.
---

# Quick

One thread, inline plan, no plan file. Anything touching several components, an architectural choice, or parallel work is not quick: plan it.

## Size it first

| Size | Limit | Rigor |
|---|---|---|
| Trivial | At most 3 edits in at most 3 files, no new imports, no dependency or config file | Edit, review the diff, run lint and the mapped tests |
| Standard | Contained, about 10 edits or fewer | Show a short plan, then edit, then the Medium checks from `testing` |
| Full | Security, authority, matching or data-changing logic | Verify the plan against acceptance criteria first, then the tier's checks from `testing` |

Past 10 edits, reassess scope; past 15, stop and plan it properly. Trivial work that outgrows 3 edits becomes standard, carrying its edits forward.

## Steps

1. Read the target and the nearest `AGENTS.md`. For unfamiliar code, read its callers and tests first (3 to 5 lines of findings).
2. If requirements are ambiguous, ask one question before editing. Before the first edit, state Modifying / Not touching / Out-of-scope observations.
3. Branch off the current base (`git switch -c quick/<slug>`); never edit on `main`. Preserve unrelated changes.
4. Make the edits; count them.
5. Review the diff for unintended changes, then run the checks for the size.
6. Stage only the intended files by name. Commit as `{type}: description` (50 characters, one logical change). Verify with `git log -1 --oneline` and `git status --porcelain`. Reaching `main` goes through `land`.
7. Report: changes, checks run with results, commit or "commit skipped", branch.

Flags in the request: `--no-commit` leaves edits uncommitted and says so; `--research` reads callers and tests first even for a trivial change.
