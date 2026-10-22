# 0015. GPT routing uses native Codex agents

Status: proposed
Date: 2026-10-22
Components: .codex/hooks.json, .codex/agents/explorer.toml, .codex/agents/executor.toml, .codex/agents/reviewer.toml, .codex/agents/planner.toml, .claude/hooks/model_router.py

## Context

Claude's model-family rules do not route native Codex `spawn_agent` calls.
Codex 0.163.0 discovers standalone project agent files and preserves `spawn_agent` in hook payloads even when a matcher uses the `Agent` alias.
Some host surfaces omit native role selection, so a task name cannot prove which profile runs.

## Decision

Use native project profiles: explorer on `gpt-6-luna` with high effort, executor on `gpt-6.1-sol` with medium effort, and reviewer and planner on `gpt-6-astra` with high effort.
Register one Codex PreToolUse hook that calls the shared model router in an explicit Codex mode.
Require the native `agent_type` to name an approved profile and refuse mismatched explicit model or effort overrides.
Report role selection as unavailable when the calling surface does not supply it.
Keep Claude's existing family rules and the interactive parent model unchanged.

## Consequences

Codex applies the model and effort from the selected native profile, without a separate workflow runner.
Missing role selection fails visibly instead of inheriting the parent model silently.
Project configuration and the exact hook definition require native trust before execution.
Runtime proof must verify effective profile discovery and blocked and allowed spawn calls on the installed client.
