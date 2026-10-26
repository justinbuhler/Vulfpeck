# Mutation testing and model judgments

Mutation testing checks whether the tests would notice a bug. The harness plants one small deliberate bug (a *mutant*) in a copy of the code, runs every test, and records whether a test fails. A failing test means the mutant was *killed*. A mutant that survives marks behaviour no test protects.

It lives in `mutants/`, plus one tracked log, `mutants/mutation_ledger.jsonl`. It has four stages: **size, allocate, write, run**. Sizing is where a model judgment (Jev) is used, under the rules in the next section.

Status in this repository is marked on each stage: **in place** or **to build**. The full design was proven in the project this harness came from. This document is the target that later work implements.

## Model judgments: the shared pattern

Every script that asks Jev, TypeSafe's judgment model, follows these rules. They apply to any future model judgment in the harness, not only mutation sizing.

- **Pinned model.** `MODEL = "jev-1.13.0"`, sent to `https://api.typesafe.ai/v1/systemone` through the repository's own small HTTP helper. No SDK or gateway sits in between.
- **Typed questions.** Jev answers a **Choice** (pick one option, with a probability for each) or a **Noul** (a yes/no probability). It never writes free text that code then parses.
- **Code decides.** Code builds the shortlist, does the arithmetic and applies fixed thresholds. Jev only judges. It never sums scores, sets a tier or approves anything.
- **No live call by default.** A script contacts Jev only with `--collect`. The gate, CI and the lander never pass that flag, so they rebuild from recorded answers and stay offline.
- **Recorded and replayable.** *In place for `scripts/jev.py`.*
  - Each request body is canonical JSON, stored as `<sha256>.json` in `JEV_RECORD_DIR`, or else `<git common dir>/harness/jev/`.
  - A record holds the redacted request and the answers, never the key or a header.
  - A payload whose record parses and validates is replayed and never sent again. Replay re-applies the current policy to the recorded answers.
  - `JEV_OFFLINE=1` turns a miss into an error the caller falls back on.
  - A failed call is `ERROR` ("unknown"), never "no".
  - `scripts/eval_consequence.py` replays the labelled consequence cases offline.
  - `jev.py` sends the alias `jev-latest`, so a replay reproduces the recorded answer, not what the alias would answer today.
  - The standalone `--replay FILE` command is still to build.
- **No authority.** Every output is a proposal, reading or observation. Only a person, or an approved review, turns it into a decision.

These are the same properties the rest of the harness demands of a gate: deterministic, replayable, and never deriving its verdict from the thing it checks (Rule 11). A live model call inside `./scripts/gate.sh` would break all three, so none is allowed there.

## 1. When mutation testing is required

The consequence table in root `AGENTS.md` rates a change by what it can do to the project's conclusions (answers, records, decisions and reported values), never by what a file is named:

| Consequence | Example | Required review |
|---|---|---|
| Low | Docs, formatting | Tests plus a standard review |
| Medium | Bounded deterministic logic | Tests plus a fresh review |
| High | Matching, identity, or logic that sets an answer, record, decision status or reported value | Mutation testing plus adversarial review |
| Critical | Human-authority decisions, destructive writes, broad reconciliation | Independent derivation plus full-population evidence |

A change that adds a reported value (a new count in a report, say) is High.

## 2. Size: `mutants/plan.py` (to build)

Code counts, Jev judges, code decides, and a person confirms.

**Code counts the mutants from the diff:**

- one per changed decision point (a condition, comparison, `and`/`or`, `not`, assertion or return), at most one per line, skipping print and logging lines;
- one per invariant that a changed data file (a schema or decision record) claims, listed in a context file. A data file with no stated invariants is a question for the author, never a count of zero. So is a changed shell or JavaScript file, which the counter cannot read.

**Jev answers two questions:**

- a Choice for the change's consequence level (Low, Medium, High or Critical);
- a Noul for "can this change alter an answer, record, decision or reported value?"

Jev sees file paths, kinds, counts, changed function names, the files that read each changed file, and the author's four-part description: what it is, what it is about, what it is for, and who decides. It never sees file contents.

**Code applies the acceptance rule:**

- High or Critical at probability **0.6** or above is accepted. A higher level only adds work.
- Low or Medium at **0.8** or above is accepted only if the alters-values Noul is **0.35** or below, and only on condition that the independent reviewer confirms the stated context.
- Anything else is referred to the owner.
- A transport or provider failure is `ERROR`, and Jev alone never waives mutation testing.

