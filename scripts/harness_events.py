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
    python3 scripts/harness_events.py report [--weeks N] [--log PATH] [--json]

`report` is the evidence for pruning a control: decisions per hook per ISO week
(UTC) over the log and its `.1` backup, then the registered hooks and gates that
left no trace. It only reads. It reads harness.toml with tomllib inside the
report path, so importing this module (which hooks do, from a bare copy) needs
neither harness_config nor tomllib.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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


FIRING = frozenset({"deny", "block"})
# A registered command names its script as `.claude/hooks/<stem>.py` or
# `scripts/<stem>.py`; the crash-closing wrapper repeats the bare file name, which
# has no directory and so does not match.
HOOK_SCRIPT = re.compile(r"(?<![\w.-])(?:\.claude/hooks|scripts)/(\w+)\.py")


@dataclass(frozen=True)
class Registration:
    """One `[[hook]]` entry; `script` is the stem records are filed under."""

    name: str
    event: str
    kind: str
    script: str | None


def parse_record(raw):
    """Return (UTC time, hook, decision) for one log line, or None when it is not a record."""
    try:
        row = json.loads(raw)
        hook, decision = row["hook"], row["decision"]
        when = datetime.fromisoformat(row["ts"])
    except (ValueError, KeyError, TypeError):
        return None
    if (
        when.tzinfo is None
        or not isinstance(hook, str)
        or not isinstance(decision, str)
    ):
        return None
    return when.astimezone(timezone.utc), hook, decision


def read_records(path):
    """Read the log's `.1` backup then the live file; never writes.

    Returns:
        (list, int, list): (time, hook, decision) records, the count of lines
        that are not records, and the files that exist.
    """
    records, malformed, files = [], 0, []
    for source in (path.with_name(path.name + ".1"), path):
        if not source.is_file():
            continue
        files.append(str(source))
        for raw in source.read_bytes().splitlines():
            parsed = parse_record(raw)
            if parsed is None:
                malformed += 1
            else:
                records.append(parsed)
    return records, malformed, files


def read_registrations(config):
    """Return the `[[hook]]` entries of the harness.toml at `config`.

    Raises:
        OSError: the file cannot be read.
        ValueError: it is not TOML (tomllib.TOMLDecodeError) or a `[[hook]]`
            entry has no name or command.
    """
    import tomllib

    hooks = tomllib.loads(config.read_text(encoding="utf-8")).get("hook", [])
    registrations = []
    for entry in hooks:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("command"), str)
        ):
            raise ValueError("a [[hook]] entry needs a name and a command")
        found = HOOK_SCRIPT.search(entry["command"])
        registrations.append(
            Registration(
                entry["name"],
                str(entry.get("event", "")),
                str(entry.get("kind", "")),
                found.group(1) if found else None,
            )
        )
    return registrations


