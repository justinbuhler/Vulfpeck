# Agent harness

A per-repository harness for agent-operated development. Every rule it states is enforced by something other than prose: a test, a hook, or the lander.
It is project-agnostic. A repository describes how it builds in `harness.toml`; the controls read that file instead of spelling commands themselves.

## Layers

| Layer | Files | What it enforces |
|---|---|---|
| Governance contract | `AGENTS.md` (shared by Claude Code, Codex, Gemini via `CLAUDE.md` and `.gemini/settings.json`), `tests/AGENTS.md` | ORCH-01..12 delegation rules, the consequence-to-review table, Rule 11 (relocating oracle), test conventions |
| Project config | `harness.toml`, `scripts/harness_config.py`, `scripts/harness_env.sh` | One definition of setup, lint, test, untracked inputs, watched controls and mapped tests; unknown keys are errors |
| Gate and landing | `scripts/gate.sh`, `scripts/run.sh`, `scripts/land.sh`, `.githooks/*`, `.github/workflows/tests.yml` | No commits or merge commits on `main`; lint at commit; the same gate at land and in CI; compare-and-swap of local `main` |
| Hooks (all runtimes) | `harness.toml` `[[hook]]`, `scripts/render_hooks.py`, `scripts/hook_adapter.py`, `.claude/settings.json`, `.codex/hooks.json`, `.gemini/settings.json`, `.claude/hooks/*.py` | One registration table rendered for Claude Code, Codex and Gemini; model routing on `Agent` calls, commit-landed verification (HEAD before and after), mapped test on edit, blast-radius advisory, lint of changed files at stop, CodeGraph freshness |
| Agent and review tooling | `.claude/agents/*`, `.agents/skills/*`, `.claude/workflows/*`, `.claude/harness/agent_harness.py`, `mutants/` | Independent re-derivation, live-API proof, per-task worktrees with a serialized commit queue, mutation testing |
| Task loop | `scripts/task_loop.py`, `harness.toml` `[loop]`, ADR 0018 | Open issues labelled `ready-for-agent` become merged pull requests: forward-only task state, priority and Jev choice, a worker in a provisioned worktree, `plan_tests.py --run`, the harness lander, the base pinned to a commit read once after the fetch, and any change outside a hard-coded docs allowlist (`docs/**` and `*.md`, except `AGENTS.md`, `CLAUDE.md`, `HARNESS.md` and `docs/agents/**`) opened as a `needs-review` PR and never merged, `gh pr checks --watch` then `gh pr merge --merge --match-head-commit` only when the check list is non-empty and all `pass`, `loop_reset.py` between tasks, stop after 2 consecutive failures or one issue failing twice; `--dry-run` runs nothing |

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
| `verify_commit_landed.py` | PreToolUse `Bash` (advisory), PostToolUse `Bash` (gate) | For a real `git commit` (cwd, `cd` and `git -C` resolved, quoted separators respected), Pre records HEAD and the HEAD reflog length where the commit runs, one baseline file per call (parallel identical commands each keep theirs). Post blocks when both are unchanged, so no commit was created (an index.lock race, or an exit code masked by a later command). Output is a second witness: a printed `[branch sha]` must equal the new HEAD, and a failure marker (`fatal:`, `error:` or index.lock lines) with no sha line blocks even when HEAD moved, since a peer commit in the same worktree can move it. With no recorded baseline it falls back to the output check alone: block unless a `[branch sha]` equals HEAD, which falsely blocks `git commit -q` on that rare path, by design |
| `test_for_edited_module.py` | PostToolUse `Edit\|Write` | Runs the test `[[mapped_tests]]` maps to the edited file and exits 2 on failure or on an unreadable `harness.toml` |
| `codegraph_freshness.py` | UserPromptSubmit / SessionStart | Reports CG-01..04 and syncs the index on CG-02, in the main checkout only |
| `readonly_guard.py` | SubagentStart / SubagentStop | When an agent matching `[roles] read_only` starts, snapshots the main checkout (HEAD, checked-out branch, local `main`, every ref, the local git config, `info/exclude`, `.git/hooks`, the worktree list, full status). At its stop, any difference blocks the stop once with the changes, then lets it stop and tells the parent. A branch checked out in another worktree may only advance, and its advance is reported; land a reviewed candidate with `land.sh --expect <reviewed sha>` so an advance is never landed unreviewed. The snapshot file is not hash-checked and no log is consulted. A stop with no snapshot is reported as unverified. Run reviewers in a throwaway worktree: the guard cannot see writes outside the main checkout |
| `queue_guard.py` | PreToolUse `Bash` | Denies `git commit`, `commit-tree` and `update-ref` inside an `agent_harness.py` task worktree and points at `enqueue`. Early feedback only: the lander refuses any request whose worktree HEAD moved outside the queue |
| `session_guard.py` | Stop / SessionEnd | At Stop, when HEAD or the working tree changed since the last check (or lint failed last time), runs `commands.lint`. When the command is `ruff check .` (with or without `uvx`), it runs on the `.py` files changed since the merge-base with local `main` (else `origin/main`) plus untracked ones, with `--force-exclude`, and not at all when none changed; any other command runs unchanged on the whole tree. A failure blocks the stop once with the output tail, then lets the session stop with a warning. Uncommitted paths and commits not on local `main` get a one-line notice when that state changes, and never block. At SessionEnd, records branch, HEAD, dirty and unlanded counts in the decision log |
| `adr_gate.py` | not registered | Optional creation-time gate for a project that sets `adr.require_for`: denies creating a matching file unless a proposed or accepted ADR names it, and exits 2 on an ADR that does not parse. Register it with `[[hook]]` entries for PreToolUse and PostToolUse `Edit\|Write` (kind `gate`). Kept because the adapter and control tests drive it from a fixture project |
| `loop_guard.py` | PreToolUse and PostToolUse `Edit\|Write`; PreToolUse, PostToolUse and PostToolUseFailure `Bash` | Counts a failed command by fingerprint (normalised command plus first error line, volatile parts removed). The 3rd sighting blocks with the AGENTS.md rule, the three attempts and Jev's `reassess()` advice; a passing run clears that command's counts. A test or lint run that fails with no fewer failing tests than the previous failing run is a stalled cycle: two block once, a second block denies all test and lint commands. A command byte-identical to the last failed one is denied until an edit or any other Bash command runs. An unknown exit (Codex rollout not yet written) changes nothing. The agent cannot run `loop_reset.py` or touch `harness/loop`; the owner or task runner resets with `scripts/loop_reset.py --session <id> --reason "..."`, which records an event. State: `<git common dir>/harness/loop/<session>[.<agent>].json` |
| `scope_guard.py` | PreToolUse `Edit\|Write` | Opt-in by lease (`scripts/scope.py set --glob ... --max-files N --max-lines N --task "..."`, stored at `<git common dir>/harness/scope/<branch>.json`). Denies a path outside the globs and an edit that would take the branch's diff against its merge base past the limits; `scope.py check` asks Jev whether the diff still serves the task; only a wider `scope.py set` unblocks |
| `budget_guard.py` | PreToolUse (every tool) | `[budget]` `max_tool_calls` and `max_minutes` per session (0 is off): one warning at 80%, a deny at 100% ("write status and stop") except a Write or Edit of a `status_glob` file; the clock counts active time (a gap over 30 minutes is not counted); `loop_reset.py --budget` starts a fresh one |
| `turn_guard.py` | Stop (Gemini AfterAgent) | Blocks once a final message whose last paragraph announces unstarted work ("next I will", "I'll now", "let me now", "I'm going to", "will start"): "do it or name the blocker". Negations, questions, quoted text, code, conditional or waiting phrasings, numbered plans and a named blocker pass |
| `harness_doctor.py` | SessionStart | Advisory. Reports missing prerequisites (Python 3.11+, uv, rsync, flock/lockf, `core.hooksPath`) and user-level hooks that can undercut the harness: SubagentStart context injection, PreToolUse Bash hooks that can deny `git push` or `gh pr` |

