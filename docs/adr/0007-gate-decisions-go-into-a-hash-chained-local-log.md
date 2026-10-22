# 0007. Gate decisions go into a hash-chained local log

Status: proposed
Date: 2026-10-22
Components: scripts/harness_events.py

## Context

Hook denials, blocks and lands printed to the session and vanished. Nobody could answer which gates fired, or show that a record had not been edited afterwards.

## Decision

Every gate decision appends one JSON line to `<git common dir>/harness/events.jsonl`: hook, decision, reason, subject, session, and the SHA-256 of the previous line. `harness_events.py verify` walks the chain. Recording never raises and never changes a decision. Tests redirect it with `HARNESS_EVENTS_LOG`.

## Consequences

Tampering with or deleting a line is detectable. The log stays on the machine until a publish step carries it out, and a rewrite of the whole file from scratch is not detectable.
