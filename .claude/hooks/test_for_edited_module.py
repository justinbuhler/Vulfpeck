#!/usr/bin/env python3
"""Run the one test file that maps to a just-edited file and report a break.

The full suite gates the land, not the session, so nothing catches an ordinary
self-inflicted break while work is in progress. One mapped test runs in seconds
and catches the common case: the file just changed broke its own test.

PostToolUse fires after the edit and cannot undo it, so this reports rather than
blocks. Exit 2 is what puts the failure in front of the model instead of leaving
it in a log nobody reads; every other outcome, including "no test maps here", is
silence and exit 0.

Which test maps to which file, and the command that runs it, come from
harness.toml `[[mapped_tests]]` (scripts/harness_config.py). The defaults are:

    <dir>/tests/test_<name>.py   pkg/scripts/x.py -> pkg/scripts/tests/test_x.py
    tests/test_<name>.py         scripts/x.py     -> tests/test_x.py

A harness.toml that cannot be read is reported with exit 2, not skipped: a hook
that goes quiet on a broken config is indistinguishable from one with nothing
to say.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

TIMEOUT = 60

# git exports these into hook processes and they override cwd for every git
# command a test runs, so a test that builds a throwaway repo would operate on
# THIS repository instead. .githooks/pre-commit scrubs them for the same reason.
GIT_LEAKS = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


def git_path(path, query):
    env = {k: v for k, v in os.environ.items() if k not in GIT_LEAKS}
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", query],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def trusted(root):
    """Return whether root is this session's project or one of its worktrees.

    The hook imports root's scripts/harness_config.py and runs a command from
    root's harness.toml, so a session that has cd'd into another repository
    must not execute that repository's code.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    mine = git_path(project, "--git-common-dir") if project else None
    return bool(mine) and mine == git_path(root, "--git-common-dir")


def load_config(root):
    """Return (harness_config module, config) for one repository root."""
    sys.path.insert(0, str(root / "scripts"))
    import harness_config

    return harness_config, harness_config.load(root)


def main():
    """Run the mapped test for the edited file and report a failure.

    Returns:
        (int): 2 when the mapped test fails, cannot complete, or the config is unreadable, else 0.
    """
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw:
        return 0
    cwd = Path(payload.get("cwd") or os.getcwd())
    top = git_path(cwd, "--show-toplevel")
    if not top:
        return 0
    root = Path(top)
    if not trusted(root):
        return 0
    if not (root / "scripts" / "harness_config.py").is_file():
        return 0
    try:
        harness_config, config = load_config(root)
    except Exception as exc:  # the reader's own error, or an import failure
        print(f"mapped test not run: harness.toml unreadable: {exc}", file=sys.stderr)
        return 2
    edited = Path(raw)
    if not edited.is_absolute():
        edited = cwd / edited
    found = harness_config.mapped_test(config, root, edited)
    if found is None:
        return 0
    test, command = found

    env = {k: v for k, v in os.environ.items() if k not in GIT_LEAKS}
    # The mapped test may run real hooks; keep its decisions out of the real record.
    env.setdefault("HARNESS_EVENTS_LOG", os.devnull)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"{test.name} not completed: {type(exc).__name__}", file=sys.stderr)
        return 2

    name = (
        test.relative_to(root.resolve())
        if test.is_relative_to(root.resolve())
        else test
    )
    if proc.returncode == 0:
        print(f"✓ {name}")
        return 0
    tail = "\n".join(f"{proc.stdout}{proc.stderr}".strip().splitlines()[-15:])
    print(
        f"✗ {name} fails after editing {Path(raw).name} (rc={proc.returncode})\n{tail}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
