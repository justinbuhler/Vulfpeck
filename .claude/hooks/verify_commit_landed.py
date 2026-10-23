#!/usr/bin/env python3
"""PostToolUse gate: a Bash call that ran `git commit` must leave that commit at HEAD.

A git `post-commit` hook cannot do this. It fires only when a commit succeeds, so
the two failures that motivated this file are the two it can never see: an
`index.lock` collision from a peer worktree, and an exit code swallowed by a
later command in the same invocation (`git commit -m x; echo done`). Both leave
the tool call looking successful with no new commit. This runs after the tool
instead and reads HEAD back from git, an oracle independent of the exit code and
the output text that lied.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# `[main 1a2b3c4] subject` - the one line a successful commit prints.
LANDED = re.compile(r"^\[[^\]\n]* ([0-9a-f]{7,40})\]", re.M)


def commit_dir(command, cwd):
    """Directory a real `git [-C x] [-c k=v] commit` runs in, or None when none does.

    Follows a plain `cd <path>` segment and `git -C <path>`, nothing more.
    """
    here = cwd
    for segment in re.split(r"[;&|\n]+", command):
        try:
            words = shlex.split(segment)
        except ValueError:  # the split cut inside a quoted `;`/`|`; fail toward checking
            words = segment.split()
        if len(words) == 2 and words[0] == "cd":
            here = os.path.join(here, os.path.expanduser(words[1]))
            continue
        if words[:1] != ["git"] or "--dry-run" in words:
            continue
        where, i = here, 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-C" and i + 1 < len(words):
                where = os.path.join(where, os.path.expanduser(words[i + 1]))
            i += 2 if words[i] in ("-C", "-c") else 1
        if words[i:i + 1] == ["commit"]:
            return where
    return None


def text_of(value):
    """Flatten a tool response of any shape into searchable text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(text_of(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(text_of(v) for v in value)
    return ""


# An inherited GIT_DIR would point these reads at another repository, and a read
# that fails returns "" - which this file treats as "nothing to verify" and stays
# silent. A stray variable would therefore switch the whole check off rather than
# make it noisy, so the verifier scrubs its own environment before asking git.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def git(cwd, *args):
    """Run git in `cwd`; empty string if it cannot answer."""
    try:
        done = subprocess.run(("git",) + args, capture_output=True, text=True,
                              timeout=5, env=ENV, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def verdict(response, where):
    """Return the reason the commit run in `where` is unverified, or None when HEAD confirms it."""
    head = git(where, "rev-parse", "HEAD")
    if not head:
        return None  # not a repository, or no commits yet - nothing to verify against
    reported = LANDED.findall(text_of(response))
    if reported and head.startswith(reported[-1]):
        return None
    if reported:
        why = (f"git printed commit {reported[-1]}, but HEAD is {head[:12]}. "
               "The commit just reported is not the one this branch is on.")
    else:
        why = ("git printed no `[branch sha]` line, so no commit was created. "
               "An index.lock collision, or an exit code masked by a later "
               "command in the same invocation, both look exactly like this.")
    # Asked for as a standing warning; it earns its place only here. A lock is
    # held during a git operation and gone after, so its presence after the call
    # explains a commit that did not land - and says nothing after an `ls`.
    lock = git(where, "rev-parse", "--git-path", "index.lock")
    if lock and os.path.exists(os.path.join(where, lock)):
        why += f" {lock} is still present, so a peer worktree likely held the index."
    return (f"Commit not verified. {why} HEAD is now: {git(where, 'log', '-1', '--format=%h %s') or '(unknown)'}. "
            "Re-run `git log -1` and `git status --short` and correct the record "
            "before reporting this commit as landed.")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        # Say so. A control that cannot read its input must not be
        # indistinguishable from one that looked and found nothing wrong.
        print("verify_commit_landed: unreadable hook input, commit NOT checked",
              file=sys.stderr)
        return
    command = (event.get("tool_input") or {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()
    where = commit_dir(command, cwd) if isinstance(command, str) else None
    reason = verdict(event.get("tool_response") or {}, where) if where is not None else None
    if where is not None:
        head = git(where, "rev-parse", "--short", "HEAD") or "-"
        record("verify_commit_landed", "block" if reason else "pass",
               "not-landed" if reason else "landed", head, event)
    if reason:
        json.dump({"decision": "block", "reason": reason,
                   "systemMessage": "git commit did not land - see the blocked reason"},
                  sys.stdout)


if __name__ == "__main__":
    main()
