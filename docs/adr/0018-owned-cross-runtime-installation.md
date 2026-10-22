# 0018. Install only owned cross-runtime controls

Status: proposed
Date: 2026-10-22
Components: scripts/harness_install.py, scripts/paths.py, scripts/native_hook_adapter.py, scripts/harness_env.sh, scripts/gate.sh, scripts/run.sh, scripts/land.sh, scripts/harness_config.py, scripts/harness_doctor.py, .claude/hooks/session_guard.py, .claude/hooks/readonly_guard.py, .claude/hooks/adr_gate.py, .claude/hooks/test_for_edited_module.py

## Context

Copying harness directories overwrites domain controls and cannot distinguish user edits from installed releases.
Payload-relative repository roots also point namespaced controls at the wrong checkout.
Native configuration remains additive, and linked Codex worktrees discover primary-checkout hook declarations.

## Decision

Keep installed code under `.vulfpeck` and separate its CODE root from the governed REPO root through scripts/paths.py.
Plan exact committed payload files, owned registrations, restricted Codex policy keys, and a chained Git hook path before applying changes.
Preserve domain instructions, scripts, permissions, credentials, plugins, profiles, and foreign hooks.
Intersect existing narrower Smartsheet tool limits and preserve disabled policy.
Apply only unchanged preimages under a common Git mutex, retaining owned hashes, nonsecret restoration values, and a journal.
Restore only owned changes and refuse edited owned files or registrations.
Never redirect an installation from a linked checkout to its primary checkout.
Git discovers the primary owned dispatcher directory by absolute path, while pass-through dispatchers preserve prior relative hook paths in the invoking checkout.
Mapped edits import the payload's config reader and resolve the governed checkout separately.

## Consequences

Projects explicitly supply setup, lint, test, and count commands rather than inheriting Vulfpeck's source-tree assumptions.
Installation does not approve native hook trust, remove global hooks, copy authentication stores, or prove runtime activation.
Disposable real adoption, upgrade, and restoration checks precede any separately authorized target installation.
