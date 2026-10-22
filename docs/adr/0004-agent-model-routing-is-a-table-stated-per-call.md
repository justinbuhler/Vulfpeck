# 0004. Agent model routing is a table, stated per call

Status: proposed
Date: 2026-10-22
Components: .claude/hooks/require_agent_model.py, .claude/hooks/model_router.py, .claude/workflows/review-changes.js

## Context

A subagent that inherits the parent's model runs review on whatever model the session happens to use, and bounded implementation on the most expensive one. Routing by prompt wording is not checkable.

## Decision

Every `Agent` call states `model`; `require_agent_model.py` denies a missing or `inherit` model. `model_router.py` denies a model outside the tier of its `subagent_type` (Haiku for Explore; Sonnet for implementation engineers and simplifiers; Opus or Fable for review, planning and dual-derive), and passes unlisted types and `fork`. Workflow `agent()` calls name a ROLE, and `tests/test_workflow_routing.py` keeps its own ROLE-to-model table (Rule 11).

## Consequences

Routing is mechanical and testable. Types the table does not list pass with any model, so each installation lists the types it uses. The interactive parent session is not routed.
