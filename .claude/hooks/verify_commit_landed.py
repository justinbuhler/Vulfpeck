#!/usr/bin/env python3
"""Gate: a Bash call that ran `git commit` must have moved HEAD.

A git `post-commit` hook cannot do this. It fires only when a commit succeeds, so
the two failures that motivated this file are the two it can never see: an
`index.lock` collision from a peer worktree, and an exit code swallowed by a
later command in the same invocation (`git commit -m x; echo done`). Both leave
the tool call looking successful with no new commit.

    PreToolUse   a command that runs `git commit`: record HEAD and the number of
                 HEAD reflog entries in the directory the commit runs in.
    PostToolUse  read both again. Unchanged means no commit was created: block.

Reading git on both sides, never the tool's output, means `git commit -q` and
wrappers that condense output pass, and a masked or locked commit still blocks. With
no recorded baseline (the PreToolUse call never ran) the commit is reported as
unchecked, not blocked.
"""

import hashlib
import json
import os
import re
import time
import shlex
import subprocess
import sys
from pathlib import Path

STALE_SECONDS = 86400  # a baseline whose PostToolUse never ran


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


def position(where):
    """Return [HEAD sha, HEAD reflog entry count] in `where`; ["", 0] before the first commit."""
    return [git(where, "rev-parse", "HEAD"), len(git(where, "reflog", "show", "--format=%h", "HEAD").splitlines())]


def baseline_path(where, event):
    """Return the file holding this call's before-state, or None outside a repository."""
    common = git(where, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    key = event.get("tool_use_id") or hashlib.sha256(
        f"{event.get('session_id')}|{event['tool_input']['command']}".encode()).hexdigest()
    return Path(common) / "harness" / "commits" / ("".join(c for c in str(key) if c.isalnum() or c in "-_") + ".json")


def save_baseline(where, event):
    path = baseline_path(where, event)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for old in path.parent.glob("*.json"):
        if time.time() - old.stat().st_mtime > STALE_SECONDS:
            old.unlink(missing_ok=True)
    path.write_text(json.dumps({"position": position(where)}))


def verdict(before, where):
    """Return the reason the commit run in `where` is unverified, or None when HEAD moved."""
    if position(where) != before:
        return None
    why = (f"HEAD was {before[0][:12] or '(unborn)'} before the call and has not moved, so no commit was created. "
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
    if where is None:
        return
    if event.get("hook_event_name") == "PreToolUse":
        save_baseline(where, event)
        return
    path = baseline_path(where, event)
    if path is None:
        return  # not a repository: nothing to verify against
    try:
        before = json.loads(path.read_text())["position"]
        path.unlink()
    except (OSError, ValueError, KeyError):
        print("verify_commit_landed: no HEAD recorded before this call, commit NOT checked", file=sys.stderr)
        record("verify_commit_landed", "unchecked", "no-baseline", git(where, "rev-parse", "--short", "HEAD") or "-", event)
        return
    reason = verdict(before, where)
    head = git(where, "rev-parse", "--short", "HEAD") or "-"
    record("verify_commit_landed", "block" if reason else "pass",
           "not-landed" if reason else "landed", head, event)
    if reason:
        json.dump({"decision": "block", "reason": reason,
                   "systemMessage": "git commit did not land - see the blocked reason"},
                  sys.stdout)


if __name__ == "__main__":
    main()
