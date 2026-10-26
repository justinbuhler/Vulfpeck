# AGENTS.md - Vulfpeck

This file is the one rules source for Claude Code, Codex and Gemini CLI; `CLAUDE.md` imports it and `.gemini/settings.json` points Gemini at it.
Read the `AGENTS.md` in every folder whose files you touch.
Root policy holds cross-agent invariants; folder files own domain rules.
`HARNESS.md` describes what each control does, how `harness.toml` configures it, and its open gaps.

## Project context

Vulfpeck has no application code: the repository is the harness (hooks, gate, lander, task loop, evals) and its tests. An adopting project replaces this paragraph with its own context and domain invariants.

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
python3 scripts/plan_tests.py --run
python3 scripts/eval_consequence.py --check
python3 scripts/eval_tasks.py --validate
python3 scripts/harness_events.py report
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
9. ADRs record real decisions, written when a choice is made and validated by `python3 scripts/adr.py check`. An author writes `proposed`; `accepted` is set by the reviewer. Change a decision with a new ADR that supersedes the old one, never by rewriting an accepted one.

## Delegation and review

The parent session is LEAD for architecture, arbitration, integration, and adversarial gating.
Delegate bounded implementation and independent review where consequence warrants it.
These LEAD obligations are held by review, not by a test, and the LEAD never approves its own consequential work.

- **ORCH-01 Single writer.** At most one agent holds write authority over a file at a time.
- **ORCH-02 A report is not evidence.** The LEAD independently re-runs the relevant check.
- **ORCH-03 Immutable candidate.** Commit the candidate before adversarial review and land exactly that commit. Reviewers never mutate repository or git state. Task-loop and task-worktree agents never commit or stage: their runner does.
- **ORCH-04 Baseline before writers.** Freeze the before-state from git before granting a write lease.
- **ORCH-05 Verified completion.** `RUNNING -> REPORTED_COMPLETE -> VERIFIED_COMPLETE`; only the last state releases dependent work.
- **ORCH-06 LEAD owns reviewer environment.** Create it, wait for reviewer termination, then remove it.
- **ORCH-07 One bounded outcome.** One question, one non-overlapping file set, commands that finish in minutes, one durable artifact.
- **ORCH-08 Parent owns waits.** Long suites, corpora, full-population counts and near-ceiling commands stay in the parent session.
- **ORCH-09 Durable artifact.** A consequential agent writes task, status, candidate commit, findings and completeness to a named artifact before returning.
- **ORCH-10 Task state is advisory.** Only artifact harvest, exit-status verification and independent re-derivation establish completion. A review without a verdict is invalid; no findings is not approval.
- **ORCH-11 At most two delegated agents, never nested.** The LEAD owns all delegation.
- **ORCH-12 Two `main` refs, one integration target.** `origin/main` is that target: a change is integrated only once reachable from it, and a local land is never "published".

| Consequence | Scope | Required review |
|---|---|---|
| Low | Docs, formatting, mechanical | Tests plus independent standard review |
| Medium | Bounded deterministic logic | Tests plus fresh review |
| High | Matching, identity, authority | Mutation testing plus adversarial review |
| Critical | Human authority, destructive writes, broad reconciliation | Independent derivation plus full-population evidence |

Each phase gets one review and one corrective round. A third round requires an explicit scope reset.

## Control principles

- **Rule 11, the relocating oracle.** A control must not derive its expectation from the thing it checks: such an oracle agrees by construction and passes hardest exactly when the artefact is wrong. `MODEL-03B` in `tests/test_workflow_routing.py` is the pattern to copy: it keeps its own expectation table and refuses to read it from the files it validates.
- Every gate hook fails closed (a crash blocks) and every advisory fails open; `tests/test_harness_controls.py` pins the registrations and proves both.
- Hook success is permission to continue, not proof the candidate is correct.
- Never hand-edit marker-delimited or generated content; change its owner and regenerate.

## Scope

- An out-of-scope finding becomes an issue or a report line, never an edit. (enforced: scope_guard, once a lease is set with `scripts/scope.py`)
- Build no unrequested feature, option or abstraction; make the simplest change that solves the task and touch only what it needs.

## Stop and reassess

- A failure fingerprint (same failing check or error) seen 3 times, meaning the original failure plus two failed repairs: stop, write what was tried and why it failed, then change approach or escalate. (enforced: loop_guard)
- Two consecutive edit-and-run cycles with no new passing check: stop. (enforced: loop_guard)
- Never re-run an unchanged failing command expecting a different result. (enforced: loop_guard)

## Proportional testing

- Before reporting, run `python3 scripts/plan_tests.py --run`: the floor tier picks the checks (Low lint and mapped tests, Medium adds naming tests, High the full suite plus mutants); the land gate is always the full suite.
- A bug fix shows red, then green.
- Every report names which gate ran and what was skipped.

## Dispatch and report

Delegation procedure is in the `delegate` skill; the task loop enforces the brief and the `STATUS:` line for its workers.

## Turn completion

- Do not end a turn announcing work you have not done; do it or name the blocker. (enforced: turn_guard)

## Working rules

- Fail loud: skipped work is reported, and "tests pass" is claimed only if none were skipped.
- Separate verified facts from assumptions; label an unreproduced root cause "unverified hypothesis".
- Commits are `{type}: description`, at most 50 characters, one logical change, with no agent attribution trailer.
- Use `git -C <path>` rather than a separate `cd`.

Adapted from ideas in AsiaOstrich/universal-dev-standards (CC BY 4.0).

## Ownership pointers

- Before test work -> `tests/AGENTS.md`.
- For dated failures, measurements, limitations, and implementation history -> `ENGINEERING_LOG.md`.
- For durable architectural decisions -> `docs/adr/` (`python3 scripts/adr.py`).
- For consequence-tier evals -> `evals/`, HARNESS.md "Consequence floor".
