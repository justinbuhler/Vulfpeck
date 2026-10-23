#!/usr/bin/env python3
"""Append-only, size-rotated record of the decisions the harness's gates make.

A log of denials alone cannot be audited: a gate that silently started allowing
everything leaves an empty log, which reads the same as a gate with nothing to
deny. So the low-volume gates record every decision, allow and deny alike: each
Agent call (require_agent_model, model_router), each verified commit
(verify_commit_landed), each land (scripts/land.sh).

One plain JSON object per line. When the file reaches MAX_BYTES it is moved to
`events.jsonl.1`, replacing the previous backup, so the log holds at most about
twice that.

Rules every caller relies on:
  - record() never raises and never prints to stdout. Logging must not change a
    decision, and a hook's stdout is its decision.
  - No prompts, commands or tool output are recorded; only identifiers, a reason
    code and a short subject (an agent type and model, a SHA).
  - No gate reads this log. A gate deriving a verdict from earlier verdicts
    would be a gate checking itself.

The log lives in the repository's common git directory, shared by every worktree
and never committed: <git common dir>/harness/events.jsonl.

    python3 scripts/harness_events.py tail [-n N]   # last N records
    python3 scripts/harness_events.py record --hook land --decision landed --subject SHA
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
MAX_BYTES = (
    5 * 1024 * 1024
)  # not in harness.toml: hooks run copied beside this file, without harness_config
SUBJECT_MAX = 160


def log_path(cwd=None):
    """Return the event log path for the repository at cwd, or None outside git.

    HARNESS_EVENTS_LOG overrides it. Tests set it, because a test that runs a
    real hook from inside this checkout would otherwise write fixture decisions
    into the real record; scripts/standalone_tests.sh and mutants/runner.py set
    it for every test they run, as a backstop.
    """
    override = os.environ.get("HARNESS_EVENTS_LOG")
    if override:
        return Path(override)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return Path(done.stdout.strip()) / "harness" / "events.jsonl"


def rotate(path):
    """Move a log at or over MAX_BYTES to `<path>.1`, replacing the old backup."""
    if path.is_file() and path.stat().st_size >= MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))


def record(hook, decision, reason="", subject="", payload=None, cwd=None):
    """Append one decision. Never raises; returns True when it was written.

    Args:
        hook (str): the control that decided, e.g. "model_router".
        decision (str): "allow", "deny", "block", "pass", "landed" or "refused".
        reason (str): a short reason code, never free text from the tool call.
        subject (str): what was decided about: "Explore/opus", a commit SHA.
        payload (dict | None): the hook input, for session and agent identity.
        cwd (str | None): directory whose repository owns the log.
    """
    try:
        payload = payload if isinstance(payload, dict) else {}
        path = log_path(cwd or payload.get("cwd"))
        if path is None:
            return False
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "v": SCHEMA,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hook": str(hook),
            "decision": str(decision),
            "reason": str(reason)[:80],
            "subject": str(subject)[:SUBJECT_MAX],
            "session": str(payload.get("session_id") or ""),
            "agent_id": str(payload.get("agent_id") or ""),
            "agent_type": str(payload.get("agent_type") or ""),
        }
        # Lock a sidecar, not the log: rotation replaces the log's inode under a held lock.
        with open(path.with_name(path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rotate(path)
            with open(path, "ab") as handle:
                handle.write((json.dumps(event, sort_keys=True) + "\n").encode("utf-8"))
        return True
    except Exception as exc:  # logging must never change a decision
        try:
            print(
                f"harness_events: not recorded ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
        except Exception:
            pass
        return False


def main():
    parser = argparse.ArgumentParser(description="Inspect the harness decision log.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    tail = sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=20)
    rec = sub.add_parser("record", help="append one decision (for shell controls)")
    for flag in ("--hook", "--decision"):
        rec.add_argument(flag, required=True)
    rec.add_argument("--reason", default="")
    rec.add_argument("--subject", default="")
    args = parser.parse_args()
    if args.cmd == "record":
        # Exit 0 whatever happens: the caller's outcome is already decided.
        record(args.hook, args.decision, args.reason, args.subject)
        return 0
    path = log_path()
    if path is None:
        print("not inside a git repository", file=sys.stderr)
        return 2
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines()[-args.n :]:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
