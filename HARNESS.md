# Agent harness

A per-repository harness for agent-operated development. Every rule it states is enforced by something other than prose: a test, a hook, or the lander.
It is project-agnostic. A repository describes how it builds in `harness.toml`; the controls read that file instead of spelling commands themselves.

## Layers

| Layer | Files | What it enforces |
|---|---|---|
| Governance contract | `AGENTS.md`, `CLAUDE.md`, `tests/AGENTS.md` | ORCH-01..12 delegation rules, the consequence-to-review table, Rule 11 (relocating oracle), test conventions |
| Project config | `harness.toml`, `scripts/harness_config.py`, `scripts/harness_env.sh` | One definition of setup, lint, test, untracked inputs, watched controls and mapped tests; unknown keys are errors |
| Gate and landing | `scripts/gate.sh`, `scripts/run.sh`, `scripts/land.sh`, `.githooks/*`, `.github/workflows/tests.yml` | No commits or merge commits on `main`; lint at commit; the same gate at land and in CI; compare-and-swap of local `main` |
| Claude Code hooks | `.claude/settings.json`, `.claude/hooks/*.py` | Model routing on `Agent` calls, commit-landed verification, mapped test on edit, blast-radius advisory, CodeGraph freshness |
| Agent and review tooling | `.claude/agents/*`, `.claude/skills/land`, `.claude/workflows/*`, `.claude/harness/agent_harness.py`, `mutants/` | Independent re-derivation, live-API proof, per-task worktrees with a serialized commit queue, mutation testing |

### One gate, three callers

`scripts/gate.sh` runs `commands.setup`, `commands.lint`, then `scripts/run.sh --tests`, stopping at the first failure and exiting with its status.
`land.sh` runs it on the merged tree, CI runs it on every pull request, and `.githooks/pre-commit` runs `gate.sh --lint-only`.
None of the three spells a command, so they cannot drift apart.

`run.sh` runs `commands.test`. When `tests.count_pattern` is set, a run must also report a non-zero number of checks: a test command that exits 0 having run nothing fails.

### Hooks

| Hook | Event | Effect |
|---|---|---|
| `require_agent_model.py` | PreToolUse `Agent` | Denies a call with no `model` or `inherit` (fork exempt) |
| `model_router.py` | PreToolUse `Agent` | Denies a model that does not match the `subagent_type` tier (Explore: Haiku; `*-engineer`/simplifiers: Sonnet; `reviewer-*`, Plan, dual-derive, pr-review-toolkit: Opus or Fable) |
| `blast_radius_advisory.py` | PreToolUse `Edit\|Write` | Advisory only. When the edit touches a file in `hooks.watched_files` or `hooks.watched_dirs`, it prints which tracked files also spell the literals being changed |
| `verify_commit_landed.py` | PostToolUse `Bash` | Blocks when a `git commit` printed no `[branch sha]` or HEAD is not that sha (catches index.lock races and exit codes masked by a later command) |
| `test_for_edited_module.py` | PostToolUse `Edit\|Write` | Runs the test `[[mapped_tests]]` maps to the edited file and exits 2 on failure or on an unreadable `harness.toml` |
| `codegraph_freshness.py` | UserPromptSubmit / SessionStart | Reports CG-01..04 and syncs the index on CG-02, in the main checkout only |
| `readonly_guard.py` | SubagentStart / SubagentStop | When an agent matching `[roles] read_only` starts, snapshots the main checkout (HEAD, checked-out branch, local `main`, every ref, the local git config, `info/exclude`, `.git/hooks`, the worktree list, full status) and records the snapshot's hash in the decision log. At its stop, any difference blocks the stop once with the changes, then lets it stop and tells the parent. A branch checked out in another worktree may only advance, and its advance is reported; land a reviewed candidate with `land.sh --expect <reviewed sha>` so an advance is never landed unreviewed. A snapshot that no longer matches its logged hash, or a missing, broken or doubled log, blocks. A stop with no snapshot is reported as unverified |
| `queue_guard.py` | PreToolUse `Bash` | Denies `git commit`, `commit-tree` and `update-ref` inside an `agent_harness.py` task worktree and points at `enqueue`. Early feedback only: the lander refuses any request whose worktree HEAD moved outside the queue |
| `session_guard.py` | Stop / SessionEnd | At Stop, when HEAD or the working tree changed since the last check (or lint failed last time), runs `commands.lint`; a failure blocks the stop once with the output tail, then lets the session stop with a warning. Uncommitted paths and commits not on local `main` get a one-line notice when that state changes, and never block. At SessionEnd, records branch, HEAD, dirty and unlanded counts in the decision log |
| `adr_gate.py` | PreToolUse and PostToolUse `Edit\|Write` | In the session's own project only, denies creating (by Write, or Edit of a missing file) a file matching `adr.require_for` unless a proposed or accepted ADR in `docs/adr/` names it; exits 2 when an ADR just written does not parse. The land enforces the same coverage for files created any other way (`tests/test_adr.py`) |
| `harness_doctor.py` | SessionStart | Advisory. Reports missing prerequisites (Python 3.11+, uv, rsync, flock/lockf, `core.hooksPath`) and user-level hooks that can undercut the harness: SubagentStart context injection, PreToolUse Bash hooks that can deny `git push` or `gh pr` |

