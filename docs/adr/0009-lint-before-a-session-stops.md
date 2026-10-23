# 0009. Lint before a session stops; record where it left the repository

Status: proposed
Date: 2026-10-22
Components: .claude/hooks/session_guard.py

## Context

Stop fires after every reply of the main agent and can block; `stop_hook_active` marks a continuation. SessionEnd fires once and cannot block. A dirty tree between replies is ordinary work in progress.

## Decision

At Stop, run `commands.lint` when HEAD or the tree changed since the last check, or lint failed last time. When the command is `ruff check .`, it runs on the `.py` files changed since the merge-base with main plus untracked ones (`--force-exclude`), and not at all when none changed; any other command runs unchanged. A failure blocks once with the output; on the continuation the session may stop and is told lint still fails. Uncommitted or unlanded work produces a one-line notice when that state changes, never a block. At SessionEnd, record branch, HEAD, and dirty and unlanded counts in the decision log. Registered fail-open, so a crash cannot trap a session.

## Consequences

Sessions do not end on red lint unnoticed. A pre-existing failure in a file the session did not touch no longer blocks it; the land gate still lints the whole tree. A lint command other than `ruff check .` still runs on the whole tree.

## Alternatives

Blocking on a dirty tree or unlanded commits nags through normal multi-step work. Running the full gate at Stop costs minutes per reply; the land owns that.
