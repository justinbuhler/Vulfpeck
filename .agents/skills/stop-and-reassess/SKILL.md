---
name: stop-and-reassess
description: Use when a check or fix has failed twice, the same error keeps returning, edits keep touching the same lines, a retry would repeat the last attempt unchanged, or the repair budget is spent - decides retry, decompose, prune or escalate and stops loops.
---

# Stop and reassess

A loop costs more than the task. After any failed verification, pick one strategy, spend from a fixed budget, and escalate when it runs out.

## Budget

Two repair attempts per task. Three identical failures in a row (same error text or same failing test) is a loop regardless of budget: stop on the third.

| Strategy | Costs | Use when |
|---|---|---|
| Retry | 1 | The error is specific and actionable (typo, missing import, wrong argument). The retry must use the failed attempt's error text. |
| Decompose | 1 | A retry failed and the task has independent parts. Each part gets 1 attempt and no further decomposition. |
| Prune | 0 | The goal is already met or the step is not needed. Write why. "I could not do it" is escalation, not pruning. |
| Escalate | 0 | The error is architectural or vague, the budget is spent, or a decomposed part failed. Mandatory when the budget is spent. |

## Before every retry

Name what is different from the last attempt: the error, the root cause, and the new strategy with its file and line scope. If the answer is "nothing", do not retry; escalate.

## Loop signals

- the same error three times;
- edits to the same lines in consecutive attempts;
- fix A breaks B and fix B reverts A (find the shared file both depend on and change that first);
- lint and build alternating (get the build green first, then lint, then confirm the build is still green);
- context past about 70%: write the state to a progress file and compress before continuing.

## On stop

Report: what failed, what was tried (one line per attempt), the suspected root cause, and two or three concrete options with a recommendation. If the failure may be the approach and not the code, say so. Do not edit the user-approved plan; adapt in memory and log each deviation.

Details and the written format are in `references/loop-detail.md`.
