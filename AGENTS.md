# AGENTS.md - Vulfpeck

This file is the one rules source for Claude Code, Codex and Gemini CLI; `CLAUDE.md` imports it and `.gemini/settings.json` points Gemini at it.
Read the `AGENTS.md` in every folder whose files you touch.
Root policy holds cross-agent invariants; folder files own domain rules.
`HARNESS.md` describes what each control does, how `harness.toml` configures it, and its open gaps.

## Project context

<!-- Describe what Vulfpeck is, its sources of truth, and its main flow here. -->

## Commands

```bash
uv sync
git config core.hooksPath .githooks
codegraph explore "<question or symbol>"
codegraph sync
uv run python3 scripts/codegraph_freshness.py
./scripts/gate.sh
./scripts/run.sh --tests
./scripts/land.sh
./scripts/worktree_status.sh
./scripts/github_bootstrap.sh --apply
python3 mutants/runner.py --only M001
python3 scripts/adr.py new "<decision>" --component <path>
python3 scripts/adr.py check
uvx ruff check .
```

`harness.toml` defines setup, lint and test commands; `scripts/gate.sh` runs them for the land, CI and (lint only) pre-commit.
The pre-commit hook lints and refuses commits on `main`; the suite gates the land, not the commit, and the hook is inert in a fresh clone until `core.hooksPath .githooks` is set.
`mutants/` is the mutation corpus for High-consequence review; `mutants/patches/M###.patch` holds the mutants and `mutants/mutation_ledger.jsonl` records kills.

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
No prose can mechanically route the interactive session; tracked workflow and hook controls are listed in `CLAUDE.md` and `HARNESS.md`.
These LEAD obligations are held by review, not by a test, and the LEAD never approves its own consequential work.

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

## Control principles

- **Rule 11, the relocating oracle.** A control must not derive its expectation from the thing it checks: such an oracle agrees by construction and passes hardest exactly when the artefact is wrong. `MODEL-03B` in `tests/test_workflow_routing.py` is the pattern to copy: it keeps its own expectation table and refuses to read it from the files it validates.
- Mechanical discovery and counting: cheapest model tier; bounded implementation: mid tier; review, planning and re-derivation: top tier; every delegation names its model explicitly. Authoring and approval occur in fresh contexts.
- Every gate hook fails closed (a crash blocks) and every advisory fails open; `tests/test_harness_controls.py` pins the registrations and proves both.
- Hook success is permission to continue, not proof the candidate is correct.
- Never hand-edit marker-delimited or generated content; change its owner and regenerate.

## Scope

- Before editing, state Modifying / Not touching / Out-of-scope observations. (prose; enforcement planned)
- An out-of-scope finding becomes an issue or a report line, never an edit. (prose; enforcement planned)
- Build no unrequested feature, option or abstraction; make the simplest change that solves the task and touch only what it needs. (prose; enforcement planned)
- A task is defined by read_first, action, acceptance criteria and a verification command. (prose; enforcement planned)

## Stop and reassess

- A failure fingerprint (same failing check or error) seen 3 times, meaning the original failure plus two failed repairs: stop, write what was tried and why it failed, then change approach or escalate. (prose; enforcement planned)
- Two consecutive edit-and-run cycles with no new passing check: stop. (prose; enforcement planned)
- Never re-run an unchanged failing command expecting a different result. (prose; enforcement planned)
- One corrective round per phase (see Delegation and review); a further round needs an explicit scope reset. (prose; enforcement planned)
- A missing worktree or broken environment is reported once, not looped on. (prose; enforcement planned)

## Proportional testing

- The consequence tier picks what an agent runs before reporting (the land gate is unchanged: the full suite). Low: lint plus mapped tests. Medium: plus affected test files. High: full suite plus mutation on the touched High paths only. Critical: plus independent re-derivation. (prose; enforcement planned)
- A bug fix shows red, then green. (prose; enforcement planned)
- Every report names which gate ran and what was skipped. (prose; enforcement planned)
- A test that cannot fail when the logic changes is wrong. (prose; enforcement planned)

## Dispatch and report

- A worker returns status `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT` or `BLOCKED`. (prose; enforcement planned)
- `NEEDS_CONTEXT` is re-dispatched with the context; `BLOCKED` is split or escalated, never retried as-is. (prose; enforcement planned)
- A brief states scope, forbidden actions, done-when and report fields. (prose; enforcement planned)
- A worker never spawns agents or worktrees unless its brief allows it. (prose; enforcement planned)

## Turn completion

- Do not end a turn announcing work you have not done; do it or name the blocker. (prose; enforcement planned)

## Working rules

- Add no defensive code without a stated reason. (prose; enforcement planned)
- Fail loud: skipped work is reported, and "tests pass" is claimed only if none were skipped. (prose; enforcement planned)
- Separate verified facts from assumptions; label an unreproduced root cause "unverified hypothesis". (prose; enforcement planned)
- Commits are `{type}: description`, at most 50 characters, one logical change, with no agent attribution trailer. (prose; enforcement planned)
- Use `git -C <path>` rather than a separate `cd`. (prose; enforcement planned)
- Never remove a worktree an agent still holds. (prose; enforcement planned)
- `uv` only, never `pip` (invariant 4).

Adapted from ideas in AsiaOstrich/universal-dev-standards (CC BY 4.0).

## Ownership pointers

- Before test work -> `tests/AGENTS.md`.
- For dated failures, measurements, limitations, and implementation history -> `ENGINEERING_LOG.md`.
- For durable architectural decisions -> `docs/adr/` (`python3 scripts/adr.py`).
