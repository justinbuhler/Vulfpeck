# 0017. Require candidate evidence before integration

Status: proposed
Date: 2026-10-22
Components: scripts/harness_receipts.py, scripts/harness_lock.py, scripts/harness_inputs.py, scripts/land.sh, scripts/run.sh, scripts/gate.sh, scripts/standalone_tests.sh, .claude/harness/agent_harness.py, .claude/hooks/readonly_guard.py, mutants/runner.py, .claude/skills/land/SKILL.md

## Context

Status-only snapshots miss changed bytes, ignored-input discovery copies private files, and PID files can grant concurrent lock ownership.
The lander reconciles origin before the candidate gate and accepts no independent review receipt.
Mutation results can report success despite red baselines, patch errors, and survivors.

## Decision

Require registered independent review completion bound to the candidate, baseline, tree, consequence, and hashed evidence before landing.
Require full mutation evidence for reviewer-selected High and Critical mutants, with independent derivation and population evidence for Critical changes.
Build origin reconciliation and the candidate merge in scratch, gate the result, and update main only after success.
Before each candidate merge, require the registered review baseline to be an ancestor of the reconciled scratch target, including retries.
Use descriptor-held locks in the common Git directory, explicit literal writer leases, and a shared allowlisted input copier that excludes credentials, scratch trees, and symlinks.
Compare content and index identities as well as status during read-only checks.
Run mutations in a standalone clone with strict green baselines and failing exit status for incomplete, erroneous, or surviving mutants.

## Consequences

Old land commands need an explicit reviewed candidate and registered receipt.
Existing implicit ignored-input copying stops; projects name required inputs in land.copy_untracked.
Local hashes and launch records provide cooperative provenance, not protection against a malicious process running as the same OS user.
Independent native launchers must prove no history and disabled external write-capable tools; snapshots are detection, not sandboxing.
The parent retains full-suite, corpus, native-runtime proof, and independent approval ownership.