def report_data(log, config, weeks=None, now=None):
    """Count decisions per hook, decision and ISO week, and find registered silent controls.

    Args:
        log (Path): the live log; `<log>.1` is read as well.
        config (Path): the harness.toml whose `[[hook]]` entries are registered.
        weeks (int | None): keep records from the last N ISO weeks, the current
            one included, opening at Monday 00:00 UTC. None keeps every record.
        now (datetime | None): a timezone-aware UTC clock; tests pin it.

    Returns:
        dict: JSON-ready; `registered.available` is False, with its lists None,
        when harness.toml could not be read.
    """
    since = None
    if weeks is not None:
        now = now or datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since = monday - timedelta(weeks=weeks - 1)
    records, malformed, files = read_records(log)
    kept = [r for r in records if since is None or r[0] >= since]
    cells = {}
    for when, hook, decision in kept:
        iso = when.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
        per_week = cells.setdefault((hook, decision), {})
        per_week[label] = per_week.get(label, 0) + 1
    try:
        registrations = read_registrations(config)
    except (OSError, ValueError) as exc:
        registered = {
            "available": False,
            "error": f"{config}: {type(exc).__name__}: {exc}",
            "no_records": None,
            "gates_without_deny_or_block": None,
        }
    else:
        seen = {hook for _, hook, _ in kept}
        firing = {hook for _, hook, decision in kept if decision in FIRING}
        registered = {
            "available": True,
            "error": None,
            "no_records": [asdict(r) for r in registrations if r.script not in seen],
            "gates_without_deny_or_block": [
                asdict(r)
                for r in registrations
                if r.kind == "gate" and r.script not in firing
            ],
        }
    return {
        "files": files,
        "malformed": malformed,
        "records": len(kept),
        "registered": registered,
        "rows": [
            {
                "hook": hook,
                "decision": decision,
                "weeks": dict(sorted(per_week.items())),
                "total": sum(per_week.values()),
            }
            for (hook, decision), per_week in sorted(cells.items())
        ],
        "weeks": sorted({label for per_week in cells.values() for label in per_week}),
        "window": {
            "weeks": weeks,
            "since": since.isoformat() if since else None,
        },
    }


def render_report(data):
    """Return the text report for one `report_data` result."""
    window = data["window"]
    lines = [
        "decision log: all records"
        if window["weeks"] is None
        else f"decision log: --weeks {window['weeks']}, since {window['since']}",
        "read: " + (", ".join(data["files"]) or "no log file found"),
        f"records in window: {data['records']}; malformed lines: {data['malformed']}",
        "",
    ]
    if data["rows"]:
        grid = [["hook", "decision", *data["weeks"], "total"]]
        for row in data["rows"]:
            counts = [str(row["weeks"].get(week, "-")) for week in data["weeks"]]
            grid.append([row["hook"], row["decision"], *counts, str(row["total"])])
        widths = [max(len(line[i]) for line in grid) for i in range(len(grid[0]))]
        for line in grid:
            cells = [
                cell.ljust(width) if i < 2 else cell.rjust(width)
                for i, (cell, width) in enumerate(zip(line, widths))
            ]
            lines.append("  ".join(cells).rstrip())
    else:
        lines.append("no records in window")
    lines.append("")
    registered = data["registered"]
    if not registered["available"]:
        lines.append(f"registered hooks: unavailable ({registered['error']})")
        return "\n".join(lines)
    for header, key in (
        ("registered, no records in window:", "no_records"),
        (
            "registered gates with no deny or block in window:",
            "gates_without_deny_or_block",
        ),
    ):
        lines.append(header)
        lines.extend(
            f"  {r['name']}  ({r['script'] or 'no script found in command'})"
            for r in registered[key]
        )
        if not registered[key]:
            lines.append("  none")
        lines.append("")
    lines.append(
        "Records are filed per script, not per registration; a hook that records "
        "nothing, or only denials, is listed above whether or not it ran."
    )
    return "\n".join(lines)


def positive_int(text):
    """argparse type: an integer of at least 1."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def report_main(args):
    """Print the report; return the exit status."""
    path = args.log or log_path()
    if path is None:
        print("not inside a git repository", file=sys.stderr)
        return 2
    from paths import REPO  # lazy: hooks import this module without paths.py

    data = report_data(path, REPO / "harness.toml", weeks=args.weeks)
    print(
        json.dumps(data, indent=2, sort_keys=True) if args.json else render_report(data)
    )
    return 0


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
    rep = sub.add_parser("report", help="fire rates per hook per ISO week")
    rep.add_argument("--weeks", type=positive_int, help="keep the last N ISO weeks")
    rep.add_argument("--log", type=Path, help="log to read, plus PATH.1")
    rep.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args()
    if args.cmd == "record":
        # Exit 0 whatever happens: the caller's outcome is already decided.
        record(args.hook, args.decision, args.reason, args.subject)
        return 0
    if args.cmd == "report":
        return report_main(args)
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