**Failure direction.** Claude Code lets a tool call proceed when a hook exits with anything other than 0 or 2, so a crashing gate looks like a gate with nothing to deny. Every gate's registered command therefore ends in `|| { ...; exit 2; }`, turning a crash or a missing file into a block; advisory hooks end in `|| true` so they never block. `tests/test_harness_controls.py` keeps its own inventory of every registration, runs each command exactly as registered, gives each gate a deny and an allow canary, and proves both failure directions by swapping in a crashing script. A hook that exceeds its timeout still fails open; keep gates fast (the canaries assert under half the timeout).

### Architecture decisions

`docs/adr/NNNN-slug.md`, one per decision, in the repository and reviewed with the code (ADR 0002). `scripts/adr.py check` validates the status, date, components, the three required sections, unique numbering and two-way supersession links. It fails when a tracked file matching `adr.require_for` is named by no proposed or accepted ADR, or when an accepted ADR names files that no longer exist. The suite runs it, so the land refuses an undocumented component. Nothing here depends on a user-level toolkit, and there is no bypass variable; an exemption is an `adr.exempt` entry in a reviewed commit.

### Decision log

`scripts/harness_events.py` appends one JSON line per gate decision to `<git common dir>/harness/events.jsonl`: every `Agent` call (`require_agent_model`, `model_router`), every commit `verify_commit_landed` checks, and every land outcome (`landed`, `noop`, `refused` with its reason). Allows are recorded as well as denies, because a gate that silently stopped denying leaves the same empty log as one with nothing to deny.

- Each line carries the sha256 of the line before it; `python3 scripts/harness_events.py verify` names the first broken link. Tamper-evident, not tamper-proof.
- Only identifiers, a reason code and a short subject are recorded, never prompts, commands or output.
- Recording never raises, never writes to stdout, and no gate reads the log, so logging cannot change a decision.
- Tests point `HARNESS_EVENTS_LOG` at a scratch file; `standalone_tests.sh`, the mutation runner and the mapped-test hook set it as a backstop.
- The log is local to the machine until a publish step carries it out (open gap 5).

## Adopting it in another repository

Requirements: git, bash, Python 3.11+ and uv for the harness's own scripts, `rsync`, and `flock` or `lockf`. macOS or Linux.

1. Copy `AGENTS.md`, `CLAUDE.md`, `HARNESS.md`, `harness.toml`, `.claude/`, `.githooks/`, `.github/`, `scripts/`, `tests/` and `mutants/` into the repository.
2. Edit `harness.toml`. For example, a Node project:

   ```toml
   [commands]
   setup = "npm ci"
   lint = "npx eslint ."
   test = "npx vitest run"

   [tests]
   count_pattern = 'Tests\s+(\d+) passed'

   [[mapped_tests]]
   match = "*.ts"
   self = "*.test.ts"
   tests = ["{dir}/{stem}.test.ts"]
   command = "npx vitest run {test}"
   ```

   The harness's own tests in `tests/` keep running through `./scripts/standalone_tests.sh`; to run both, set `test = "./scripts/standalone_tests.sh && npx vitest run"` and a `count_pattern` that matches the last line.
3. Add the project's toolchain step to `.github/workflows/tests.yml` before the gate.
4. `git config core.hooksPath .githooks`, then `python3 scripts/adr.py init` (the suite fails until the ADR directory exists and every governed file has an ADR), then `./scripts/run.sh --tests`.
5. Make the first commit on a branch and point `main` at it (`git branch main HEAD` on an unborn `main`), since the pre-commit hook refuses commits on `main`.

## Strengths

- **Controls are tested against their own bypasses.** Examples: MODEL-04 mutation self-tests, the land tests that fake `uv`/`uvx` to prove exit codes propagate, and the commit-landed hook reading HEAD back instead of trusting output.
- **Rule 11 is applied consistently.** Expectation tables are hand-written literals and never derived from the artefact they check; `tests/test_harness_config.py` does not read the defaults it checks.
- **`GIT_*` scrubbing appears in every hook and script that shells out to git.** Without it, a test run from a hook rewrites real branches.
- **Each fail-open versus fail-closed choice is deliberate and documented.** Advisory hooks exit 0, gates name what they could not check, and an unreadable `harness.toml` fails loudly.