**Limits of the loop, scope and budget guards.** The scope lease (`scope_guard.py`) binds Edit and Write only: under Claude Code a Bash `rm`, `mv`, `sed -i` or `cat >` changes files outside it, and only the branch's diff size, not the path, is visible to a later edit. Hooks of parallel tool calls run concurrently, and a loop or budget counter update can be lost when two read the state before either writes it (the write is atomic; read-modify-write is not locked). An unchanged re-run is allowed after any other Bash command, so a cat-and-retry loop is held only by the 3-sighting rule. A Gemini block on PostToolUse replaces the tool output with the message. Deleting the loop state with `find -delete` or `python -c` is not caught (only `rm`, `mv`, `sed`, `tee`, `dd`, `cp`, `truncate`, `unlink` and redirects are). A numbered last line in a final message disables `turn_guard.py` for that message.

**Failure direction.** Claude Code lets a tool call proceed when a hook exits with anything other than 0 or 2, so a crashing gate looks like a gate with nothing to deny. Every gate's registered command therefore ends in `|| { ...; exit 2; }`, turning a crash or a missing file into a block; advisory hooks end in `|| true` so they never block. `tests/test_harness_controls.py` keeps its own inventory of every registration, runs each command exactly as registered, gives each gate a deny and an allow canary, and proves both failure directions by swapping in a crashing script. A hook that exceeds its timeout still fails open; keep gates fast (the canaries assert under half the timeout).

