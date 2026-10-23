# Test value: authoring gate and audit

One value bar for three jobs. The authoring gate checks every new or changed test when it is written. Audit mode sweeps existing tests for low-value, implementation-coupled or duplicate tests. Campaign mode prunes one subsystem's test surface. Optimize for confidence, not deletion count.

Test layout: `tests/` for shared checks, `<folder>/scripts/tests/` beside folder code. Each file is a standalone script (see `tests/AGENTS.md`).

## Authoring gate

Answer all four before adding a test. A missing answer means do not add it yet.

1. What observable behavior, invariant or independent contract does it protect?
2. What credible regression makes it fail?
3. Why does existing coverage not already catch that failure? Each contract has one primary test owner at the strongest boundary. Another layer needs its own distinct risk, such as a subprocess, file I/O or hook-event failure the owner cannot reach. Prefer a new case in an existing table of cases over a near-duplicate test.
4. Does it need a production seam (module global, flag, wrapper, importable `_private`) that no production caller needs? If yes, test at the real boundary instead: the script's CLI, the hook's stdin/stdout contract, or the module's public function.

Then check the test against the junk patterns below. A match fails the gate unless the retention bar names the contract it independently guards. A test that breaks under a behavior-preserving refactor asserts implementation; rewrite it at the owning boundary.

A bug regression test must fail on the pre-fix code for the intended reason and pass after the fix, at the owner boundary. One regression at the owner boundary covers the bug; do not replay it at every layer it crosses.

## Junk patterns

- assertion-free coverage probes;
- self-comparisons and identity copiers;
- copied fixtures, inventories or export lists that mirror a directory;
- exact source, import or string greps;
- private predicate or call-shape tests duplicated at real boundaries;
- duplicate invocations of the same contract;
- per-script replays of a shared helper's tests;
- tests whose only purpose is preserving test-only globals, wrappers or importable privates;
- dead production code whose only callers are tests;
- expected values produced by the helper or renderer under test (Rule 11, the relocating oracle);
- mocks that implement the asserted behavior, or one identical mock standing in for different APIs;
- fixtures that supply the result the owner should produce, or persistence asserted against a store the path never writes;
- capability tests that restate declared flags or frontmatter instead of exercising the behavior;
- negative controls that pass for an unrelated reason, such as a denial from a different guard;
- names that promise more than the input exercises, such as "blocks the write" with no assertion on the block decision.

## Retention bar

Keep a test that independently enforces a contract others depend on:

- hook event contract (stdin JSON in, decision out, exit codes);
- script CLI flags, exit codes and output format that other tools or CI read;
- defaults, file layout, or byte-exact output a consumer parses;
- security gates and path guards;
- call ordering when order is observable;
- regressions with a credible failure mode;
- source inspection when it is the cheapest independent guard and survives an identifier-only refactor;
- a retained test that fails on the baseline: treat it as a possible product bug, reproduce it and fix the owner instead of deleting the test.

Static or slow is not a deletion reason.

## Audit procedure

Keep discovery read-only. Before judging a candidate, read the whole test and its production owner: entry point, callers, callees, overlapping tests, CI wiring in `.github/workflows/`, and `git log`. Prefer a few high-confidence candidates over a large speculative inventory.

Record every field before editing. A missing field means the candidate is not ready for deletion:

- exact test name and location;
- what failure it can actually detect;
- non-test callers of the covered seam;
- stronger remaining owner-boundary proof, or why none is needed;
- history and the reason the test or seam exists;
- production or test-support deletion it unlocks;
- risk and the focused validation command.

Edit one coherent owner-boundary batch. Delete obsolete test-only globals and dead production paths instead of keeping aliases. Prefer net-negative production LOC. Do not add replacement tests that restate the same implementation.

## Validation

1. Run the owner test and its siblings.
2. For a removed source grep, run the script or validator that owns the real contract.
3. Run `uvx ruff check .` and `git diff --check`.
4. For High paths, run the mutation runner on the touched paths.
5. Run the full suite (`./scripts/run.sh --tests`) for High and above.
6. Report production LOC and test LOC separately (`git diff --numstat`).

## Handoff

Report root cause and removed categories, retained false positives and why, the proof actually run, LOC split, and named follow-ups.
