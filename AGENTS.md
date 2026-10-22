# AGENTS.md - Vulfpeck

Read `CLAUDE.md`, then the `AGENTS.md` in every folder whose files you touch.
Root policy holds cross-agent invariants; folder files own domain rules.

## Cross-agent invariants

1. Sample 3 to 5 representative cases and inspect output by hand before a full pass. A sample finds defects; it does not support a population claim.
2. Import repository paths from `scripts/paths.py`; never anchor them to `Path(__file__).parent`.
3. When `.codegraph/` exists, use `codegraph explore` before text search. In the main checkout `codegraph sync` is automated: `.githooks/post-merge` runs it in the background when main moves, and the SessionStart hook runs it once when `scripts/codegraph_freshness.py` reports CG-02. In a linked worktree the index describes main, not your branch: use it read-only to locate code, read the files your branch changed directly, and never run `codegraph sync` there.
4. `uv` owns Python. Never use `pip install`. Prefer the standard library; a runtime dependency needs a stated reason in `pyproject.toml`.
5. Policy states mechanisms, not snapshots. Derive current quantities with a command (for example `./scripts/run.sh --tests`) rather than writing them here.
6. A reported count, rate or coverage figure is derived twice by independent means before it enters a commit message, PR body or report (`.claude/agents/dual-derive.md`).
7. An external-API claim needs a retained request/response artifact on disk (`.claude/agents/live-proof-auditor.md`).
8. Never commit secrets, private keys, `.env`, HAR captures, or data and binaries over 10 MB.
9. Record the decision before the component. A file matching `harness.toml` `adr.require_for` needs a proposed or accepted ADR in `docs/adr/` naming it in `Components:` before it is created (`.claude/hooks/adr_gate.py`) and at every land (`tests/test_adr.py`). An author writes `proposed`; `accepted` is set by the reviewer. Change a decision with a new ADR that supersedes the old one, never by rewriting an accepted one.

## Delegation and review

The parent session is LEAD for architecture, arbitration, integration, and adversarial gating.
Delegate bounded implementation and independent review where consequence warrants it.
No prose can mechanically route the interactive session; tracked workflow and hook controls are described in `CLAUDE.md`.

- **ORCH-01 Single writer.** At most one agent holds write authority over a file at a time.
- **ORCH-02 A report is not evidence.** The LEAD independently re-runs the relevant check.
- **ORCH-03 Immutable candidate.** Commit the candidate before adversarial review, and land exactly the reviewed commit: `./scripts/land.sh --expect <reviewed sha>` refuses a tip that moved after the review. Reviewers never mutate repository or git state; mutation attacks use a disposable copy outside all authoritative checkouts. For every agent type in `harness.toml [roles] read_only`, `.claude/hooks/readonly_guard.py` compares the main checkout before and after: HEAD, every ref, the local git config, `info/exclude`, `.git/hooks`, the worktree list, and the status of every path. A branch checked out in another worktree may only advance (a writer committing there), and that advance is reported. The snapshot's hash is recorded in the hash-chained decision log, so rewriting it is caught. It cannot see writes outside the main checkout. Task-worktree agents under `agent_harness.py` never commit or stage: the lander refuses a worktree whose HEAD moved outside its queue or whose index holds a path the request does not name, and commits only the queued paths, taken literally (`enqueue` refuses patterns).
- **ORCH-04 Baseline before writers.** Freeze the before-state from git before granting a write lease (`.claude/harness/agent_harness.py provision`).
- **ORCH-05 Verified completion.** `RUNNING -> REPORTED_COMPLETE -> VERIFIED_COMPLETE`; only the last state releases dependent work.
- **ORCH-06 LEAD owns reviewer environment.** Create it, wait for reviewer termination, then remove it.
- **ORCH-07 One bounded outcome.** One question, one non-overlapping file set, commands that finish in minutes, and one durable artifact.
- **ORCH-08 Parent owns waits.** Long suites, corpora, full-population counts, and near-ceiling commands remain in the persistent parent session.
- **ORCH-09 Durable artifact.** Consequential agents write task, status, candidate commit, findings, and completeness to a named artifact before returning.
- **ORCH-10 Task state is advisory.** Only artifact harvest, exit-status verification, and independent re-derivation establish completion. A review without a verdict is invalid; no findings is not approval.
- **ORCH-11 At most two delegated agents, never nested.** The LEAD owns all delegation.
- **ORCH-12 Two `main` refs, one integration target.** `origin/main` is that target: a change counts as integrated only once it is reachable from `origin/main`, and a local land gates and stages a candidate rather than publishing one. `./scripts/land.sh` gates the merged tree and moves **local** `refs/heads/main` by compare-and-swap, re-merging when a peer lands first, and exits non-zero unless the reviewed candidate is provably reachable from `main`. It contains no `git push`: it cannot move `origin/main`, which is moved by merging a pull request. Fast-forward local `main` from `origin/main` before landing onto it or cutting a branch from it. Agents commit freely on their own branch (ORCH-03); the pre-commit hook refuses commits made while `HEAD` is `main`, which a land never trips because its merge commit is made in a detached scratch worktree. Claim a land only from `land.sh`'s own exit status, never from narration, and never call a local land published - `git merge-base --is-ancestor <sha> origin/main` is the only proof of that.

The LEAD edits directly only for trivially mechanical changes where delegation creates more concurrency risk, followed by an independent delta review.

| Consequence | Scope | Required review |
|---|---|---|
| Low | Docs, formatting, mechanical | Tests plus independent standard review |
| Medium | Bounded deterministic logic | Tests plus fresh review |
| High | Matching, identity, authority | Mutation testing plus adversarial review |
| Critical | Human authority, destructive writes, broad reconciliation | Independent derivation plus full-population evidence |

Each phase gets one review and one corrective round. A third round requires an explicit scope reset.

## Ownership pointers

- Before test work -> `tests/AGENTS.md`.
- For dated failures, measurements, limitations, and implementation history -> `ENGINEERING_LOG.md`.
- For durable architectural decisions -> `docs/adr/` (`python3 scripts/adr.py`).