### One table, three runtimes

Hooks are registered once, in `harness.toml` `[[hook]]` entries (`hook`, not `hooks`: `[hooks]` is already a table). `python3 scripts/render_hooks.py` writes the `hooks` key of `.claude/settings.json` and `.gemini/settings.json` (other keys are kept) and all of `.codex/hooks.json`; `--check` exits 1 when a file is stale and `tests/test_harness_controls.py` fails on it. Never edit the rendered hook sections by hand.

Every guard speaks Claude Code's payload and output schema. Under Codex and Gemini the registered command is `scripts/hook_adapter.py --runtime <codex|gemini> --event <native event> -- <the same guard command>`: it converts the native payload to the Claude shape (one event per file for a Codex `apply_patch`), runs the guard, and converts exit code 2, `permissionDecision: deny`, `decision: block` and `additionalContext` back to what that runtime acts on. A result the runtime cannot express (a deny on SessionStart, `updatedInput`, `continue: false`) raises; the registration then blocks for a gate and is ignored for an advisory, as under Claude Code.

| Claude Code event | Codex 0.164 | Gemini 0.55.1 |
|---|---|---|
| SessionStart `startup` | SessionStart | SessionStart |
| UserPromptSubmit | UserPromptSubmit | BeforeAgent |
| PreToolUse, PostToolUse | same names | BeforeTool, AfterTool |
| Stop | Stop | AfterAgent |
| SessionEnd | SessionEnd (Codex clamps its timeout to 3s) | SessionEnd |
| SubagentStart, SubagentStop | same names | none |

| Claude Code tool | Codex | Gemini |
|---|---|---|
| Bash | Bash | `run_shell_command` |
| Edit, Write | `apply_patch` (one synthetic Edit or Write per file) | `replace`, `write_file` |
| Agent | `spawn_agent` (`agent_type` becomes `subagent_type`) | none |

