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
| Codex Smartsheet policy | `.codex/config.toml`, `tests/test_codex_permissions.py` | Positive native read-tool allowlist and duplicate app disabled; credentials remain in user configuration |
| Codex model routing | `.codex/agents/*.toml`, `.codex/hooks.json`, `.claude/hooks/model_router.py` | Native profiles pin GPT model and effort; spawn gate requires a known role and matching explicit overrides |
| Codex event adapter | `scripts/native_hook_adapter.py`, `.codex/hooks.json` | Per-file patch policy, shell workdir normalization and stable lifecycle IDs reuse the existing policy scripts |
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
| `model_router.py --runtime codex` | Codex PreToolUse `spawn_agent` | Requires native `agent_type`: explorer uses Luna/high, executor uses 6.1 Sol/medium, reviewer and planner use Astra/high; mismatched explicit model or effort is denied |
| `blast_radius_advisory.py` | PreToolUse `Edit\|Write` | Advisory only. When the edit touches a file in `hooks.watched_files` or `hooks.watched_dirs`, it prints which tracked files also spell the literals being changed |
| `verify_commit_landed.py` | PreToolUse / PostToolUse `Bash` | Records before-state using stable session/call IDs; checks HEAD, intended branch and status after plain, git -C and RTK commits, including quiet output |
| `test_for_edited_module.py` | PostToolUse `Edit\|Write` | Runs the test `[[mapped_tests]]` maps to the edited file and exits 2 on failure or on an unreadable `harness.toml` |
| `codegraph_freshness.py` | UserPromptSubmit / SessionStart / Git checkout and merge | Reports CG-01..04 against the event checkout; prepares and verifies checkout-local indexes only in repositories already opted in |
| `readonly_guard.py` | SubagentStart / SubagentStop | When an agent matching `[roles] read_only` starts, snapshots the main checkout (HEAD, checked-out branch, local `main`, every ref, the local git config, `info/exclude`, `.git/hooks`, the worktree list, full status) and records the snapshot's hash in the decision log. At its stop, any difference blocks the stop once with the changes, then lets it stop and tells the parent. A branch checked out in another worktree may only advance, and its advance is reported; land a reviewed candidate with `land.sh --expect <reviewed sha>` so an advance is never landed unreviewed. A snapshot that no longer matches its logged hash, or a missing, broken or doubled log, blocks. A stop with no snapshot is reported as unverified |
| `queue_guard.py` | PreToolUse `Bash` | Denies `git commit`, `commit-tree` and `update-ref` inside an `agent_harness.py` task worktree and points at `enqueue`. Early feedback only: the lander refuses any request whose worktree HEAD moved outside the queue |
| `session_guard.py` | Stop / SessionEnd | At Stop, when HEAD or the working tree changed since the last check (or lint failed last time), runs `commands.lint`; a failure blocks the stop once with the output tail, then lets the session stop with a warning. Uncommitted paths and commits not on local `main` get a one-line notice when that state changes, and never block. At SessionEnd, records branch, HEAD, dirty and unlanded counts in the decision log |
| `adr_gate.py` | PreToolUse and PostToolUse `Edit\|Write` | In the session's own project only, denies creating (by Write, or Edit of a missing file) a file matching `adr.require_for` unless a proposed or accepted ADR in `docs/adr/` names it; exits 2 when an ADR just written does not parse. The land enforces the same coverage for files created any other way (`tests/test_adr.py`) |
| `harness_doctor.py` | SessionStart | Advisory. Reports missing prerequisites (Python 3.11+, uv, rsync, flock/lockf, `core.hooksPath`) and user-level hooks that can undercut the harness: SubagentStart context injection, PreToolUse Bash hooks that can deny `git push` or `gh pr` |

**Failure direction.** Claude gates use `onFailure: "block"`, supported from Claude Code 2.1.295, as well as crash-to-deny wrappers.
Advisories retain `|| true` and their existing normal output semantics.
The Codex adapter supervises policy subprocesses with a deadline inside its native timeout and turns gate crashes, missing files, malformed output and inner timeouts into explicit denials.
Codex 0.163.0 still treats an outer hook timeout or invalid outer output as a failed hook without blocking the tool.
An untrusted hook definition is also skipped.
Doctor reports these restrictions; no wrapper proves enforcement when the runtime never executes it.
`tests/test_harness_controls.py` and `tests/test_native_hook_adapter.py` exercise the registered commands and controlled failures; actual client execution canaries remain separate evidence.

The native adapter expands every patch operation before policy runs and includes both sides of a rename.
It rejects malformed patches, path traversal and symlink escapes from the checkout.
Native shell arguments use `workdir` when supplied, while native session, tool-call and agent IDs remain unchanged.
Reviewer, planner and explorer lifecycle roles map to the existing read-only policy names; these drift snapshots are detectors, not shell or credential isolation.
Run `uv run python scripts/harness_doctor.py --runtime both --probe-native --json` to inspect effective Codex layers and exact-definition trust without a model turn.
Claude hook activation still needs the client hook inventory and live event canary; its declared settings and supported version alone are insufficient.

