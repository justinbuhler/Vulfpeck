# 0021. Evals place Jev: off the test plan, a deterministic intake gate

Status: proposed
Date: 2026-10-26
Components: scripts/plan_tests.py, scripts/task_loop.py, scripts/jev.py, .claude/hooks/loop_guard.py, harness.toml, scripts/eval_consequence.py
Supersedes: 0016

## Context

ADR 0016 gave Jev three jobs, and ADR 0018 a fourth:

- `plan_tests.py` asked `jev.consequence` for the tier, a floor from the `[tiers]` globs that Jev could raise and never lower;
- `loop_guard.py` appended `jev.reassess()` advice to a block;
- the task loop broke a priority tie with a Jev choice over issue titles.

The decision at the time was made without measurement. `evals/consequence/` now holds the measurement: every merged pull request up to #37, 27 cases reduced to what `plan_tests.change()` sees, rated by two model labellers (agreement 21 of 27, Cohen's kappa 0.69) and adjudicated into a `gold` tier per case. The labels are model-made and not yet reviewed by the owner. `python3 scripts/eval_consequence.py` scores the code floor, and Jev where a recorded answer exists, against gold. Derived twice (the eval, and a separate script over the raw files):

| Floor | Exact | Under (gold above floor) | Over (floor above gold) |
|---|---|---|---|
| `[tiers]` with `critical = []` | 11 of 27 | 6 | 10 |
| plus `critical = ["scripts/task_loop.py"]` | 15 of 27 | 0 | 12 |

- All six under-rated cases are Critical and touch `scripts/task_loop.py`, the unattended-merge path. One glob fixes all six and leaves none.
- Jev can only raise a tier, so its headroom on this set is the under count: 6 before the glob, 0 after it. It cannot lower the 12 over-rated cases (test-only, CI-bump and instruction-text changes rated High) by design.
- No Jev key exists in any environment this was run in, so `plan_tests` always planned at the floor. The answer Jev would have given has never been recorded.
- `loop_guard`'s `reassess` advice needs the same key, can never unblock (the owner or the runner resets), and puts a network call inside a gate. It produced no output in any run here.
- The tie-break orders the queue by a model answer, and nothing measures whether that order helps.

27 cases are a sample: they show where the floor fails, not how often.

## Decision

**The floor is the tier.** `harness.toml` `[tiers] critical = ["scripts/task_loop.py"]`. `tests/test_eval_consequence.py` runs the eval offline on the shipped cases and labels with `--gold gold` and fails when any case is under or when the gold-labelled count is not 27, so a change to `[tiers]` that under-rates a labelled pull request fails the suite.

**Jev leaves the test plan.** `plan_tests.py` plans at the floor, or at `--tier` (refused below the floor unless `--below-floor`). `tier_source` is `floor` or `given`; the `consequence` key and the referral note are gone. `plan(..., mutation=False)` and `--no-mutation` leave the mutation command out and record `mutation: skipped by --no-mutation (review owns it)` in `skipped`. `floor_tier` and `TIERS` stay in `scripts/jev.py`.

**Jev leaves the loop guard.** `loop_guard.py` no longer asks Jev. A trip message is the rule quote, the attempts and the way out. `jev.reassess`, its constants and its command-line subcommand are removed, with `jev._ask`, whose only caller was the tie-break.

**The task loop chooses without Jev, and gates intake without it.**

- The next issue is the highest `[loop] priority` label; a tie goes to the lowest issue number. This amends the tie-break in ADR 0018.
- Right after the move to `running`, and before any fetch, provision or worker, `execute()` refuses an issue whose body has no `Done when` section. A section is a line that begins with `Done when` (any case, a word boundary after `when`), after optional Markdown heading marks, a list marker or bold, followed by non-blank text after a colon or by a non-blank line before the next heading. A refusal comments on the issue that it needs a `Done when` section and was not dispatched, and ends the task `blocked` with reason `needs-info: no Done when section`. `--dry-run` shows the refusal and the comment.
- Jev is not asked at intake. There is no labelled intake data, so nothing can say whether a model reads an issue better than a regex; the regex is the baseline any model must beat.

**Jev stays where it is scored or advisory.** `consequence` is scored by the eval; `intent_alignment` is advisory in `scripts/scope.py`. The record and replay machinery stays, because the eval replays from it.

**The runner keeps Jev records out.** `mutants/runner.py` gives every test run a temporary `JEV_RECORD_DIR`, beside the `HARNESS_EVENTS_LOG` backstop, so a mutant run neither writes to the real record directory nor replays another run's records.

## Consequences

- Over-rating is unchanged: 12 of 27 labelled changes are planned above their gold tier, which costs extra checks. Lowering a tier needs a rater that code can trust; none exists. The eval is the test for one: it must reduce the over count without adding an under case.
- The gold set gates `[tiers]` only for the cases it holds. A new kind of change that the globs under-rate is found when it is labelled, not before.
- The gold labels are model-made. If the owner re-labels a case, the gate follows the new label.
- To bring Jev back into the plan, a key must exist, `python3 scripts/eval_consequence.py --collect` must record answers, and the final tier must be closer to gold than the floor on cases the floor gets wrong; that is a new ADR.
- The intake gate checks that a `Done when` section is present, not that it is any good. An issue whose author words it another way is refused, and the owner must edit it. An edit after the `ready-for-agent` label event is blocked by the stale-text check, so the owner re-applies the label and resets the task. Two refused issues in a row end a `run` (the existing rule for two consecutive blocked tasks).
- `plan_tests.change()` still returns diff stats, changed symbols and commit subjects, now used only to build eval cases.
- The worker's checker and the loop's verification skip mutation (ADR 0020); review owns it.

## Alternatives

- Keep Jev raise-only in the plan. Rejected: its headroom on the labelled set is zero once one glob is added, and it is a network call in the path of every plan.
- Let Jev lower a tier. Rejected: `docs/mutation-testing.md` has code decide and Jev only judge, and no measurement shows a lowered tier would be right.
- Ask Jev at intake whether an issue is actionable. Rejected for now: no labelled intake data exists to score it against the regex.
