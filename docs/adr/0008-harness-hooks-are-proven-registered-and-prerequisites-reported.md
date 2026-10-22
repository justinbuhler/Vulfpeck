# 0008. Harness hooks are proven registered; prerequisites are reported

Status: proposed
Date: 2026-10-22
Components: scripts/harness_doctor.py

## Context

Claude Code lets a tool call proceed when a hook exits with anything other than 0 or 2, so a crashing or unregistered gate looks like a gate with nothing to deny. A missing prerequisite (Python 3.11, uv, flock, `core.hooksPath`) silently disables part of the harness, and user-level hooks can undercut it.

## Decision

`tests/test_harness_controls.py` keeps its own inventory of every registration, runs each command as registered, gives each gate a deny and an allow canary, and proves gates fail closed and advisories fail open on a crash or a missing file. `scripts/harness_doctor.py` runs at session start and reports missing prerequisites and interfering user-level hooks; it never blocks.

## Consequences

A hook that is removed, renamed, mis-wrapped or crashing fails the suite. A hook that exceeds its timeout still fails open.
