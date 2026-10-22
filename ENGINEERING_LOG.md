# Engineering log

Dated failures, measurements, limitations, and implementation history (AGENTS.md ownership pointers).
Newest first. Each entry: date, what happened, how it was measured, what changed.

## 2026-10-22 - installed domain hook propagation corrections

Independent scoped review requested changes because adoption disconnected prior Git hook events, linked Git resolved the owned relative hook path locally, mapped edits required an unowned domain reader, and relative runner invocation lost its location after changing directory.
Real disposable Git and installed-hook cases reproduce these failures in `tmp/json/installer-domain-before.json` and `tmp/txt/installer-domain-before.log`.
The corrective cases exercise prior commit-msg and pre-push refusal, distinct worktree-relative hooks, actual edit registrations, and relative runner reexecution.
The installer now records an absolute owned dispatcher path and preserves prior relative hook paths and event dispatchers across adoption, upgrade and restore.
Mapped edits import owned code and normalize the execution root; the runner reuses its absolute control directory.
Focused before/after and individual fault detections are retained in `tmp/json/installer-domain-corrections.json`; parent integration and release review remain separate.

The subsequent real native preflight rejected the inherited environment set map before inference, without an authorizing receipt.
A leaf `set.PATH` override recursively merged other user set keys; replacing the aggregate map is not supported by the inspected native merge semantics.
The native lane's correction filters inherited keys with `shell_environment_policy.include_only=["PATH"]`, retains public PATH and verifies the effective environment.
The retained failure is `tmp/json/native-review-live-codex-corrected-proof.json`; no global configuration changed.

## 2026-10-22 - receipt fixture race and owned installation

The full configured gate on immutable e641bd4 failed in the concurrent land fixture with a missing worker result.
A focused land rerun passed under the actual outer verification mutex, so that result alone did not identify a lock defect.
Independent concurrent calls to the real approval fixture reproduced `isolation evidence digest does not match`: reviewers truncated the same fixture evidence files while peers hashed them.
The retained reproduction is `tmp/json/core-receipt-fixture-race.json`.
Each fixture launch now writes a unique evidence directory, concurrent checks validate every receipt, and land worker exceptions retain their full traceback.
Production evidence hash validation remains strict.

The owned installer separates payload CODE from governed REPO, journals local changes and restores only owned entries, policy spans and files.
The disposable adoption, upgrade and restore record is `tmp/json/installer-adoption-proof.json`.
It exercises actual installed Claude/native registrations and the configured gate, retains stronger user policy and foreign additions, and refuses edited ownership or ambiguous linked policy.
This local proof does not approve native hook trust or authorize a real target installation.

The earlier invocation-only native trust probe used quoted dotted keys on the override's left side.
Inspection and a live metadata probe confirmed that Codex splits those keys literally on dots; aggregate TOML values on the right side activate the intended project and hook keys.
The corrected public no-tools sentinel also required `features.apps=false` to remove implicit `codex_apps`, separately from the default app policy.
The literal proof is `tmp/json/native-readonly-sentinel-proof.json`; global configuration was unchanged.

The later exact native reviewer launch failed closed after preflight and a registered turn, without an authorizing receipt.
The diagnostic attempt identified `mcpToolCall` for server `codex`, tool `list_mcp_resources`; that native discovery classification remained under tagged-source review at this installer checkpoint.
The retained failures are `tmp/json/native-review-live-codex-proof.json` and `tmp/txt/g/native-review-live/tmp/json/review-cbdc9a299aa44180/native-events.jsonl`.
These failures do not supersede the earlier no-tools sentinel or prove completion of the actual reviewer launcher.
No global configuration was changed by either failed launch.
Tagged native source identifies the resource-list event as built-in discovery that can aggregate active MCP connections.
The launcher's pending correction permits only empty built-in listings after measured native status proves every external server disabled and every external catalogue empty; external fetches and unknown tools remain refused.
A separate reproduced launch bug supplied `thread/start environments=[]`, which disables environment access.
The pending correction omits that override and verifies the returned sole local environment and its exact clone roots.
The parent still owns the corrected live retry; this checkpoint records no successful actual reviewer turn.

## 2026-10-22 - native Codex trust and linked-worktree activation

The native policy probe using the real `~/.codex` did not load the untrusted project layer.
An invocation-only project-trust override also did not activate bootstrap trust.
An isolated `CODEX_HOME` with the primary project trusted and the existing Smartsheet header-helper string inherited proved the native read-tool policy, with literal resource-guide and workspace-list request/response artifacts at `tmp/json/native-isolated-policy-proof.json`.
No authentication files or token values were copied, and production configuration was unchanged.

The isolated probe initially returned an empty `hooks/list` despite loading the worktree's ordinary project config.
Review identified the scope difference: Codex 0.163.0 loads linked-worktree hook declarations from the primary checkout, where this branch's registry is not yet installed.
A standalone disposable clone of the candidate is required to prove its hook registration before integration; branch-local configuration alone does not activate the registry in this linked worktree.

