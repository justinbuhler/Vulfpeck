#!/usr/bin/env python3
"""Lint before the session stops; record where the session left the repository.

Stop fires after every reply of the main agent, not once per session, so this
stays quiet unless something changed:

    Stop        if the tree is dirty or HEAD or status changed since this
                session's last check, run harness.toml commands.lint, on the .py
                files changed since the merge-base with main plus untracked ones
                when the command is `ruff check .`, else on the whole tree. A
                pre-existing failure in an untouched file never blocks. A failure blocks the stop
                once with the output; on the continuation (stop_hook_active) the
                session may stop and is told lint still fails. Uncommitted or
                unlanded work gets a one-line notice when that state changed since
                the last notice, and never blocks: a dirty tree between replies
                is ordinary work in progress.
    SessionEnd  record branch, HEAD, dirty paths, and commits not yet on local
                main or origin/main in the harness decision log. Cannot block.

Per-session state lives in <git common dir>/harness/sessions/<session>.json.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT_TIMEOUT = 50
TAIL = 25


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_events

        harness_events.record(*args, **kwargs)
    except Exception:
        pass


ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15, env=ENV
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def count(cwd, spec):
    out = git(cwd, "rev-list", "--count", spec)
    return int(out) if out and out.isdigit() else None


def state(top):
    """Return the repository state this hook reports on."""
    status = git(top, "status", "--porcelain=v1", "--untracked-files=all") or ""
    head = git(top, "rev-parse", "HEAD") or ""
    has = lambda ref: git(top, "rev-parse", "--verify", "-q", ref) is not None  # noqa: E731
    return {
        "branch": git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
        or "(detached)",
        "head": head,
        "dirty": len(status.splitlines()),
        "unlanded": count(top, "main..HEAD")
        if has("refs/heads/main") and head
        else None,
        "unpublished": count(top, "origin/main..HEAD")
        if has("refs/remotes/origin/main") and head
        else None,
        "tree": hashlib.sha256((head + "\n" + status).encode()).hexdigest(),
    }


def lint_command(top):
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_config

        return harness_config.load(top)["commands"]["lint"]
    except Exception as exc:
        return f"__unreadable__:{exc}"


RUFF_ALL = re.compile(r"(?:uvx )?ruff check \.")


def changed_py(top):
    """Return the .py files this branch or session changed, or None when that is unknown.

    Changed means: differs from the merge-base with local main (else origin/main),
    in the commits or the working tree, plus untracked files. Deleted files are left out.
    """
    base = next(
        (
            b
            for ref in ("main", "origin/main")
            if (b := git(top, "merge-base", "HEAD", ref))
        ),
        None,
    )
    if not base:
        return None
    tracked = git(top, "diff", "-z", "--name-only", "--diff-filter=d", base)
    untracked = git(top, "ls-files", "-z", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    return sorted(
        {f for f in (tracked + "\0" + untracked).split("\0") if f.endswith(".py")}
    )


def scoped(top, command):
    """Return the lint command to run: `ruff check .` limited to the changed .py files,
    "" when none changed, any other command unchanged (it cannot take paths)."""
    files = changed_py(top) if RUFF_ALL.fullmatch(command) else None
    if files is None:
        return command
    return (
        f"{command[:-1]}--force-exclude {' '.join(shlex.quote(f) for f in files)}"
        if files
        else ""
    )


def session_file(top, session):
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    safe = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in (session or "unknown")
    )
    return Path(common) / "harness" / "sessions" / f"{safe}.json"


def notice(s):
    parts = []
    if s["dirty"]:
        parts.append(f"{s['dirty']} uncommitted path(s)")
    if s["unlanded"]:
        parts.append(
            f"{s['unlanded']} commit(s) on {s['branch']} not on local main (./scripts/land.sh)"
        )
    elif s["unpublished"]:
        parts.append(
            f"{s['unpublished']} commit(s) not on origin/main (publish via a pull request)"
        )
    return "session_guard: " + "; ".join(parts) if parts else ""


def on_stop(payload, top):
    path = session_file(top, payload.get("session_id"))
    if path is None:
        return 0
    try:
        memo = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        memo = {}  # unreadable state: start fresh, lint again
    now = state(top)
    out = {}

    # Porcelain labels do not change when an already-dirty file is edited again.
    if now["dirty"] or now["tree"] != memo.get("linted_tree") or memo.get("lint_failed"):
        command = lint_command(top)
        if command.startswith("__unreadable__:"):
            out["systemMessage"] = (
                f"session_guard: lint not run, harness.toml unreadable: {command[15:]}"
            )
        elif command:
            run = scoped(top, command)
            failed, text = False, ""
            if run:
                try:
                    done = subprocess.run(
                        ["bash", "-c", run],
                        cwd=top,
                        capture_output=True,
                        text=True,
                        timeout=LINT_TIMEOUT,
                        env=ENV,
                    )
                    failed, text = done.returncode != 0, f"{done.stdout}{done.stderr}"
                except subprocess.TimeoutExpired:
                    failed = None
                    out["systemMessage"] = (
                        f"session_guard: lint exceeded {LINT_TIMEOUT}s; not checked"
                    )
            if failed is not None:  # a timed-out tree was not checked: try it again
                memo.update(linted_tree=now["tree"], lint_failed=failed)
            if failed:
                tail = "\n".join(text.strip().splitlines()[-TAIL:])
                subject = f"{now['branch']}@{now['head'][:9]}"
                if payload.get("stop_hook_active"):
                    record(
                        "session_guard",
                        "block-overridden",
                        "lint-failing",
                        subject,
                        payload,
                    )
                    out["systemMessage"] = (
                        f"session_guard: lint still fails ({run}):\n{tail}"
                    )
                else:
                    record("session_guard", "block", "lint-failing", subject, payload)
                    out = {
                        "decision": "block",
                        "reason": f"Lint fails ({run}). Fix it before stopping:\n{tail}",
                    }

    if "decision" not in out:
        message = notice(now)
        key = json.dumps(
            [now["dirty"], now["unlanded"], now["unpublished"], now["head"]]
        )
        if message and key != memo.get("noticed"):
            memo["noticed"] = key
            out["systemMessage"] = "\n".join(
                filter(None, [out.get("systemMessage"), message])
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memo))
    if out:
        print(json.dumps(out))
    return 0


def on_session_end(payload, top):
    s = state(top)
    reason = (
        f"dirty={s['dirty']} unlanded={s['unlanded']} unpublished={s['unpublished']}"
    )
    record(
        "session_guard",
        "session-end",
        reason,
        f"{s['branch']}@{s['head'][:9]}",
        payload,
    )
    path = session_file(top, payload.get("session_id"))
    if path is not None and path.is_file():
        path.unlink()
    return 0


def trusted(top):
    """Return whether top is the project this hook belongs to, or one of its worktrees.

    The lint command comes from top's harness.toml, so a session that has cd'd
    into some other repository must not run that repository's command.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR") or str(ROOT)
    mine = git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    theirs = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return bool(mine) and mine == theirs


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        return 0
    top = git(payload.get("cwd") or os.getcwd(), "rev-parse", "--show-toplevel")
    if not top or not trusted(top):
        return 0
    event = payload.get("hook_event_name")
    if event == "Stop":
        return on_stop(payload, top)
    if event == "SessionEnd":
        return on_session_end(payload, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
