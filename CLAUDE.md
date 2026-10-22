# CLAUDE.md - Vulfpeck

Root `AGENTS.md` is the normative governance contract; after this file, read only the folder-local `AGENTS.md` files governing paths you inspect or change.
`HARNESS.md` describes the agent harness: what each control does, how `harness.toml` configures it, and its open gaps.

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
The pre-commit hook lints and refuses commits on `main`; the suite gates the land, not the commit, and is inert in a fresh clone until `core.hooksPath .githooks` is configured.
`mutants/` is the mutation corpus that root `AGENTS.md` requires for High-consequence review; `mutants/patches/M###.patch` holds the mutants and `mutants/mutation_ledger.jsonl` records kills.

## Claude routing, hooks, and workflows

The interactive session is not routed by this file.
The parent session remains LEAD for architecture, arbitration, integration, and adversarial gating.
These LEAD obligations are held by review, not by a test, and the LEAD never approves its own consequential work.

Workflow model routing is mechanical: every tracked `.claude/workflows/*.js` `agent()` call selects a named ROLE, and `tests/test_workflow_routing.py` independently pins ROLE-to-model mappings and workflow inventory.
The Agent-matcher hooks run `require_agent_model.py`, which denies an `Agent` call with no or `inherit` model, and `model_router.py`, which denies an `Agent` call whose `model` does not match its `subagent_type` tier (Haiku: Explore; Sonnet: implementation engineers and code-simplifiers; Opus or Fable: review, planning and dual-derive) and passes unlisted types and `fork`.
These mechanisms select models only on the surfaces they control; neither routes the interactive parent session.

**Rule 11, the relocating-oracle rule.** A control must not derive its expectation from the thing it checks: such an oracle agrees by construction and passes hardest exactly when the artefact is wrong.
`MODEL-03B` in `tests/test_workflow_routing.py` is the pattern to copy - it keeps its own expectation table and refuses to read it from the files it validates.
Its model-shaped case is delegation: bounded implementation goes to Sonnet, mechanical discovery and complete counting to Haiku, review, planning and independent re-derivation to Opus, and an `Agent` call states `model` explicitly rather than inheriting.
Authoring and approval occur in fresh contexts, with review depth set by consequence in root `AGENTS.md`.

Every gate hook fails closed (a crash blocks) and every advisory fails open; `tests/test_harness_controls.py` pins the registrations and proves both. Gate decisions (allow and deny) are recorded, hash-chained, in `.git/harness/events.jsonl` by `scripts/harness_events.py`; `python3 scripts/harness_events.py verify` checks the chain. `scripts/harness_doctor.py` reports missing prerequisites and interfering user-level hooks at SessionStart.
Claude hooks also report CodeGraph freshness, run the test mapped to an edited module, verify that a `git commit` really landed, and print the blast radius of an edit to a shared control.
Treat hook success as permission to continue, not proof that the candidate is correct.
Never edit marker-delimited or generated content by hand; change its owner and regenerate.

## Agent skills

### Issue tracker

GitHub Issues, accessed through `gh`.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five canonical triage labels.
See `docs/agents/triage-labels.md`.

### Domain docs

See `docs/agents/domain.md`.