Known gaps: Gemini has no subagent events and no Agent tool matcher, so `readonly_guard.py`, `require_agent_model.py` and `model_router.py` do not run there (`render_hooks.gaps()` lists them, and a test pins the list). Codex loads a project's `.codex/hooks.json` only from a trusted folder, and runs a hook only after that hook's hash is trusted inside Codex (`/hooks`); `--dangerously-bypass-hook-trust` skips that and is not used here. Codex 0.164 reads `.codex/hooks.json` from the main checkout for every linked worktree (nested or sibling) and loads no hook, silently, when main has none. A branch that changes hook registration is therefore only exercised under Codex from a standalone clone or after it lands on main; `harness_doctor.py --runtimes` fails with "Codex in this worktree runs main's hooks: missing|stale" when main's file differs from this tree's rendering. Codex `write_stdin` (typing into an open `exec_command` session) does not reach PreToolUse (probed live: one event for the `cat` that opened the session, none for the typed text), so a shell opened with `exec_command` can run commands no gate sees; this is an open gap. A patch sent through the shell (`apply_patch <<'EOF'`) is reported as Bash, so the edit guards are also registered on Bash under Codex and the adapter (`--edits`) turns such a command into one Edit or Write event per file for every Begin..End block of every `apply_patch` invocation, where `apply_patch` is the command word of a simple command (start of a line, after `&&`, `;`, `|`, `(`, or inside `bash -c`) and the patch is its heredoc or argument; text elsewhere (echo, rg, a commit message, a `cat > f <<EOF` example) is not a patch, and `apply_patch < file` and `cat p | apply_patch` carry no patch the adapter can see. Unverified whether Codex exposes a runnable apply_patch on the shell PATH (redirected or piped forms). The adapter kills its guard's process group on SIGTERM or SIGINT, and caps its budget at the runtime's own limit where known (Codex SessionEnd 3s). Every adapter run has a deadline about 1s under the hook's registered timeout (`--timeout`) on both the guard and reading stdin; past it the adapter exits non-zero, which blocks a gate and fails open for an advisory. Gemini BeforeTool deny is proven by tests only; live proof is pending owner auth (the account fails with IneligibleTierError). Gemini loads project settings only from a trusted folder (`~/.gemini/trustedFolders.json`, matched by ancestor path).

`python3 scripts/harness_doctor.py --runtimes` reports each CLI's presence and version, whether the rendered files are fresh, whether this folder is trusted, and whether credentials exist (Codex: `codex login status`; Gemini: credentials only, eligibility is not checked). `python3 scripts/harness_install.py` is a dry run; `--apply` renders, sets `core.hooksPath` and runs `uv sync`; `--apply --trust` also writes the Codex and Gemini folder trust. It prints, never runs, the CLI install commands and never touches `~/.claude`.

### Architecture decisions

`docs/adr/NNNN-slug.md`, one per decision, in the repository and reviewed with the code (ADR 0002). ADRs record real decisions and are written when a choice is made; `adr.require_for` is empty by default. `scripts/adr.py check` validates the status, date, components, the three required sections, unique numbering and two-way supersession links for the ADRs that exist, and fails when an accepted ADR names files that no longer exist. A project that lists globs in `adr.require_for` also fails the check when a tracked matching file is named by no proposed or accepted ADR, so its land refuses an undocumented component; an exemption is an `adr.exempt` entry in a reviewed commit. Nothing here depends on a user-level toolkit, and there is no bypass variable.

### Decision log

`scripts/harness_events.py` appends one plain JSON line per gate decision to `<git common dir>/harness/events.jsonl`: every `Agent` call (`require_agent_model`, `model_router`), every commit `verify_commit_landed` checks, and every land outcome (`landed`, `noop`, `refused` with its reason). Allows are recorded as well as denies, because a gate that silently stopped denying leaves the same empty log as one with nothing to deny.

- The log is telemetry, not evidence: lines are not chained, so an edit or deletion is not detected.
- At 5 MB (`MAX_BYTES` in the script, not in `harness.toml`: hooks run beside a copy of it without `harness_config`) the file moves to `events.jsonl.1`, replacing the old backup, so at most about 10 MB is kept. Writers lock `events.jsonl.lock`, not the log.
- Only identifiers, a reason code and a short subject are recorded, never prompts, commands or output.
- Recording never raises, never writes to stdout, and no gate reads the log, so logging cannot change a decision.
- Tests point `HARNESS_EVENTS_LOG` at a scratch file; `standalone_tests.sh`, the mutation runner and the mapped-test hook set it as a backstop.
- The log is local to the machine until a publish step carries it out (open gap 5).

## Adopting it in another repository

Requirements: git, bash, Python 3.11+ and uv for the harness's own scripts, `rsync`, and `flock` or `lockf`. macOS or Linux.

