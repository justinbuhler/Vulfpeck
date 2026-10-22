#!/usr/bin/env python3
"""The decision is recorded before the component exists, and the record is well formed.

    PreToolUse Edit|Write    creating a file that harness.toml `adr.require_for`
                             governs is denied unless a proposed or accepted ADR
                             names it in `Components:`. Editing an existing file
                             passes; so does anything ungoverned.
    PostToolUse Edit|Write   an ADR just written is parsed (scripts/adr.py); a
                             malformed one exits 2 with every problem, so the
                             agent fixes it while the context is fresh.

This is the early, plain message. The land enforces the same rule whatever
created the file (a shell redirect gets past a Write hook): tests/test_adr.py runs
`adr.py check` over every tracked governed file.

Only the session's own project (CLAUDE_PROJECT_DIR) and its worktrees are
checked; a Write into any other repository passes.

Registered as a gate: a crash exits 2 (see .claude/settings.json).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from paths import REPO
import adr  # noqa: E402
import harness_config  # noqa: E402


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def common_dir(cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              cwd=cwd, capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def trusted(top):
    """Return whether top is this session's project or one of its worktrees.

    The rules come from this project's harness.toml schema; another repository
    the session writes into is not this hook's business.
    """
    mine = common_dir(os.environ.get("CLAUDE_PROJECT_DIR") or str(REPO))
    return bool(mine) and mine == common_dir(top)


def top_of(path):
    """Return the repository top-level directory containing path, or None."""
    here = path.parent
    while not here.exists() and here != here.parent:
        here = here.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                              capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(done.stdout.strip()).resolve() if done.returncode == 0 else None


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        return 0
    event = payload.get("hook_event_name")
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd = Path(payload.get("cwd") or os.getcwd())
    # realpath resolves symlinks in the part that exists, so a path through
    # /var (macOS: /private/var) compares equal to git's resolved top level.
    path = Path(os.path.realpath(cwd / raw))
    top = top_of(path)
    if top is None or not trusted(top):
        return 0
    try:
        rel = path.relative_to(top).as_posix()
    except ValueError:
        return 0
    try:
        config = harness_config.load(top)
    except harness_config.ConfigError as exc:
        print(f"adr_gate: {top}/harness.toml is unreadable, so ADR rules cannot be applied: {exc}",
              file=sys.stderr)
        return 2
    adr_dir = config["adr"]["dir"].rstrip("/")

    if event == "PostToolUse":
        if path not in [p.resolve() for p in adr.adr_files(top / adr_dir)]:
            return 0
        _, problems = adr.parse(path)
        if not problems:
            record("adr_gate", "pass", "adr-valid", rel, payload)
            return 0
        record("adr_gate", "block", "adr-malformed", rel, payload)
        print("adr_gate: this ADR does not parse:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2

    # Edit creates a missing file when old_string is empty, so both count.
    if event != "PreToolUse" or payload.get("tool_name") not in ("Write", "Edit") or path.exists():
        return 0
    if not adr.governed(config, rel):
        return 0
    records, _ = adr.load_all(top / adr_dir)
    found = adr.covering(records, rel)
    if found:
        record("adr_gate", "allow", f"adr-{found[0]['number']}", rel, payload)
        return 0
    record("adr_gate", "deny", "no-adr", rel, payload)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"{rel} is governed by harness.toml adr.require_for, and no proposed or accepted "
            f"ADR in {adr_dir}/ names it. Record the decision first: "
            f"`python3 scripts/adr.py new \"<decision>\" --component {rel}`, fill in Context, "
            "Decision and Consequences, then create the file. If an existing ADR already "
            "covers it, add the path to that ADR's Components."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
