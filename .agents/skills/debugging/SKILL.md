---
name: debugging
description: Use when a test, command or behavior fails and the cause is not yet known, before proposing a fix, and when investigating a failed or stuck agent session after the fact.
---

# Debugging

Find the cause before changing code. A fix without a reproduced cause is a guess.

## Procedure

1. **Reproduce.** Get the smallest command that fails, and run it. Save its output. If it is intermittent, find what varies (order, time, environment, shared state) before anything else.
2. **Read the evidence.** The full error and stack, not the last line. Note the first failure, not the loudest.
3. **Isolate.** Shrink the input or the code path until removing anything makes it pass. Check the recent change with `git log -p -- <path>` and `git bisect` when the failure is new.
4. **One hypothesis.** State it as a prediction: "if X is the cause, then Y will show at Z". Test the prediction with a print, a probe or a one-line experiment. Change one thing at a time.
5. **Fix at the cause.** Fix the shared code once, after grepping every caller. Do not patch the symptom at the call site.
6. **Prove it.** Add a regression test that fails before the fix and passes after (see `testing`). Run the tier's checks.

## Guardrails

- Two failed fixes on the same failure: stop and use `stop-and-reassess`.
- Label an unreproduced root cause as an unverified hypothesis, never as a finding.
- Never suppress or widen an error to make a check pass.

## Post-mortem of a failed or stuck session

Read-only. Never change state, even if asked to fix; report and recommend. Method and detector table: `references/forensics.md`.
