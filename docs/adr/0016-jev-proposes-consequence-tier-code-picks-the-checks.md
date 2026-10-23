# 0016. Jev proposes consequence tier, code picks the checks

Status: proposed
Date: 2026-10-23
Components: scripts/jev.py, scripts/plan_tests.py

## Context

`AGENTS.md` ("Proportional testing") says the consequence tier picks what an agent runs before it reports, but nothing computes the tier or turns it into commands. `docs/mutation-testing.md` already fixes the rules for any model judgment: Jev answers typed Choices and Nouls, code applies thresholds, a failed call is never read as "no". Path globs alone mis-rate a small change in a High area, and a model alone can lower a tier that a control file deserves.

## Decision

`scripts/jev.py` is a stdlib-only Jev client with three typed decisions: `consequence`, `intent_alignment` and `reassess`. Each returns a plain dict with probabilities and a `source` of `jev` or `fallback`. It sends only file paths, diff stats, changed symbol names, the task description and short rule text, never file contents or environment values, redacts secret-shaped strings, and truncates a request that exceeds the size cap. `JEV_DISABLE=1`, a missing key or any transport error yields the deterministic fallback.

`consequence` takes a floor from the `[tiers]` globs in `harness.toml`; Jev may raise the tier above the floor, never lower it. The `docs/mutation-testing.md` acceptance rule applies: High or Critical at probability 0.6 or more is accepted; Low or Medium at 0.8 or more is accepted only with the alters-values Noul at 0.35 or less; anything else is a referral to the owner. Thresholds live in code.

`scripts/plan_tests.py` maps a tier to the exact commands: Low is lint plus mapped tests, Medium adds test files that name a changed module, High is the full suite plus mutants whose patch touches a changed file, Critical adds a note requiring independent re-derivation. It prints the tier and what it skipped. The land gate stays the full suite; `gate.sh` and `land.sh` do not call Jev.

## Consequences

An agent can run a proportionate check set and state what it skipped. A live call needs `TYPESAFE_API_KEY`; without it everything runs on the code floor. The planner trusts the `[tiers]` globs for the floor, so a High path missing from them is rated by Jev alone above Medium. This does not wire `reassess` to the loop guard and does not record plans for the land gate.
