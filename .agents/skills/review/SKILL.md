---
name: review
description: Use when asked to review a file, diff, branch or pull request for correctness, security, performance and maintainability, or when handing a candidate commit to an independent reviewer.
---

# Review

Review the committed candidate, not the working tree. Reviewers never change repository or git state. No findings is not approval: a review without a verdict is invalid.

## Severity and verdict

When in doubt, classify up.

| Level | Meaning | Examples |
|---|---|---|
| BLOCKING | Security, correctness, reliability | Auth bypass, race, resource leak, logic error, failing test |
| SHOULD FIX | Material quality, pattern, test gaps | Missing tests, unhelpful errors, N+1 on a hot path |
| SUGGESTION | Optional, stylistic | Naming, comments, micro-optimization |

Any BLOCKING: REQUEST-CHANGES. SHOULD FIX only: FIX before merge. Otherwise APPROVE. Omit empty sections and state scope, evidence and limits.

## Systematic review (default)

1. **Understand.** Read the repo instructions and the whole diff. Read the surrounding code. For a changed signature, find every caller and implementation. Trace each parameter to its source and check validation at each caller.
2. **Verify.** Run the checks the consequence tier requires (see `testing`). Check claims in comments and the PR body against code and tests. A skipped or inferred check is not a pass.
3. **Assess.** Security (auth, validation, injection, secrets), performance (unbounded work, N+1), design (conventions, scope, needless abstraction). Recheck the contract and callers of any extracted helper.
4. **Verify findings.** For each finding confirm the input sequence reaches it and no existing guard covers it. Drop unreachable or handled claims.
5. **Report**, using `references/report-template.md`.

## Reviewer environment

Run a reviewer in a throwaway worktree. `readonly_guard.py` compares only the main checkout before and after, so it cannot see a reviewer write anywhere else. A mutation attack uses a disposable copy outside every authoritative checkout. The LEAD creates the environment and removes it once the reviewer has terminated (`delegate`).

## Two reviewers

For a consequential change, use at most two independent reviewers, never nested and each on an explicit model (root `AGENTS.md` ORCH-11): `reviewer-code` for quality, tests and design, `reviewer-system` for security, concurrency, errors and contracts. Give each the candidate sha and the diff command, no conclusions. Deduplicate findings and resolve severity disagreements from evidence. If a reviewer returns nothing, report the gap; do not claim approval.

## References

| Signal | Load |
|---|---|
| Architecture lens | `references/architecture-smell-baseline.md` (pass it to the architecture reviewer verbatim) |
| Receiving review feedback | `references/receiving-feedback.md` |
| Writing the report | `references/report-template.md` |
| Security-sensitive diff | the `security` skill |
