# Loop handling detail

## Attempt log

Keep one line per attempt in the task's progress file:

```
ATTEMPT 1 - changed channel direction in consumer.go - FAILED: 3 direction errors
ATTEMPT 2 - changed producer.go to match - FAILED: build broken in producer.go
ATTEMPT 3 - reverted producer, changed consumer - FAILED: original errors returned
LOOP: fix in one file breaks the other. Shared type in types.go must change first.
```

Detect repeated errors in that file with `rg "FAILED:" <progress file> | sort | uniq -d`.

## Root-cause note (written before escalating)

```
Blocker: <short name>
Attempts: <n>/<budget> failed
Root cause: <specific, testable, grounded in evidence>
Required strategy: <ordered steps, each with a verification command>
Needs: <the decision only the owner can make, if any>
```

## Compilation first

For any lint or style task, make the build or import check pass first, run the lint, then confirm the build still passes. Success criteria are all three, in that order.

## Re-dispatch

A re-dispatched task carries (1) the specific error from the prior attempt, (2) the root cause, (3) the new strategy with file and line scope. A bare "try again" is not a dispatch.

## Decision tree

```
Verification fails
  |-- specific and actionable error --> retry with the error text
  |       |-- fails again --> decomposable? decompose
  |       |                   goal already met? prune with reason
  |       |                   else escalate
  |-- vague or architectural --> escalate now
  |-- budget spent --> escalate (mandatory)
```

## Deviation log

Record each repair action in the task summary:

| Task | Strategy | Error | Outcome | Attempts used |
|---|---|---|---|---|

## Rationalizations that mean stop

| Thought | Action |
|---|---|
| "One more retry should fix it" | The budget exists to prevent this. Escalate. |
| "I will skip it, it is probably fine" | Prune needs a stated reason the goal is met, or escalate. |
| "The error is vague but I will try anyway" | Vague errors do not respond to targeted fixes. Escalate. |
| "I will remove the task from the plan" | The plan is approved scope. Prune in memory and log it. |
| "This part needs more attempts" | Parts get one. Escalate. |
