#!/usr/bin/env python3
"""Deny a commit made directly inside an agent_harness task worktree.

Task worktrees land through one serialized queue (.claude/harness/agent_harness.py
enqueue -> land). The lander refuses a request whose worktree HEAD moved outside
the queue, which catches a direct commit by any route, but only at land time.
This gives the early, plain message for the common route: `git commit`,
`git commit-tree` or `git update-ref` run with cwd inside a task worktree.

Everything else passes: ordinary branches, and Claude Code `isolation: worktree`
agents, which ORCH-03 lets commit their own candidate. Task worktrees are the
`worktree` paths in <git common dir>/agent-harness/tasks/*.json.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERBS = {"commit", "commit-tree", "update-ref"}


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def git_verb(command):
    """Return (committing git verb, [`-C` directories in order]) for a shell command.

    Advisory parsing, not enforcement: wrappers such as `sh -c` or `env` get past
    it. The lander's HEAD check against the task manifest is what enforces.
    """
    for segment in re.split(r"[;&|\n()]+", command):
        words = segment.split()
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]                       # VAR=value prefixes
        if not words or Path(words[0]).name != "git":
            continue
        rest, targets = words[1:], []
        while rest and rest[0].startswith("-"):     # git -c k=v, -C dir, --no-pager
            if rest[0] == "-C" and len(rest) > 1:
                targets.append(rest[1])
            elif rest[0].startswith(("--git-dir=", "--work-tree=")):
                targets.append(rest[0].split("=", 1)[1])
            skip = 2 if rest[0] in ("-c", "-C", "--git-dir", "--work-tree") else 1
            if rest[0] in ("--git-dir", "--work-tree") and len(rest) > 1:
                targets.append(rest[1])
            rest = rest[skip:]
        if rest and rest[0] in VERBS and "--dry-run" not in rest:
            return rest[0], targets
    return None, []


def task_worktrees(cwd):
    """Return the resolved worktree paths of every agent_harness task."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              cwd=cwd, capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode:
        return []
    paths = []
    for manifest in (Path(done.stdout.strip()) / "agent-harness" / "tasks").glob("*.json"):
        try:
            paths.append(Path(json.loads(manifest.read_text())["worktree"]).resolve())
        except (OSError, ValueError, KeyError):
            continue
    return paths


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    verb, targets = git_verb(command) if isinstance(command, str) else (None, [])
    if not verb:
        return 0
    # Each -C applies relative to the one before (git's rule); every directory
    # on the way counts, so neither the start nor a later hop hides a worktree.
    here = Path(payload.get("cwd") or os.getcwd())
    places = [here.resolve()]
    for target in targets:
        here = here / target
        places.append(here.resolve())
    worktrees = task_worktrees(places[0]) or task_worktrees(places[-1])
    inside = [w for w in worktrees for p in places if p == w or w in p.parents]
    if not inside:
        return 0
    record("queue_guard", "deny", f"git-{verb}", inside[0].name, payload)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"`git {verb}` inside agent_harness task worktree {inside[0]}. Task agents "
            "never commit: queue the change with `python3 .claude/harness/agent_harness.py "
            "enqueue --task <task> -m <message> --file <path>...` and the lander commits it "
            "through the gate. A commit made here is refused at land anyway."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
