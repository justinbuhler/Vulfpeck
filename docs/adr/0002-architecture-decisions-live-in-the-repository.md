# 0002. Architecture decisions live in the repository

Status: proposed
Date: 2026-10-22
Components: scripts/adr.py, .claude/hooks/adr_gate.py

## Context

The ADR practice this harness inherited came from a user-level toolkit. Its records live in a per-user directory outside the repository: they are not versioned, not reviewed with the change and never pushed. Its gate accepts any file with the right name, even an empty one, guards only the Write tool, so a shell redirect skips it, honours a bypass environment variable, and fails open when it crashes. The harness must not depend on that toolkit being installed.

## Decision

ADRs are `docs/adr/NNNN-slug.md` in the repository (`harness.toml` `adr.dir`). Each has a Status (proposed, accepted, rejected, deprecated, superseded), a Date, the `Components` it governs, optional `Supersedes` / `Superseded-by` links, and non-empty Context, Decision and Consequences sections. `scripts/adr.py` creates (`new`), validates (`check`) and queries (`for`, `list`) them.

Files matching `adr.require_for` need a proposed or accepted ADR naming them. That is enforced twice. `.claude/hooks/adr_gate.py`, a fail-closed gate, denies creating an uncovered file and makes a malformed ADR exit 2 the moment it is written. `tests/test_adr.py` runs `adr.py check` over every tracked file in the suite, so the land refuses an uncovered component however it was created. The check also flags an accepted ADR that names files which no longer exist, and supersession links that do not resolve both ways. Matching is case-insensitive, components and exemptions are literal paths, never globs, so one record cannot stand in for every governed file (every glob rule tried was beaten by the next glob), template placeholder text is refused, and the gate applies only to the session's own project. Every gate decision goes to the decision log. There is no bypass variable: exempting a file means changing `adr.exempt` in a reviewed commit.

## Consequences

A decision is reviewed in the same diff as the code it governs, and it travels with the repository. Adding a hook or script costs one short record first. Status is set by the author; moving an ADR from proposed to accepted is a review outcome, and the harness does not check who made that change.
