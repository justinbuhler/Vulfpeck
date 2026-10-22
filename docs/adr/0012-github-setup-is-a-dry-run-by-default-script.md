# 0012. GitHub setup is a dry-run-by-default script

Status: proposed
Date: 2026-10-22
Components: scripts/github_bootstrap.sh

## Context

Branch protection and triage labels are repository settings that no commit carries. Creating them by hand drifts from what the docs describe, and running a setup script by mistake changes a live repository.

## Decision

`scripts/github_bootstrap.sh` prints the label and branch-protection calls it would make; only `--apply` makes them. It creates the five canonical triage labels and requires the `tests` check on `main`.

## Consequences

Repository settings are reproducible from the tree. Applying them is a deliberate, outward-facing step that someone confirms.
