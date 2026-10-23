# 0019. Copy declared inputs into isolated specimens

Status: proposed
Date: 2026-10-23
Components: scripts/harness_inputs.py, scripts/land.sh, .claude/harness/agent_harness.py, .claude/hooks/readonly_guard.py

## Context

The task provisioner copies ignored directories while the lander uses a separate copy path.
Those paths can carry credentials or scratch worktrees, follow destination links into other data, or overwrite tracked candidate bytes through filesystem aliases.
Status-only reviewer snapshots also miss further edits to files already reported as dirty or untracked.

## Decision

Use scripts/harness_inputs.py for both callers and copy only land.copy_untracked entries declared by the task baseline or landing candidate.
Validate the complete input list before copying, reject secret and operational paths, reject source links and destination symlink ancestors or multiply-linked files, and exclude tracked files by path and filesystem identity.
Copy regular files into independent destination files while preserving executable modes and checking their hashes.
Reviewer snapshots include working bytes, file types, modes, link targets and staged identities while retaining the existing reference checks and hash-chained snapshot verification.

## Consequences

Task and landing inputs follow the same explicit policy, and a missing or unsafe declared input stops the operation.
Projects must list required inputs rather than relying on ignored-directory discovery.
The reviewer guard detects content changes even when Git status stays unchanged.
These checks do not provide an operating-system sandbox or protect against concurrent hostile filesystem changes.
