# 0011. Code intelligence and blast radius are advisory

Status: proposed
Date: 2026-10-22
Components: scripts/codegraph_freshness.py, .claude/hooks/blast_radius_advisory.py

## Context

A stale code graph answers questions about code that no longer exists. Edits to shared controls break callers that spell the same literals. Neither condition is a reason to stop work.

## Decision

`codegraph_freshness.py` reports index freshness (CG-01 to CG-04) at each prompt and syncs a stale index at startup. `blast_radius_advisory.py` prints which tracked files also spell the literals an edit to a watched file changes. Both are registered fail-open and never block.

## Consequences

Agents see staleness and blast radius in context. Neither one enforces anything; the suite does.