## 2026-10-22 - CodeGraph preparation and land receipt regressions

Independent review reproduced a land that integrated its candidate, then failed during the final CodeGraph refresh without printing its successful receipt.
Pre-gate preparation remains strict; only the post-integration refresh now reports a pending refresh while retaining the successful land status and receipt.
The regression fixture fails refresh only after the candidate is actually reachable from `main`.

The initial fake let `init --yes` overwrite metadata in an already initialized index, although installed CodeGraph 1.6.2 returns early in that state.
The fake now preserves that behavior.
Existing indexes with missing initialization metadata, foreign ownership or no timestamp are retained for explicit manual recovery; genuinely absent local indexes still initialize after the repository opted in, and verified local stale indexes still sync.
Focused RED/GREEN artifacts are retained under `tmp/log/land-refresh-*.log` and `tmp/log/codegraph-ownership-*.log`.

## 2026-10-22 - mutation runner timeout hid a kill

M028 (`land.sh --expect`) was recorded as a crash-only kill, then as a survivor on a rerun, although the mutant fails three named checks when `tests/test_land.py` runs alone (98 s). Under the runner's 12 parallel workers the file exceeded the 120 s per-script timeout. A baseline timeout counted as "red at baseline", which quarantined the file, so its kills disappeared. The default timeout is now 600 s, and a baseline timeout stops the runner with the timeout to raise. The uncommitted survivor row from the misconfigured rerun was discarded; the committed crash-only row (6da00dc) stays as history, superseded by the rerun after the fix.

## 2026-10-22 - flaky gate test

`tests/test_codegraph_sync.py` failed once inside `./scripts/gate.sh` while adding the hook self-check, then passed the next full gate and 8 of 8 standalone runs. Its failing check was not captured (the gate prints only the last five lines of a failing file). It drives background `codegraph sync` processes and a lock directory with timing, which is the likely source. Unresolved; rerun the file alone before treating a failure as real, and capture its full output when it recurs.

Resolved the same day. It recurred standalone (1 run in 3) on `codegraph sync exiting 7: the failure is recorded in the log, not lost`. `wait_for` returned as soon as the background sync's log was non-empty, which can be before the hook appends its `exit 7` line. It now waits for the expected text.

## 2026-10-21 - harness made project-agnostic

`harness.toml` now defines setup, lint and test commands, untracked lander inputs, watched controls and mapped tests; `scripts/gate.sh` is the one gate for the land, CI and pre-commit.
The lander's hard-coded input provisioning was replaced by `land.copy_untracked`, and the mutation runner's `--verify-specs` mode was removed because the spec harness it needs was never part of this repository.

## 2026-10-21 - harness audit

Fixed: the mutation runner crashed with `ZeroDivisionError` on an empty corpus, wrote its ledger at the repository root while `CLAUDE.md` named `mutants/`, and missed indented `N passed` lines (199 of 488 checks counted). A `CLAUDE_VERIFY_ON_STOP` setting had no consumer and was removed.
Measured: suite and lint pass in a clean environment with no git identity and no `codegraph`; `land.sh` landed a branch end to end in a scratch clone; the runner killed M001.

## 2026-10-22 - native event adapter and commit proof

Added per-file native patch adaptation with rename coverage, path validation, shell execution workdir handling and stable lifecycle identities.
Existing policy scripts still decide ADR, test, routing, snapshot and session outcomes.
The adapter bounds policy subprocesses and kills their process groups on inner timeouts, then emits explicit gate denials; advisory crashes continue.
Claude gate registrations now use onFailure:block, which installed Claude 2.1.305 supports according to the documented 2.1.295 floor.
Installed Codex 0.163.0 source leaves outer command-hook timeout, invalid output and untrusted definitions non-blocking; this is a runtime restriction, not repaired by the adapter.
The real local doctor config/read and hooks/list probe is retained at tmp/json/native-doctor-phase1.json.
Its effective configuration response is restricted to safe layer metadata; it is not a byte-for-byte config response and contains no copied credential configuration.
The probe found real user project trust disabled and the new project registrations absent.
No user or global trust/configuration was modified.
Paired commit receipts now capture branch, HEAD and status before and after supported plain, git-C and RTK commands, so quiet output does not hide failure.
Receipt command identity is hashed rather than storing shell command text.
The native review launcher remains pending the integration receipt core and real isolation canary.

The documented worktree_capacity.py helper was absent from both ~/.claude/scripts and ~/.claude/skills/process/worktree-agent/scripts.
Work continued in the existing isolated candidate checkout; no checkout was allocated.
Explicit git -C addresses the intended checkout when global home-cwd hook detection otherwise reports not-a-repo.
No global toolkit script was changed.
