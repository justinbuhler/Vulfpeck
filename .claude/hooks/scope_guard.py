#!/usr/bin/env python3
"""Keep an edit inside the scope the owner leased for this branch (AGENTS.md "Scope").

    PreToolUse Edit|Write   with no lease file, allow silently. With one
        (<git common dir>/harness/scope/<branch>.json, written by scripts/scope.py):
        deny a path outside the lease's globs, and deny an edit that would take the
        branch's diff against its merge base past max_files or max_lines. Only
        `scripts/scope.py set` with a wider lease unblocks; `scope.py check` asks
        Jev whether the diff still serves the task and decides nothing.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]). Codex apply_patch reaches
this as Edit or Write through scripts/hook_adapter.py.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import adr  # noqa: E402
import guard_lib as g  # noqa: E402


def lease_path(top):
    """Return (path, branch) of the lease file for top's checked-out branch, or (None, None) when detached."""
    branch = g.git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    return (g.state_file("scope", top, branch), branch) if branch else (None, None)


def diff_totals(top, base):
    """Return ({changed paths}, changed lines) of the working tree against base, untracked files included."""
    files, lines = set(), 0
    for row in (g.git(top, "diff", "--numstat", base) or "").splitlines():
        added, deleted, name = row.split("\t", 2)
        files.add(name)
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    for name in (g.git(top, "ls-files", "--others", "--exclude-standard") or "").splitlines():
        files.add(name)
        try:
            lines += len((Path(top) / name).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    return files, lines


def estimate(tool_input):
    """Return the lines an edit adds, read from its new text."""
    return len(str(tool_input.get("new_string") or tool_input.get("content") or "").splitlines())


def main():
    payload = g.read_payload()
    if not payload or payload.get("hook_event_name") != "PreToolUse" or payload.get("tool_name") not in ("Edit", "Write"):
        return 0
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path")
    top = g.git(g.cwd_of(payload), "rev-parse", "--show-toplevel")
    if not isinstance(raw, str) or not raw or not top:
        return 0
    path = Path(os.path.realpath(Path(g.cwd_of(payload)) / raw))
    try:
        rel = path.relative_to(Path(top).resolve()).as_posix()
    except ValueError:
        return 0
    lease_file, branch = lease_path(top)
    if lease_file is None or not lease_file.is_file():
        return 0
    try:
        lease = json.loads(lease_file.read_text(encoding="utf-8"))
        globs, max_files, max_lines, base = lease["globs"], lease["max_files"], lease["max_lines"], lease["base"]
    except (OSError, ValueError, KeyError) as exc:
        g.record("scope_guard", "deny", "lease-unreadable", rel, payload)
        g.pre_deny(f"scope_guard: the lease {lease_file} is unreadable ({type(exc).__name__}). "
                   "Fix it with `python3 scripts/scope.py set ...` or remove it with `scope.py clear`.")
        return 0
    if not any(adr.glob_match(pattern, rel) for pattern in globs):
        g.record("scope_guard", "deny", "out-of-scope", rel, payload)
        g.pre_deny(f"out of scope: {rel}; file an issue or report it, do not edit. "
                   f"This branch's lease allows only {', '.join(globs)} for: {lease.get('task', '')}")
        return 0
    files, lines = diff_totals(top, base)
    new_files = len(files | {rel})
    new_lines = lines + estimate(tool_input)
    if new_files > max_files or new_lines > max_lines:
        g.record("scope_guard", "deny", "over-budget", rel, payload)
        g.pre_deny(
            f"scope exceeded: this edit takes {branch} to {new_files} files and {new_lines} lines against its "
            f"merge base; the lease allows {max_files} files and {max_lines} lines. Re-check intent with "
            "`python3 scripts/scope.py check` (asks Jev whether the diff still serves the task; advisory). "
            "Only an explicit `python3 scripts/scope.py set` with a wider lease unblocks this branch.")
        return 0
    g.record("scope_guard", "allow", "in-scope", rel, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
