# 0005. Read-only agents are checked by snapshot, not by command blocklist

Status: proposed
Date: 2026-10-22
Components: .claude/hooks/readonly_guard.py, .claude/agents/dual-derive.md, .claude/agents/live-proof-auditor.md

## Context

ORCH-03: reviewers never mutate repository or git state. A PreToolUse blocklist of mutating shell commands cannot be complete (`sed -i`, `tee`, `python -c`, `git update-ref`). Claude Code passes `agent_id`, `agent_type` and `cwd` to SubagentStart and SubagentStop, and SubagentStop can block. `dual-derive` and `live-proof-auditor` are this harness's own read-only roles.

## Decision

When an agent whose type matches `[roles] read_only` starts, snapshot the main checkout: HEAD, checked-out branch, local `main`, every ref, the local git config, `info/exclude`, `.git/hooks`, the worktree list, and full status. A branch checked out in another worktree may only advance, because a writer committing there is expected, and its advance is reported; any other move blocks. The snapshot file is not hash-checked and no log is consulted: an agent that rewrites the snapshot to match its own changes defeats the comparison, so reviewers run in a throwaway worktree where a change cannot reach the main checkout. Compare when it stops. A difference blocks the stop once with the changed paths; on `stop_hook_active` the agent may stop, the snapshot is kept and the parent is told. A stop with no snapshot is reported as unverified, and is checked against the default read-only list as well as the current config, so an agent cannot opt out by editing the roles. Every outcome is recorded.

## Consequences

Detection is complete for the main checkout and blind outside it: writes to another worktree or outside the repository are not seen. A parent session editing while the reviewer runs also trips it, so the message says the checkout changed, not that the agent changed it.
