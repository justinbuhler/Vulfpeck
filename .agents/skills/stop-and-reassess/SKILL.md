---
name: stop-and-reassess
description: Use when a check or fix has failed and a retry is coming, the same error keeps returning, edits keep touching the same lines, or a retry would repeat the last attempt unchanged - decides retry, decompose, prune or escalate and stops loops.
---

# Stop and reassess

A loop costs more than the task. The stop rule is owned by AGENTS.md "Stop and reassess": a failure fingerprint seen 3 times, meaning the original plus two failed repairs, means stop; two consecutive edit-and-run cycles with no new passing check means stop. Cite that rule elsewhere; do not restate it.

Below the stop line, pick one strategy per failed verification:

| Strategy | Use when |
|---|---|
| Retry | The error is specific and actionable (typo, missing import, wrong argument). The retry must use the failed attempt's error text. |
| Decompose | A retry failed and the task has independent parts. Each part gets one attempt and is not decomposed again. |
| Prune | The goal is already met or the step is not needed. Write why. "I could not do it" is escalation, not pruning. |
| Escalate | The error is architectural or vague, the stop rule has fired, or a decomposed part failed. |

## Before every retry

Name what is different from the last attempt: the error, the root cause, and the new strategy with its file and line scope. If the answer is "nothing", do not retry; escalate.

## Loop signals

- the same error again;
- edits to the same lines in consecutive attempts;
- fix A breaks B and fix B reverts A (find the shared file both depend on and change that first);
- lint and build alternating (get the build green first, then lint, then confirm the build is still green);
- context past about 70%: write the state to a progress file and compress before continuing.

## On stop

Report: what failed, what was tried (one line per attempt), the suspected root cause, and two or three concrete options with a recommendation. If the failure may be the approach and not the code, say so. Do not edit the user-approved plan; adapt in memory and log each deviation.

Details and the written format are in `references/loop-detail.md`.
