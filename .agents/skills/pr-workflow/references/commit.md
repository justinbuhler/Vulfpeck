# Commit and review-reply detail

## Stage

1. `git status --porcelain`, then `git diff` for each file you intend to stage.
2. Stage by path. Group files that serve one logical change; split unrelated changes into separate commits.
3. Never stage secrets (`.env`, `*.pem`, `*.key`), capture files (`*.har`), generated output, or files over 10 MB.
4. `git diff --cached --stat` must show exactly the intended files.

## Message

`{type}: description`, imperative, at most 50 characters, no trailing period. Add a body only when the why is not obvious, wrapped at 72.

Banned vocabulary in commit messages and docstrings: consolidate, modernize, streamline, flexible, delve, establish, enhanced, comprehensive, optimize.

Weak: `fix: update stuff`, `chore: changes`, `feat: improvements`.
Good: `fix: stop land aborting on stale index lock`, `test: pin model tiers for vendored agents`.

## Verify

```bash
git log -1 --format=%H
git status --porcelain
git branch --contains <sha>
```

Report the sha only after all three agree with what you intended.

## Replying to review comments

- Quote the finding, state fixed, declined or deferred, and give the commit sha or the evidence.
- Do not argue taste; apply the project's convention and say which one.
- A declined finding needs a reason a reviewer can check.
