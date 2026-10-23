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
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# `[main 1a2b3c4] subject` - the one line a successful commit prints.
LANDED = re.compile(r"^\[[^\]\n]* ([0-9a-f]{7,40})\]", re.M)


def commit_targets(command, cwd=None):
    """Return execution directories for plain, git -C, and RTK commit commands."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    words, segments = [], []
    for word in lexer:
        if word and all(c in ";&|()\n" for c in word):
            segments.append(words)
            words = []
        else:
            words.append(word)
    segments.append(words)
    here, targets = Path(cwd or os.getcwd()), []
    for words in segments:
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", words[0]):
            words = words[1:]
        if words[:1] == ["cd"] and len(words) == 2:
            here = (here / words[1]).resolve()
            continue
        if words and Path(words[0]).name == "rtk":
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
        if not words or Path(words[0]).name != "git":
            continue
        rest, target = words[1:], here
        while rest and rest[0].startswith("-"):
            flag = rest[0]
            if flag in ("-C", "-c") and len(rest) > 1:
                if flag == "-C":
                    target = target / rest[1]
                rest = rest[2:]
            elif flag.startswith("-C") and len(flag) > 2:
                target, rest = target / flag[2:], rest[1:]
            elif flag.startswith(("--git-dir", "--work-tree")):
                raise ValueError("commit proof does not support git-dir/work-tree overrides; use git -C")
            else:
                rest = rest[1:]
        if rest[:1] == ["commit"] and "--dry-run" not in rest:
            targets.append(target.resolve())
    return list(dict.fromkeys(targets))


def ran_commit(command):
    """Return whether a supported shell segment invokes git commit."""
    return bool(commit_targets(command))


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


def git(*args, cwd=None):
    """Run git in the session's repository; empty string if it cannot answer."""
    try:
        done = subprocess.run(("git",) + args, capture_output=True, text=True,
                              timeout=5, env=ENV, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def state(cwd):
    """Read independent HEAD, branch, reachability, and status evidence."""
    root = git("rev-parse", "--show-toplevel", cwd=cwd)
    if not root:
        raise ValueError(f"commit checkout cannot be read: {cwd}")
    branch = git("symbolic-ref", "--quiet", "HEAD", cwd=root) or "HEAD"
    head = git("rev-parse", "--verify", "HEAD", cwd=root)
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                            cwd=root, env=ENV, capture_output=True, text=True, timeout=5)
    if status.returncode:
        raise ValueError(f"commit status cannot be read: {status.stderr.strip()}")
    return {"root": root, "head": head, "branch": branch,
            "branch_head": git("rev-parse", "--verify", branch, cwd=root),
            "status": status.stdout}


def receipt_path(event, target):
    """Locate one before/after receipt using stable native or Claude call IDs."""
    session, call = event.get("session_id"), event.get("tool_use_id")
    if not session or not call:
        return None
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=target)
    if not common:
        raise ValueError(f"commit Git common directory cannot be read: {target}")
    key = hashlib.sha256(f"{session}\0{call}\0{target}".encode()).hexdigest()
    return Path(common) / "harness" / "commits" / f"{key}.json"


def verdict(command, response, cwd=None, before=None, target=None):
    """Return a failure unless independent state confirms the reported commit."""
    targets = commit_targets(command, cwd) if isinstance(command, str) else []
    if not targets:
        return None
    after = state(target or targets[-1])
    head = after["head"]
    reported = LANDED.findall(text_of(response))
    if before and (before["branch"] != after["branch"] or before["head"] == head):
        why = "HEAD did not advance on the intended branch recorded before this tool call."
    elif head and head == after["branch_head"] and (
            (reported and head.startswith(reported[-1])) or (before and not reported)):
        return None
    elif reported:
        why = (f"git printed commit {reported[-1]}, but HEAD is {head[:12]}. "
               "The commit just reported is not the one this branch is on.")
    else:
        why = ("git printed no `[branch sha]` line and no matching pre-tool state proves a commit. "
               "Quiet or filtered output requires the registered PreToolUse commit receipt.")
    # Asked for as a standing warning; it earns its place only here. A lock is
    # held during a git operation and gone after, so its presence after the call
    # explains a commit that did not land - and says nothing after an `ls`.
    lock = git("rev-parse", "--git-path", "index.lock", cwd=after["root"])
    if lock and os.path.exists(lock):
        why += f" {lock} is still present, so a peer worktree likely held the index."
    return (f"Commit not verified. {why} HEAD is now: {head or '(unknown)'}. "
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
        return 2
    command = (event.get("tool_input") or {}).get("command", "")
    reason, targets = None, []
    try:
        targets = commit_targets(command, event.get("cwd")) if isinstance(command, str) else []
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        if len(targets) > 1:
            raise ValueError("commit proof supports one checkout per tool call")
        for target in targets:
            path = receipt_path(event, target)
            if event.get("hook_event_name") == "PreToolUse":
                if path is None:
                    raise ValueError("commit preflight requires session_id and tool_use_id")
                before = state(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"command_sha256": command_hash, "before": before}) + "\n")
                continue
            receipt = json.loads(path.read_text()) if path and path.is_file() else {}
            if path and not receipt:
                raise ValueError("matching PreToolUse commit receipt is missing")
            if receipt and receipt.get("command_sha256") != command_hash:
                raise ValueError("commit receipt command differs from this tool call")
            reason = verdict(command, event.get("tool_response") or {}, event.get("cwd"),
                             receipt.get("before"), target)
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({**receipt, "command_sha256": command_hash, "after": state(target),
                                            "verified": not reason}) + "\n")
            if reason:
                break
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        reason = f"Commit not verified: {exc}"
    if targets:
        head = git("rev-parse", "--short", "HEAD", cwd=targets[-1]) or "-"
        record("verify_commit_landed", "block" if reason else "pass",
               "not-landed" if reason else "preflight" if event.get("hook_event_name") == "PreToolUse" else "landed",
               head, event)
    if reason:
        if event.get("hook_event_name") == "PreToolUse":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "deny", "permissionDecisionReason": reason}}))
        else:
            print(json.dumps({"decision": "block", "reason": reason,
                              "systemMessage": "git commit is unverified - see the blocked reason"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