## Known weaknesses

- **Comments carry dated incident history.** It explains why each control exists. Trim it once it stops being useful.
- **Model routing is split across two places.** Tiers live in `model_router.py` and in `tests/test_model_router_hook.py`, by design under Rule 11, so a new agent type has to be added to both by hand.
- **The mutation runner assumes the standalone-test layout.** It runs `tests/test_*.py` scripts in parallel and reads their `FAIL <name>` lines; it does not yet use `commands.test`.
- **Mutation tooling is the older generation.** `docs/mutation-testing.md` is the target: Jev-assisted sizing (`plan.py`), ID ranges (`registry.py`), probes and specs, and a runner that works in an isolated clone with a double baseline. Only patches and the in-place runner exist here.
- **Hook timeouts fail open.** Claude Code proceeds when a hook times out, and the fail-closed wrapper cannot catch that. Newer Claude Code documents an `onFailure: "block"` hook setting; it is not used because the installed version could not be confirmed to accept it.
- **`agent_harness.py` clones ignored inputs with macOS `cp -c`.** Per-task worktrees are untested on Linux.

## Open gaps

### Before the first push

1. Decide visibility and a licence. Branch protection on a private repository needs a paid GitHub plan, and `github_bootstrap.sh --apply` exits non-zero when it is refused.
2. Create the repository, push `main`, set it in `docs/agents/issue-tracker.md`, run `./scripts/github_bootstrap.sh --apply`. The first CI run is the first test of `run.sh`'s `flock` path on Linux.
3. Fill in the Project context in `CLAUDE.md` and add domain invariants to `AGENTS.md`.
4. Run `codegraph init`, or remove the freshness hooks. Until then every prompt reports CG-01.

### For autonomous operation

5. **Publication is manual.** `land.sh` stops at local `main`; nothing pushes the branch or opens the pull request. Add a publish step (`git push -u origin <branch>`, `gh pr create`, `gh pr merge --auto`) so CI, not a human, is the last gate before `origin/main`.
6. **The read-only check is tamper-evident, not tamper-proof.** An agent runs as the user and can reach every file the user can, including the snapshot and the decision log. Rewriting the snapshot, deleting or breaking the log, or appending a second snapshot row is caught, because the stop checks the snapshot against the one row the start logged for that run. An agent that deliberately forges entries through `scripts/harness_events.py` itself (a fake finished stop, then a fake snapshot) is not caught: nothing running as the same user can stop that. Real isolation needs an OS sandbox or a separate user. **Read-only agents are also checked only against the main checkout.** `readonly_guard.py` cannot see a reviewer writing outside the repository, or into another worktree. Run reviewers with `isolation: "worktree"` so anything they change is discarded; whether a workflow `agent()` call can request that is unverified.
7. **Stop lints the whole tree.** `session_guard.py` runs the configured lint over the repository, not only the files this session changed, so a pre-existing lint failure blocks once in every session that edits anything. Lint is quick; scoping it would need a per-tool file list in `harness.toml`.
8. **`verify_commit_landed.py` false positive.** It needs git's `[branch sha]` line, so `git commit -q` (or a wrapper that condenses git output) is blocked although the commit landed. The land skill forbids `-q`; a sturdier check reads `git reflog -1` instead of the output.
9. **Model routing covers one plugin set.** Agent types it does not list pass with any model. List the types each installation uses in both `model_router.py` and its test.

### For auditability

10. **Lands leave no durable receipt.** `land.sh` prints `✓ LANDED` and writes nothing; `agent_harness.py`'s ledger lives in `.git/` and is never pushed. Once item 5 exists, the pull request and its `tests` check are the published record; until then, append each land to a ledger.
11. **The decision log stays on the machine.** It is recorded (see Decision log) but nothing publishes it; the publish step should summarise a branch's records into its pull request.
12. **`review-changes.js` returns findings but persists nothing**, against ORCH-09. Write its result to a named artifact.
14. **Consequence is still prose at land.** Port `mutants/plan.py` (`docs/mutation-testing.md`, stage 2): code counts, Jev judges from paths and the author's description, code applies the 0.6 / 0.8 / 0.35 rule, and the owner settles referrals. `land.sh` then requires, offline, the recorded plan, a review receipt for Medium and above, and full-suite ledger rows for every sized mutant for High.
13. **Track `mutants/mutation_ledger.jsonl`.** After a run it is untracked, and `land.sh` refuses untracked files. The runner has no test of its own.
