---
name: grill-jev
description: Use when a plan, spec, design or code change needs its hidden problems found before work starts or ships - "grill this", "find holes", "stress test this", "is this ready" - by sending a battery of up to 50 questions generated from the artifact itself to Jev.
---

# Grill Jev

Generate questions from the artifact (a plan, spec, design, diff or any topic), send them to Jev with the artifact as state, and report the high-signal answers. Questions come from the artifact, not from a fixed list, so they target its actual gaps. Read `building-with-jev` first for the primitives and request rules.

## Steps

1. **Read the artifact** and note audience, system context, the decision it supports, and the depth wanted. These sharpen the questions.
2. **Write the battery** as JSON to a file under `tmp/`: 15 to 20 questions for a simple artifact, 25 to 35 medium, up to 50 complex.
   - Noul for "is this true or absent", with `true`/`false` criteria where the boundary is subtle.
   - Choice for risk level or reversibility, Score for completeness or realism.
   - Name the specific step, system or claim in each question ("does step 4's migration define a rollback safe under live traffic" beats "is there rollback").
   - When the artifact mentions a risk, ask more about it: acknowledging a risk is not a mitigation.
3. **Cover** completeness, feasibility, risk and failure modes, scope, verification, consistency, reversibility, security, and cost and throughput. Skip a category only when the artifact has nothing in it.
4. **Send and read.** Split the battery by estimated tokens (the artifact is resent with every request). Report answers above a signal threshold with `file:line` or step references and a suggested action, then a readiness summary.
5. **Calibrate.** If findings are generic, add context (audience, constraints) and rerun once.

A plan that calls Jev or any metered API is also graded on arithmetic: calls, tokens per run and per second against the documented limits, fan-out, concurrency and retry policy. "Each request fits" does not show the run fits.

## Output

```
GRILL FINDINGS - <mode> - <n> questions
HIGH SIGNAL:
  [category/question_id] value - what it means
    -> suggested action
CATEGORY SUMMARY
OVERALL READINESS: <0-3> - one line
```

Jev proposes; a person or a threshold in code decides. Without a Jev transport, run the questions through a text model as a labeled fallback and say so.

Example battery shapes and a fallback battery: `references/question-battery.md`.
