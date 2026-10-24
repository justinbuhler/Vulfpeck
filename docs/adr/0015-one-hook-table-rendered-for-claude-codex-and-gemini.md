# 0015. One hook table, rendered for Claude, Codex and Gemini

Status: proposed
Date: 2026-10-23
Components: scripts/hook_adapter.py, scripts/render_hooks.py, scripts/harness_install.py

## Context

The harness runs under Claude Code, Codex CLI 0.164 and Gemini CLI 0.55.1. Its guards in `.claude/hooks/` speak Claude Code's payload and output schema, and the hook registration lived by hand in `.claude/settings.json` alone. Codex and Gemini use other event names (Gemini), other tool names (both), `apply_patch` instead of Edit and Write (Codex), and other answer shapes. Three hand-kept registrations and a second copy of each guard would drift. Codex also asks the owner to trust each project hook (`[hooks.state]` in `~/.codex/config.toml`), and Codex and Gemini both load project config only from a trusted folder; nothing in the repository can grant either.

## Decision

Claude Code's schema is the internal format and every guard keeps speaking it. `scripts/hook_adapter.py` translates at the process boundary: it reads the native payload, builds the Claude-shaped payload (one per file for an `apply_patch`), runs the guard command, and converts the result back. A gate that crashes, or a result the target runtime cannot express, exits non-zero and the registration turns that into a block; an advisory fails open, as under Claude Code.

`harness.toml` holds one `[[hook]]` table (name, event, matcher, kind, timeout, command; `hook`, not `hooks`, because `[hooks]` is already a table). `scripts/render_hooks.py` writes the hook sections of `.claude/settings.json` and `.gemini/settings.json` and all of `.codex/hooks.json` from it, keeping every other key, and `--check` fails when a file is stale. `tests/test_harness_controls.py` keeps its own hand-written inventory (Rule 11) and asserts the Codex and Gemini registrations too.

`scripts/harness_doctor.py` reports per runtime the CLI and version, whether the rendering is fresh, whether the folder is trusted and whether auth is usable. `scripts/harness_install.py` is a dry run by default; `--apply` renders, sets `core.hooksPath` and runs `uv sync`, and `--trust` alone writes the Codex and Gemini folder trust entries. It prints, never runs, the CLI install commands and never touches `~/.claude`.

## Consequences

A guard is written once and its deny and allow canaries run through all three runtimes. Gemini has no subagent events and no Agent tool matcher, so `readonly_guard.py`, `require_agent_model.py` and `model_router.py` do not run there: a recorded gap, not an emulation. Codex hook trust is per hook hash and is granted in Codex; the installer does not write it. Codex reads `.codex/hooks.json` from the main checkout for every linked worktree and loads none when main has none, so a branch that changes hook registration is only exercised under Codex from a standalone clone or after it lands on main; the doctor fails when main's file is missing or differs. Codex `write_stdin` does not reach PreToolUse, an open gap. A patch sent through the Codex shell is reported as Bash and is gated by registering the edit guards on Bash too. Every adapter run has a deadline about 1s under the registered timeout, so a gate that hangs blocks. Open gap: Gemini BeforeTool deny is proven by tests only; live proof is pending owner auth. The `spawn_agent` field names and Codex subagent payloads are mapped from the documented shape and need a live subagent run to confirm. This does not cover rules files or skills (ADR 0013, 0014).
