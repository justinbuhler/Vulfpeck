# 0013. CodeGraph indexes belong to checkouts

Status: proposed
Date: 2026-10-22
Components: scripts/codegraph_freshness.py, .githooks/post-checkout, .githooks/post-merge, .claude/hooks/blast_radius_advisory.py

## Context

A linked checkout without its own index can resolve upward to a primary checkout and report unrelated code as fresh.
Vulfpeck must preserve the repository owner's choice to leave CodeGraph disabled.

## Decision

Port the checkout-local lifecycle from LA28 commit `6709caeea76958fea41a388e137fc902c261ecd6`.
Prepare and verify an index only when the current or primary checkout already opted in.
Require the index project path to match the checkout, scrub inherited Git overrides, and prepare lander checkouts before verification.
Clean up only a verified linked checkout's index after stopping its own writer, retaining shared Git hooks.

## Consequences

Unindexed repositories stay unindexed.
Each indexed checkout answers for its own content, and unverified ownership prevents cleanup.
The existing gate, review policy and project commands remain unchanged.
