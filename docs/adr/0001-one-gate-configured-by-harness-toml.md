# 0001. One gate, configured by harness.toml

Status: proposed
Date: 2026-10-22
Components: scripts/gate.sh, scripts/run.sh, scripts/standalone_tests.sh, scripts/harness_config.py, scripts/harness_env.sh, scripts/paths.py, .githooks/pre-commit, .claude/hooks/test_for_edited_module.py

## Context

The harness came from one Python project whose commands (uv, ruff, its own test loop) were spelt inside the lander, the pre-commit hook, CI and the mapped-test hook. Each copy could drift from the others, and none of it worked in a repository that builds differently.

## Decision

One script, `scripts/gate.sh`, is the gate: setup, lint, then `scripts/run.sh --tests`. The lander, CI and pre-commit (`--lint-only`) all call it. Every project-specific command lives in `harness.toml`, read by `scripts/harness_config.py`; an unknown key is an error, so a misspelt key cannot silently leave a default in force. `run.sh` refuses a test run that exits 0 without reporting a non-zero check count. The PostToolUse mapped-test hook takes its edited-file-to-test rules from the same file.

## Consequences

Adopting the harness means copying it and editing `harness.toml`. A gate run that passes is the same check wherever it runs. Reading `harness.toml` needs Python 3.11 (tomllib); `harness_env.sh` falls back to `uv run` when the system Python is older.

## Alternatives

Per-project forks of `run.sh` and `land.sh`, which cannot take each other's fixes. Configuring the gate through environment variables only, which is neither reviewable nor versioned with the code. Ignoring unknown config keys, which would leave the default gate silently in force under a misspelt key.
