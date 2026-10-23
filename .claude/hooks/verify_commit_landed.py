#!/usr/bin/env python3
"""Gate: a Bash call that ran `git commit` must have moved HEAD.

A git `post-commit` hook cannot do this. It fires only when a commit succeeds, so
the two failures that motivated this file are the two it can never see: an
`index.lock` collision from a peer worktree, and an exit code swallowed by a
later command in the same invocation (`git commit -m x; echo done`). Both leave
the tool call looking successful with no new commit.

    PreToolUse   a command that runs `git commit`: record HEAD and the number of
                 HEAD reflog entries in the directory the commit runs in. Each
                 call gets its own baseline file; PostToolUse takes the oldest
                 for that call id (else session and command), so parallel
                 identical commands each get theirs.
    PostToolUse  read both again. Unchanged means no commit was created: block.
                 Git's output is a second witness: a `[branch sha]` line must
                 equal the new HEAD, and a failure marker (index.lock, nothing
                 to commit, a hook rejection) with no sha line blocks even when
                 HEAD moved, because a peer commit in the same worktree can move it.

With no recorded baseline (the PreToolUse call crashed, never fired, or its file was
taken) the old output check applies: block unless the output holds a `[branch sha]`
line equal to HEAD. A `git commit -q` is falsely blocked on that rare path; that is
accepted, because the alternative is a gate that passes whatever it cannot see.
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
# `[main 1a2b3c4] subject` - the one line a successful commit prints.
LANDED = re.compile(r"^\[[^\]\n]* ([0-9a-f]{7,40})\]", re.M)
# What git or a hook prints when a commit is refused.
FAILED = re.compile(r"index\.lock|nothing (?:added )?to commit|^fatal:|hook .*(?:exit|fail|reject)", re.M | re.I)


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


def position(where):
    """Return [HEAD sha, HEAD reflog entry count] in `where`; ["", 0] before the first commit."""
    return [git(where, "rev-parse", "HEAD"), len(git(where, "reflog", "show", "--format=%h", "HEAD").splitlines())]


def baseline_dir(where):
    """Return the directory holding before-states, or None outside a repository."""
    common = git(where, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) / "harness" / "commits" if common else None


def call_key(event):
    key = event.get("tool_use_id") or hashlib.sha256(
        f"{event.get('session_id')}|{event['tool_input']['command']}".encode()).hexdigest()
    return "".join(c for c in str(key) if c.isalnum() or c in "-_")


def save_baseline(where, event):
    folder = baseline_dir(where)
    if folder is None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        if time.time() - old.stat().st_mtime > STALE_SECONDS:
            old.unlink(missing_ok=True)
    (folder / f"{call_key(event)}.{time.time_ns()}.{os.getpid()}.json").write_text(json.dumps(position(where)))


def take_baseline(where, event):
    """Return and remove the oldest baseline recorded for this call, or None."""
    folder = baseline_dir(where)
    for path in sorted(folder.glob(f"{call_key(event)}.*.json")) if folder else []:
        try:
            before = json.loads(path.read_text())
            path.unlink()
        except (OSError, ValueError):
            continue  # a parallel Post took it first
        return before
    return None


def verdict(before, where, output):
    """Return the reason the commit run in `where` is unverified, or None when it landed.

    before is the recorded position, or None when no baseline exists.
    """
    head = git(where, "rev-parse", "HEAD")
    reported = LANDED.findall(output)
    failed = FAILED.search(output)
    if before is None:
        if not head or (reported and head.startswith(reported[-1])):
            return None
        why = ("No HEAD was recorded before this call, so git's output was checked instead, and it holds "
               "no `[branch sha]` line equal to HEAD. A `git commit -q`, or a wrapper that condenses output, is "
               "blocked on this path although the commit may have landed; confirm with `git log -1`.")
    elif reported and not head.startswith(reported[-1]):
        why = (f"git printed commit {reported[-1]}, but HEAD is {head[:12]}. "
               "The commit just reported is not the one this branch is on.")
    elif position(where) == before:
        why = (f"HEAD was {before[0][:12] or '(unborn)'} before the call and has not moved, so no commit was created. "
               "An index.lock collision, or an exit code masked by a later "
               "command in the same invocation, both look exactly like this.")
    elif failed and not reported:
        why = (f"HEAD moved, but git's output reports a failure ({failed.group(0)!r}) and no `[branch sha]` line. "
               "Another commit in this worktree may have moved HEAD while this one failed.")
    else:
        return None
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
    if baseline_dir(where) is None:
        return  # not a repository: nothing to verify against
    before = take_baseline(where, event)
    reason = verdict(before, where, text_of(event.get("tool_response") or {}))
    head = git(where, "rev-parse", "--short", "HEAD") or "-"
    record("verify_commit_landed", "block" if reason else "pass",
           "not-landed" if reason else ("landed" if before else "landed-no-baseline"), head, event)
    if reason:
        json.dump({"decision": "block", "reason": reason,
                   "systemMessage": "git commit did not land - see the blocked reason"},
                  sys.stdout)


if __name__ == "__main__":
    main()
