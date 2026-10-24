# Vulfpeck

An agent-operated repository with its governance enforced by tests, hooks and a gated lander rather than by prose.
The harness is project-agnostic: `harness.toml` says how the project builds, and `HARNESS.md` describes each control, how to adopt it elsewhere, and its open gaps.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (uv installs it). `gh` is needed for GitHub setup; CodeGraph is optional.

```bash
uv sync
git config core.hooksPath .githooks     # lint at commit, no commits or merge commits on main
./scripts/gate.sh                       # setup, lint, suite: what the land and CI run
```

## How a change reaches main

1. Commit on a branch. The pre-commit hook lints and refuses commits on `main`.
2. `./scripts/land.sh` merges the branch onto local `main` in a scratch worktree, runs `scripts/gate.sh` on the merged tree, then moves local `main` by compare-and-swap.
3. Push the branch and open a pull request. The `tests` workflow gates it; `origin/main` moves only when the PR merges (AGENTS.md ORCH-12).

## Where the rules live

| File | Role |
|---|---|
| `AGENTS.md` | The one rules source for Claude Code, Codex and Gemini CLI: commands, invariants, delegation rules ORCH-01..12, consequence-to-review table, scope, stop-and-reassess and testing rules |
| `CLAUDE.md` | Imports `AGENTS.md`; Claude-only notes on hooks, model routing and workflows |
| `.gemini/settings.json` | Points Gemini CLI at `AGENTS.md` as its context file |
| `.agents/skills/`, `.codex/` | Skills for every runtime (`.claude/skills` is a symlink to them); Codex role declarations |
| `harness.toml` | Setup, lint and test commands, untracked inputs, watched controls, mapped tests |
| `HARNESS.md` | What each control enforces, adopting it in another repository, open gaps |
| `tests/AGENTS.md` | Test conventions (standalone scripts, not pytest) |
| `docs/adr/` | Architecture decision records, one per decision (`scripts/adr.py`) |
| `ENGINEERING_LOG.md` | Dated failures, measurements and history |

## First-time GitHub setup

After the repository exists and `origin` points at it:

```bash
./scripts/github_bootstrap.sh           # dry run
./scripts/github_bootstrap.sh --apply   # triage labels + protect main behind the tests check
```
