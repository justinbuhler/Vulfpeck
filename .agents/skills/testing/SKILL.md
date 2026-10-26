---
name: testing
description: Use when writing, changing, auditing or pruning tests, doing test-first work on a bug or feature, fixing a flaky or brittle test, or deciding how much testing a change needs before it can be called done.
---

# Testing

Read `tests/AGENTS.md` and the nearest folder `AGENTS.md` first. This repo's tests are standalone scripts, not pytest; the repo's convention beats any default here.

## How much testing

Size the testing to the consequence of the change (root `AGENTS.md` table). Do not run more than the tier asks for.

| Tier | Run before claiming done |
|---|---|
| Low (docs, formatting, mechanical) | Lint, plus the tests mapped to the touched files |
| Medium (bounded deterministic logic) | Low, plus every test file that imports or exercises the touched module |
| High (matching, identity, authority) | Medium, plus the full suite, plus mutation on the touched High paths only |
| Critical | High, plus independent re-derivation and full-population evidence |

`python3 scripts/plan_tests.py --run` runs the floor tier's checks; the land gate is always the full suite. Mapped tests come from `harness.toml` `[[mapped_tests]]`. Full suite: `./scripts/run.sh --tests`. Mutation: `mutants/runner.py --only <id>` per touched path, never the whole corpus (`docs/mutation-testing.md`). A check that was skipped or timed out is reported as skipped, not passed.

## Loop for a bug or feature

1. Pass the authoring gate in `references/audit-test-value.md` before writing the test.
2. Write the test, run it, and see it fail for the intended reason (not an import or syntax error). A test that passes before the fix proves the mock, not the fix.
3. Write the minimum change that passes it. Run the tier's checks.
4. Refactor with the tests green; rerun after each step.
5. Commit the test and the change together.

## Test smells

A test that cannot fail when the logic changes is wrong. Fix or reject these on sight: asserting private state, mocks that implement the asserted behavior, `!= None` or `> 0` as the only assertion, `sleep` for synchronization, order-dependent tests, skipped tests with no reason, happy-path-only coverage, expected values produced by the code under test.

## Auditing or pruning tests

Use `references/audit-test-value.md`. Delete only with the candidate evidence it lists. Keep tests that enforce a contract another component depends on.

## Verifying completion

Run the commands, read the output, and report command, status and count. Check that new code is wired into its callers and that real data reaches it; an unused file is not a feature. Reuse an earlier passing result only if it covered the current code and nothing changed since.
