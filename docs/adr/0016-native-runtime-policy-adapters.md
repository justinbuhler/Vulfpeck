# 0016. Adapt native runtime events to repository policies

Status: proposed
Date: 2026-10-22
Components: scripts/native_hook_adapter.py, tests/test_native_hook_adapter.py, scripts/native_review.py, tests/test_native_review.py, .codex/hooks.json, .claude/settings.json, scripts/harness_doctor.py, .claude/hooks/verify_commit_landed.py, tests/test_harness_controls.py, tests/test_commit_landed_hook.py

## Context

Codex preserves native tool payloads even when match aliases resemble Claude tools.
A patch can edit, add, delete, and rename several files in one invocation.
Commit output can be quiet or filtered by RTK, and a command can address a different checkout through git -C.
Gate crashes, unavailable scripts, malformed output, and timeouts require explicit failure behavior.

## Decision

Keep the existing policy scripts and translate native events in one small adapter.
Expand every supported patch into file events, including both rename paths, and deny malformed patches or paths escaping the checkout before running policy.
Preserve native session, tool-call, and agent identities; use the execution workdir for shell policy and explicit native roles for model routing.
Run policy subprocesses with an inner deadline, convert gate failures into bounded explicit denials, and retain advisory failure semantics.
Register the applicable policies in both runtimes, with Claude gate onFailure set to block.
Capture commit state before supported commands and verify HEAD, branch reachability, and status afterward independently of printed output.
Expose a fresh Codex review launcher that uses the shared candidate-bound receipt core rather than inventing another approval format.
Doctor reports effective runtime registration and trust failures; discovered declarations alone never establish activation.

## Consequences

The native client loops remain native and the policy cores retain their Claude behavior.
An unsupported payload is named rather than treated as permission to bypass a gate.
Codex outer command-hook timeout and untrusted hook definitions can still skip enforcement; this adapter cannot repair that runtime behavior and doctor must report the limitation.
Read-only drift snapshots detect changes inside their scope and do not isolate shell access, credentials, or token privileges.
Review execution and its cooperative process receipts do not make same-user filesystem state tamper-proof.
