---
name: building-with-jev
description: Use when writing, wiring or improving code that calls Jev, TypeSafe's System One judgment model - choosing what Jev decides, writing questions and state, sizing and pricing requests, setting thresholds, or debugging wrong answers and 429/503 failures.
---

# Building with Jev

Jev reads one `state`, answers every question in the request independently and in parallel, and returns a probability over answers you defined. Code decides; Jev judges; an LLM writes only when the output is a new artifact.

Read the live docs first: `https://docs.typesafe.ai/llms.txt` (append `.md` to a page path). They are the source of truth for the API, models, limits and prices. Pin the model version and recheck the limits when it changes.

## Three tiers

| Tier | Use for |
|---|---|
| 1. Program | Anything computable: search, parse, count, diff, run a command, validate |
| 2. Jev | A judgment over evidence in hand: classify, gate, score, triage, choose from a fixed set |
| 3. LLM | A new artifact: code, prose, a plan, a novel diagnosis |

Use the lowest tier that does the job. A Score, a Choice or a yes/no is never generative. Programs produce the evidence, Jev judges the residual the programs leave undecided, and an unavailable or invalid answer is `unknown`, never `no`.

## Build procedure

Build one system at a time. It is done when it has labeled cases, a measured score, a measured cost per run, and an action that uses the answer.

1. State the decision, the unit it applies to, and the action code takes per answer. A person can label one unit in under a minute.
2. Build the grader: human-confirmed labels, a frozen train/dev split, and a sealed group-disjoint heldout. Agent labels are diagnostics, not ground truth.
3. Discover signals in code, write a deterministic policy, and score it on dev. The units it is unsure about are the residual.
4. Design the request for the residual only: bounded, labeled state; one atomic question per judgment; all independent questions in one request.
5. Price the run in tokens per run and per second, with retries and concurrency (`references/state-and-budget.md`).
6. Smoke run on three units, then a dev sample. Failed calls must be zero and answers must parse.
7. Score rules alone, Jev on the residual, and the combined system, with calibration.
8. Improve one lever at a time: evidence in state, decomposition, criteria wording, thresholds (`references/improve-and-calibrate.md`).
9. Score the heldout once. Run any action-changing gate in shadow mode before it can act.

## Primitives

| Primitive | Ask when | Returns |
|---|---|---|
| Noul | A clean yes/no | `noul` in [0, 1] |
| Choice | One of a known unordered set | `choice`, `probabilities`, `confidence` |
| Score | A position on a spectrum described in steps | `score` (a weighted mean), `probabilities` |

Shapes and answer semantics: `references/primitives.md`. Question wording: `references/question-design.md`.

## Rules that prevent most failures

- State exact conditions; Jev reads scoping words and negations literally. One narrow judgment per question.
- State is evidence, not instructions. Bound every field, label sections, and put rules in `instructions` and `criteria`. Jev does not treat state as hostile, so keep untrusted text separate.
- Code does all counting, arithmetic and date comparison.
- A request holds 64,000 tokens (state plus every question), and the account has per-second limits. A request that fits can still be a run that does not; cascade large sets instead of sending full detail for every unit.
- Retry 429 and 529 (503 through a gateway) with jittered exponential backoff, and give each run a retry budget. Retries multiply cost.
- Confidence measures concentration, not permission to act. Derive thresholds from labeled data and action costs.
- Keep a record per call (payload hash, answer distribution, versions); retune thresholds from stored answers at zero cost.
- A live-call claim needs a saved request and response on disk (root `AGENTS.md`). Mutation sizing and recorded replay rules are in `docs/mutation-testing.md`.

## References

| Signal | Load |
|---|---|
| Request or response shapes, reading `score` | `references/primitives.md` |
| Writing instructions, criteria, levels, examples | `references/question-design.md` |
| Size limits, batching, rate limits, untrusted state | `references/state-and-budget.md` |
| Fan-out, cascades, routing, composite scores | `references/composition-patterns.md` |
| Wrong answers, low confidence, calibration, failures | `references/improve-and-calibrate.md` |
| Gate design and thresholds | `references/decision-card.md` |
