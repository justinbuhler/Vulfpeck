#!/usr/bin/env python3
"""Report what this machine lacks for the harness, at session start.

A repository test can prove the harness is wired; it cannot see the machine it
will run on. This checks the prerequisites the controls assume and the
user-level Claude Code hooks that can undercut them, and says so once, at
SessionStart. It never blocks: it prints findings and exits 0.

Two kinds of user-level hook are named, because the harness cannot switch off a
hook it does not own:
  - SubagentStart hooks can inject the parent session's context into reviewers,
    which breaks "authoring and approval occur in fresh contexts" (CLAUDE.md).
  - PreToolUse Bash hooks can deny `git push` or `gh pr`, which the publish
    path needs.

Runs on Python 3.9+ so that it can report an interpreter too old for the rest.

    python3 scripts/harness_doctor.py            # findings only; silent when clean
    python3 scripts/harness_doctor.py --verbose  # also list what passed
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    """Return git's stdout in the repository, or "" when git cannot answer."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def user_settings():
    """Return the user-level settings Claude Code reads, or {} when absent."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        return json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hook_commands(settings, event, matcher_has=None):
    """Return the commands registered for one event, optionally by matcher."""
    found = []
    for group in (settings.get("hooks") or {}).get(event) or []:
        matcher = group.get("matcher") or ""
        if matcher_has and matcher and matcher_has not in matcher.split("|") and matcher != "*":
            continue
        for hook in group.get("hooks") or []:
            if hook.get("command"):
                found.append(hook["command"])
    return found


def script_name(command):
    """Return the hook script a command runs, or the command's first word."""
    found = re.findall(r"[\w.-]+\.(?:py|sh|js)\b", command)
    return found[-1] if found else command.split()[0]


def findings():
    """Return (problems, passes) as lists of one-line strings."""
    problems, passes = [], []

    def need(ok, good, bad):
        (passes if ok else problems).append(good if ok else bad)

    need(sys.version_info >= (3, 11), f"python3 {sys.version.split()[0]}",
         f"python3 is {sys.version.split()[0]}; hooks reading harness.toml need 3.11+")
    for tool in ("git", "uv", "rsync"):
        need(shutil.which(tool) is not None, f"{tool} found", f"{tool} is not on PATH")
    need(bool(shutil.which("flock") or shutil.which("lockf")), "flock/lockf found",
         "neither flock nor lockf is on PATH; scripts/run.sh cannot serialise the suite")
    hooks_path = git("config", "--get", "core.hooksPath")
    need(hooks_path == ".githooks", "core.hooksPath is .githooks",
         f"core.hooksPath is {hooks_path or 'unset'}; run: git config core.hooksPath .githooks")
    if (ROOT / "harness.toml").is_file() and sys.version_info >= (3, 11):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import harness_config
            harness_config.load(ROOT)
            passes.append("harness.toml reads cleanly")
        except Exception as exc:
            problems.append(f"harness.toml: {exc}")

    user = user_settings()
    for command in hook_commands(user, "SubagentStart"):
        problems.append("user-level SubagentStart hook may inject context into reviewers: "
                        + script_name(command))
    bash_hooks = hook_commands(user, "PreToolUse", matcher_has="Bash")
    if bash_hooks:
        names = sorted({script_name(c) for c in bash_hooks})
        problems.append(f"{len(bash_hooks)} user-level PreToolUse Bash hook(s) run here and "
                        f"can deny git push or gh pr: {', '.join(names)}")
    return problems, passes


def main():
    problems, passes = findings()
    if "--verbose" in sys.argv:
        for line in passes:
            print(f"  ok   {line}")
    if problems:
        print("harness doctor: " + str(len(problems)) + " finding(s)")
        for line in problems:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
