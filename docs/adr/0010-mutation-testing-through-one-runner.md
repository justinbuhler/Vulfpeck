# 0010. Mutation testing through one runner

Status: proposed
Date: 2026-10-22
Components: mutants/runner.py

## Context

AGENTS.md requires mutation testing for High-consequence changes. Mutants applied by hand, and kills judged by eye, are not evidence.

## Decision

`mutants/runner.py` is the only supported way to run the corpus: it refuses a dirty tree, runs the suite once clean, applies each `mutants/patches/M###.patch`, records the failing assertion names per mutant in `mutants/mutation_ledger.jsonl`, and checks the tree is byte-identical after each revert. The target design (sizing, isolation, baseline twice) is `docs/mutation-testing.md`.

## Consequences

Kills are recorded per assertion, so a kill by import crash shows up as a gap. The runner applies mutants in the checkout until isolation is built.