**Exit status:** 0 when the harness decided (`ACCEPTED`) or on a dry run; 3 when the owner must act (`REFERRED`, `QUESTION`, `ERROR`); 1 when the API key is missing.

**Worked example.** A change with 12 decision points and 2 schema invariants is sized at 14 mutants. Jev says Medium at 0.83 but alters-values at 0.65. Medium cleared 0.8, but the Noul is above 0.35, so the plan is **referred**. The owner rates it High, because it adds a reported value.

## 3. Allocate: `mutants/ranges.json` and `mutants/registry.py` (to build)

- Each mutant gets an `M###` ID, which must sit inside exactly one named range.
- A branch claims its own range starting at `next_free`, and advances `next_free` past it.
- `python3 mutants/registry.py` fails on a duplicate ID or one outside every range.
- **Why:** parallel branches that number mutants by list position collide.

## 4. Write the mutants (patches in place; probes and specs to build)

There are three kinds:

- **Patch** (`mutants/patches/M###.patch`): a header plus a small diff. In place here; M001 to M013 guard the harness's own controls. For example:

  ```
  # mutant_id: M003
  # target: scripts/gate.sh
  # operator: swallow-status
  # behaviour: a lint failure fails the gate
  ```

  The filename is only the ID. The operator stays inside the file, because a test author writing a spec for that gap must not learn which mutation it is.
- **Probe** (`mutants/probes/M###.py`): breaks data rather than code (truncates a record, drops a field) and requires the checks that read it to report FAIL.
- **Spec** (`mutants/specs/M###.py`): a test fragment written blind against the behaviour. It must pass on clean code and fail on its own mutant. Specs are then joined into the real test files.

## 5. Run: `mutants/runner.py` (partly in place)

`runner.py` is the only supported way to run the corpus.

| Property | Here today | Target |
|---|---|---|
| Kills recorded per assertion (`FAIL <name>` lines); an import-crash-only kill is a gap | in place | same |
| Ledger row per mutant: `mutant_id`, `killed`, `failing_spec_names` | in place | adds `kind` and `mode` |
| Refuses a dirty tree; byte-identical revert check after each mutant | in place | superseded by isolation |
| **Isolation:** clone HEAD into a temp directory outside every checkout; never touch the working tree | **to build**; mutants are applied in the checkout | required |
| **Machine-wide lock:** one run at a time | **to build** | `~/.cache/<project>-mutants/` |
| **Baseline twice:** run the suite twice clean; a check that differs is quarantined and can't be the only killer | **to build** | required |
| **Fast mode** (`--function`): run only a declared subset; its rows are marked `fast` and don't count for a High change | **to build** | uses `harness.toml` |
| `--verify-specs`: each spec fails on its own mutant | removed (needs specs) | returns with specs |
| `--only M001 M003`, `--report`, `--round` | in place | same |

Example ledger row:

```json
{"kind": "mutant", "mode": "full", "mutant_id": "M003", "killed": true,
 "failing_spec_names": ["tests/test_harness_config.py::a lint failure fails the gate with lint's status and skips the suite"]}
```

## 6. Pass bar for a High change

- A full-suite ledger row for every mutant the plan sizes.
- Each surviving mutant is either killed by a new test, or recorded as *equivalent* (no test could ever see it), with the reason.
- **Prefer a real change to an equivalent mutant.** Example: deleting an `isinstance(value, bool)` guard outright is equivalent when a regex already rejects `"True"`. Change it to `isinstance(value, int)` instead, a real change that a test can kill.

## Known weak spot

Running in a fresh clone means the clone needs every dependency the suite imports. A test that cannot import fails at baseline and is quarantined, and then any mutant whose only killer is in that test wrongly looks like a survivor. The runner should report baseline-red scripts by name, and a High change's mutants must not rely on them.

A ledger row lists every check that failed under the mutant, including checks that fail only because an earlier one left different state behind (a cascade). The kill is real if any listed check targets the mutated behaviour; read the first name, not the count.

## How this connects to the land gate

The consequence tier and review receipt (`HARNESS.md`, open gap 10) take their tier from `plan.py`'s recorded plan, not from path globs alone. `land.sh` reads the recorded plan, and the ledger rows for this candidate. It never calls Jev itself.