Codex discovers standalone project agent files natively; no extra orchestration runner or legacy `config_file` table is required.
The project routing hook resolves its script from the Git root and has a crash-to-deny wrapper.
Codex must trust the project layer and the exact hook definition before executing it.
In Codex 0.163.0, linked worktrees use the primary checkout's hook declarations while ordinary project config remains worktree-local.
A branch-only registry is inactive in that linked worktree; verify it in a standalone disposable clone before installation in the primary checkout.
Unsupported hosts without native role selection receive a routing-unavailable denial when they dispatch the spawn hook; hosts without hook execution cannot claim this control.
`tests/test_codex_model_routing.py` keeps independent profile expectations and exercises the registered command from a subdirectory.
Claude routing and the interactive parent model remain unchanged.

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

Requirements: git, bash, Python 3.11+ and uv for the harness's own scripts, `rsync`, and `flock` or `lockf`.
The offline cleanup-protocol test also needs Node, supplied by CI; it uses the bundled interpreter when CodeGraph is installed and PATH Node otherwise.
CodeGraph remains optional.
macOS or Linux.

Plan a committed release against an explicitly named primary checkout and supply the domain build commands:

```sh
uv run --no-sync python3 scripts/harness_install.py plan --source "$RELEASE_REPO" --revision "$RELEASE_SHA" --target "$TARGET" --setup "npm ci" --lint "npm run lint" --test "npm test" --output tmp/json/install-plan.json
uv run --no-sync python3 scripts/harness_install.py apply --plan tmp/json/install-plan.json
uv run --no-sync python3 scripts/harness_install.py verify --target "$TARGET"
```

Review the plan's literal destinations, file hashes, registration entries, policy spans, collisions and local Git hook-path delta before applying.
The payload stays in `.vulfpeck`; domain instructions, scripts, CI and tests remain user owned.
Native profiles use individually owned `.codex/agents` files.
Existing deny/ask rules, credentials, plugins, custom hooks and narrower Smartsheet limits survive.
An existing domain `harness.toml`, unowned destination or ambiguous native policy requires explicit reconciliation before adoption.
Linked installation targets are refused; installed primary-discovered hooks retain the event's checkout as their governed root.
Git uses the absolute owned primary dispatcher directory across worktrees.
Pass-through dispatchers retain prior hook events and their relative paths in each invoking checkout, preserving arguments, input and refusal status.
Discovery includes existing linked checkout directories and Git-directory paths used by receive hooks.
Mapped edit hooks import the owned reader and resolve their governed checkout independently of domain scripts.
Native project trust and exact hook approval remain separate from installation.

For an upgrade, plan and apply the next committed release against the unchanged owned generation.
Restore one generation with a fresh reviewed plan:

```sh
uv run --no-sync python3 scripts/harness_install.py restore --target "$TARGET" --output tmp/json/restore-plan.json
uv run --no-sync python3 scripts/harness_install.py restore --target "$TARGET" --apply --plan tmp/json/restore-plan.json
```

Restoring an upgrade recovers its prior owned release; restoring the first adoption removes its owned files and entries and restores the prior Git hook path.
Foreign additions survive, while edited owned entries refuse restoration.
Interrupted installations report their generation; use `restore --generation <id>` to plan and apply recovery.
The common Git directory retains public payload journals and nonsecret touched spans, without whole configuration backups.

## Strengths

- **Controls are tested against their own bypasses.** Examples: MODEL-04 mutation self-tests, the land tests that fake `uv`/`uvx` to prove exit codes propagate, and the commit-landed hook reading HEAD back instead of trusting output.
- **Rule 11 is applied consistently.** Expectation tables are hand-written literals and never derived from the artefact they check; `tests/test_harness_config.py` does not read the defaults it checks.
- **`GIT_*` scrubbing appears in every hook and script that shells out to git.** Without it, a test run from a hook rewrites real branches.
- **Each fail-open versus fail-closed choice is deliberate and documented.** Advisory hooks exit 0, gates name what they could not check, and an unreadable `harness.toml` fails loudly.

## Known weaknesses

- **Comments carry dated incident history.** It explains why each control exists. Trim it once it stops being useful.
- **Model routing is split across two places.** Tiers live in `model_router.py` and in `tests/test_model_router_hook.py`, by design under Rule 11, so a new agent type has to be added to both by hand.
- **The mutation runner assumes the standalone-test layout.** It runs `tests/test_*.py` scripts in parallel and reads their `FAIL <name>` lines; it does not yet use `commands.test`.
- **Mutation tooling is the older generation.** `docs/mutation-testing.md` is the target: Jev-assisted sizing (`plan.py`), ID ranges (`registry.py`), probes and specs, and a runner that works in an isolated clone with a double baseline. The runner now uses a standalone clone, mandatory green baselines, candidate/corpus hashes, and failing status for survivors, timeouts, patch errors, or unmapped kills. Sizing and allocation remain separate work.
- **Runtime failure direction differs.** Claude gate registrations set `onFailure: "block"`, supported from 2.1.295 and present in the inspected 2.1.305 client. Codex 0.163.0 outer timeouts and untrusted definitions remain non-blocking; inner adapter denials cannot repair that runtime behavior. Installation alone does not prove exact hook trust or live activation.
- **Scratch inputs are explicit.** Task provisioning, land, and mutation clones copy only `land.copy_untracked` inputs through `harness_inputs.py`, retaining file hashes and excluding credentials, scratch trees, links, and candidate-tracked files. Writer provisioning requires literal `--file` leases; `release --task <task> --expect <sha>` verifies a clean expected tip before releasing ownership.