1. Copy `AGENTS.md`, `CLAUDE.md`, `HARNESS.md`, `harness.toml`, `.claude/`, `.agents/`, `.codex/`, `.gemini/`, `.githooks/`, `.github/`, `scripts/`, `tests/` and `mutants/` into the repository.
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
4. `git config core.hooksPath .githooks`, then `./scripts/run.sh --tests`. ADRs are optional: run `python3 scripts/adr.py init` when the first real decision is made, or list globs in `adr.require_for` to require one per matching file (the suite then fails until the directory exists and each such file has an ADR).
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
- **`agent_harness.py` clones ignored inputs with `cp -c` on macOS and `cp --reflink=auto` elsewhere.** On a filesystem without reflinks the Linux path is a full copy, so large inputs cost disk and time there.

## Open gaps

### Before the first push

1. Decide visibility and a licence. Branch protection on a private repository needs a paid GitHub plan, and `github_bootstrap.sh --apply` exits non-zero when it is refused.
2. Create the repository, push `main`, set it in `docs/agents/issue-tracker.md`, run `./scripts/github_bootstrap.sh --apply`. The first CI run is the first test of `run.sh`'s `flock` path on Linux.
3. Fill in the Project context and add domain invariants in `AGENTS.md`.
4. Run `codegraph init`, or remove the freshness hooks. Until then every prompt reports CG-01.

### For autonomous operation

5. **Publication is automated only by the task loop, and `main` is unprotected.** `scripts/task_loop.py` pushes the task branch (git hooks off), opens the pull request and merges it with `gh pr merge --merge` once every check passed (never `--auto`: repository auto-merge is off). Only a change whose every path is on the docs allowlist merges unattended; every other change, a rename or delete included, is left for the owner. A green check list is the only gate until branch protection exists (gap 1). The worker, verification and the lander run without `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `SSH_AUTH_SOCK`, with an empty `GH_CONFIG_DIR` and `GIT_CONFIG_NOSYSTEM=1`. **This does not contain a hostile worker that runs as the owner:** the keyring, ssh keys on disk and the owner's file permissions stay reachable. Only a sandbox plus branch protection contains it, and enabling the loop on a real repository requires both. Known gaps: the loop does not wait for checks to register or compare the required check names (a repository with no checks fails every task, and a check that never appears is not noticed), and the stale-text check covers body edits only, not a title rename or who applied the label. The loop's reset clears the `task-loop` session, not the worker's own session counters, and a Codex task is `blocked` unless main's `.codex/hooks.json` is fresh. It has not been run against the real repository.
6. **The read-only check is a diff, not a sandbox.** An agent runs as the user and can reach every file the user can, including the start snapshot, so it can rewrite the snapshot to match its own changes. `readonly_guard.py` also cannot see a reviewer writing outside the main checkout or into another worktree. Run reviewers in a throwaway worktree (`isolation: "worktree"`) so anything they change is discarded; whether a workflow `agent()` call can request that is unverified. Real isolation needs an OS sandbox or a separate user.
7. **Stop lints the whole tree for any command but `ruff check .`.** `session_guard.py` narrows only that command to the session's changed files; another lint command still runs over the repository, so a pre-existing failure blocks once in every session that edits anything. Add a per-tool file list to `harness.toml` for a second tool.
9. **Model routing covers one plugin set.** Agent types it does not list pass with any model. List the types each installation uses in both `model_router.py` and its test.

### For auditability

10. **Lands leave no durable receipt.** `land.sh` prints `✓ LANDED` and writes nothing; `agent_harness.py`'s ledger lives in `.git/` and is never pushed. Once item 5 exists, the pull request and its `tests` check are the published record; until then, append each land to a ledger.
11. **The decision log stays on the machine.** It is recorded (see Decision log) but nothing publishes it; the publish step should summarise a branch's records into its pull request.
12. **`review-changes.js` returns findings but persists nothing**, against ORCH-09. Write its result to a named artifact.
14. **Consequence is still prose at land.** Port `mutants/plan.py` (`docs/mutation-testing.md`, stage 2): code counts, Jev judges from paths and the author's description, code applies the 0.6 / 0.8 / 0.35 rule, and the owner settles referrals. `land.sh` then requires, offline, the recorded plan, a review receipt for Medium and above, and full-suite ledger rows for every sized mutant for High.
13. **The mutation runner has no test of its own.** `mutants/mutation_ledger.jsonl` is tracked; the runner that writes it is exercised only by running it.
