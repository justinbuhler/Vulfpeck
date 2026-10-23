@AGENTS.md

# Claude Code notes

Rules live in `AGENTS.md`; this file holds only what is specific to Claude Code.

- Hooks are registered once in `harness.toml` `[[hook]]` and rendered by `scripts/render_hooks.py` into `.claude/settings.json`, `.codex/hooks.json` and `.gemini/settings.json` (never hand-edit their `hooks`); they are implemented in `.claude/hooks/`: CodeGraph freshness, the test mapped to an edited module, `git commit` landed verification, and the blast radius of an edit to a shared control. `scripts/harness_doctor.py` reports missing prerequisites and interfering user-level hooks at SessionStart.
- Model tiers for `Agent` calls: Haiku for mechanical discovery and counting, Sonnet for bounded implementation, Opus for review, planning and re-derivation; an `Agent` call states `model` explicitly.
- The Agent-matcher hooks run `require_agent_model.py`, which denies an `Agent` call with no or `inherit` model, and `model_router.py`, which denies an `Agent` call whose `model` does not match its `subagent_type` tier (Haiku: Explore; Sonnet: implementation engineers and code-simplifiers; Opus or Fable: review, planning and dual-derive) and passes unlisted types and `fork`. Neither routes the interactive parent session.
- Every tracked `.claude/workflows/*.js` `agent()` call selects a named ROLE; `tests/test_workflow_routing.py` pins ROLE-to-model mappings and workflow inventory.
- Gate decisions are recorded, hash-chained, in `.git/harness/events.jsonl` by `scripts/harness_events.py`; `python3 scripts/harness_events.py verify` checks the chain.
- Codex runs the hooks of the main checkout, not of a linked worktree: a branch that changes hook registration is only exercised under Codex from a standalone clone or after it lands on main (`scripts/harness_doctor.py --runtimes` reports it).
- Skills live once in `.agents/skills/`; `.claude/skills` links to it. Gemini and Codex load project skills, settings and agent roles only after the owner trusts the folder.

## Agent skills

- Issue tracker: GitHub Issues through `gh` (`docs/agents/issue-tracker.md`).
- Triage labels: the five canonical labels (`docs/agents/triage-labels.md`).
- Domain docs: `docs/agents/domain.md`.
