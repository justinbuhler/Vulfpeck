# 0007. Gate decisions go into a rotated local log

Status: proposed
Date: 2026-10-22
Components: scripts/harness_events.py

## Context

Hook denials, blocks and lands printed to the session and vanished. Nobody could answer which gates fired. A hash chain over the lines added integrity checks that no gate relied on and that an agent running as the user could rebuild.

## Decision

Every gate decision appends one plain JSON line to `<git common dir>/harness/events.jsonl`: hook, decision, reason, subject, session. At 5 MB the file moves to `events.jsonl.1`, replacing the old backup. Recording never raises and never changes a decision. Tests redirect it with `HARNESS_EVENTS_LOG`.

## Consequences

The log is telemetry, not evidence: it can be edited or deleted and nothing detects that. It stays on the machine until a publish step carries it out, and holds at most about 10 MB.