## Open gaps

### Before the first push

1. Decide visibility and a licence. Branch protection on a private repository needs a paid GitHub plan, and `github_bootstrap.sh --apply` exits non-zero when it is refused.
2. Create the repository, push `main`, set it in `docs/agents/issue-tracker.md`, run `./scripts/github_bootstrap.sh --apply`. The first CI run is the first test of `run.sh`'s `flock` path on Linux.
3. Fill in the Project context in `CLAUDE.md` and add domain invariants to `AGENTS.md`.
4. Decide whether the repository needs CodeGraph.
   Until the owner opts in with `codegraph init`, lifecycle hooks leave it unindexed and reports skip index lookup.
   Once opted in, each checkout owns an index; the lander prepares its merged checkout before the gate and removes only verified linked indexes after integration.

### For autonomous operation

5. **Publication is manual.** `land.sh` stops at local `main`; nothing pushes the branch or opens the pull request. Add a publish step (`git push -u origin <branch>`, `gh pr create`, `gh pr merge --auto`) so CI, not a human, is the last gate before `origin/main`.
6. **The read-only check is tamper-evident, not tamper-proof.** An agent runs as the user and can reach every file the user can, including the snapshot and the decision log. Rewriting the snapshot, deleting or breaking the log, or appending a second snapshot row is caught, because the stop checks the snapshot against the one row the start logged for that run. An agent that deliberately forges entries through `scripts/harness_events.py` itself (a fake finished stop, then a fake snapshot) is not caught: nothing running as the same user can stop that. Real isolation needs an OS sandbox or a separate user. **Read-only agents are also checked only against the main checkout.** `readonly_guard.py` cannot see a reviewer writing outside the repository, or into another worktree. Run reviewers with `isolation: "worktree"` so anything they change is discarded; whether a workflow `agent()` call can request that is unverified.
7. **Stop lints the whole tree.** `session_guard.py` runs the configured lint over the repository, not only the files this session changed, so a pre-existing lint failure blocks once in every session that edits anything. Lint is quick; scoping it would need a per-tool file list in `harness.toml`.
8. **Commit proof scope.** Quiet and RTK-filtered commits require the paired PreToolUse receipt; post-tool output alone is insufficient.
   Supported forms are plain Git, git -C, and RTK Git/proxy commands addressing one checkout per tool call.
   Arbitrary shell wrappers and git-dir/work-tree overrides do not supply supported proof.
9. **Model routing covers one plugin set.** Agent types it does not list pass with any model. List the types each installation uses in both `model_router.py` and its test.

### For auditability

10. **Land evidence is local.** `land.sh --expect <sha> --receipt <registered receipt>` requires completed independent review evidence and writes outcomes under the common Git directory `harness/lands/`. Gate logs and registered launches remain local until explicitly published. Each scratch attempt requires its reconciled target to include the registered review baseline before merging the candidate. Origin reconciliation and candidate merge run together in scratch before the single successful main update.
11. **The decision log stays on the machine.** It is recorded (see Decision log) but nothing publishes it; the publish step should summarise a branch's records into its pull request.
12. **`review-changes.js` returns findings but persists nothing**, against ORCH-09. Write its result to a named artifact.
14. **Consequence is review-bound.** The registered reviewer records consequence and required mutant IDs. High and Critical land receipts require candidate/corpus-bound full-suite mutation results. Critical receipts also require independently produced candidate-bound derivation and population artifacts. Same-user local records remain forgeable; runtime launch proof and independent LEAD verification remain required.
13. **Mutation history and run evidence differ.** The existing tracked ledger remains history. New runs default to `tmp/json/mutation-ledger.jsonl` and `tmp/json/mutation-receipt.json`; `tests/test_mutation_runner.py` exercises real cloning, green baseline, kill, survivor, and patch-error paths.

The configured gate, direct standalone suite, land window, and mutation runner share descriptor-held verification ownership in the common Git directory.
The lock order is queue ownership when used, then verification, then the short integration section; task-state operations never wait for verification.
Read-only snapshots now include path bytes/types/modes and staged-index identities, so already-dirty changes are detected.
Snapshots still do not revoke shell, network, MCP, app, plugin, or same-user filesystem authority.
