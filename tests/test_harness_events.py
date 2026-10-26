"""The harness decision log records allows and denies, rotates by size, and never decides.

Expectations are literals (CLAUDE.md Rule 11). Every write goes to a scratch
HARNESS_EVENTS_LOG; nothing here touches the real record in .git/harness/.
"""

import fcntl
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import harness_events  # noqa: E402

HOOKS = ROOT / ".claude" / "hooks"
SCRATCH = Path(tempfile.mkdtemp(prefix="harness-events-test."))
os.environ["HARNESS_EVENTS_LOG"] = str(SCRATCH / "unused.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


def fresh(name):
    path = SCRATCH / f"{name}.jsonl"
    os.environ["HARNESS_EVENTS_LOG"] = str(path)
    return path


def rows(path):
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


# --- flat lines ----------------------------------------------------------------
log = fresh("flat")
for i in range(3):
    harness_events.record(
        "model_router",
        "allow",
        "tier-match",
        f"Explore/haiku#{i}",
        {"session_id": "s1", "agent_id": "a1", "agent_type": "reviewer-code"},
    )
got = rows(log)
check("three records are written", len(got) == 3)
check("a record has no hash link", all("prev" not in row for row in got))
check(
    "the subjects are kept in order",
    [r["subject"] for r in got] == [f"Explore/haiku#{i}" for i in range(3)],
)
check(
    "identity fields come from the hook payload",
    (got[0]["session"], got[0]["agent_id"], got[0]["agent_type"])
    == ("s1", "a1", "reviewer-code"),
)
check(
    "the verify subcommand is gone",
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "harness_events.py"), "verify"],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    ).returncode
    == 2,
)
check(
    "the module has no chain helpers",
    not any(
        hasattr(harness_events, name) for name in ("verify", "line_hash", "GENESIS")
    ),
)

# --- rotation --------------------------------------------------------------------
log = fresh("rotate")
backup = log.with_name(log.name + ".1")
harness_events.MAX_BYTES = 600
for i in range(6):
    harness_events.record("h", "allow", "", f"n{i:02d}")
harness_events.MAX_BYTES = 5 * 1024 * 1024
check("a log over the cap is moved to .1", backup.is_file() and log.is_file())
check(
    "rotation keeps every record across the two files",
    [r["subject"] for r in rows(backup) + rows(log)] == [f"n{i:02d}" for i in range(6)],
)
check(
    "a rotated file stays under the cap plus one record",
    backup.stat().st_size < 600 + 400,
    str(backup.stat().st_size),
)
log.write_text("x" * 500)
backup.write_text("old backup")
harness_events.MAX_BYTES = 400
harness_events.record("h", "allow", "", "after")
harness_events.MAX_BYTES = 5 * 1024 * 1024
check("a second rotation replaces the backup", backup.read_text() == "x" * 500)
check(
    "the new log starts with the new record",
    [r["subject"] for r in rows(log)] == ["after"],
)

# --- what is recorded is bounded ----------------------------------------------
log = fresh("bounded")
harness_events.record("h", "deny", "r" * 500, "s" * 500)
row = rows(log)[0]
check(
    "reason and subject are truncated",
    len(row["reason"]) == 80 and len(row["subject"]) == 160,
)
check(
    "a record carries exactly the declared fields",
    set(row)
    == {
        "v",
        "ts",
        "hook",
        "decision",
        "reason",
        "subject",
        "session",
        "agent_id",
        "agent_type",
    },
    str(sorted(row)),
)


# --- concurrent writers lose nothing -----------------------------------------
def writer(args):
    path, n = args
    os.environ["HARNESS_EVENTS_LOG"] = path
    for i in range(n):
        harness_events.record("concurrent", "allow", "", f"w{i}")


log = fresh("concurrent")
with multiprocessing.get_context("fork").Pool(8) as pool:
    pool.map(writer, [(str(log), 25)] * 8)
check(
    "200 concurrent appends are all present", len(rows(log)) == 200, str(len(rows(log)))
)
check(
    "every concurrent line is whole JSON", all(isinstance(r, dict) for r in rows(log))
)

# --- record() never raises, never prints to stdout -------------------------------
blocker = SCRATCH / "a-file"
blocker.write_text("x")
os.environ["HARNESS_EVENTS_LOG"] = str(blocker / "sub" / "events.jsonl")
probe = subprocess.run(
    [
        sys.executable,
        "-c",
        "import sys; sys.path.insert(0, 'scripts'); import harness_events as h; "
        "print('result', h.record('h', 'allow'))",
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
    env=dict(os.environ),
)
check(
    "an unwritable log returns False without raising",
    probe.stdout.strip() == "result False",
    probe.stdout + probe.stderr,
)
check(
    "an unwritable log is reported on stderr",
    "not recorded" in probe.stderr,
    probe.stderr,
)


# --- hooks record allow and deny, and logging cannot change a decision ------------
def hook(script, payload, log_path):
    env = {**os.environ, "HARNESS_EVENTS_LOG": str(log_path)}
    done = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    return done.returncode, done.stdout


def agent(kind, model):
    tool_input = {"description": "x", "prompt": "y", "subagent_type": kind}
    if model:
        tool_input["model"] = model
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "session_id": "s2",
        "agent_type": "",
    }


log = fresh("hooks")
hook("model_router.py", agent("Explore", "opus"), log)
hook("model_router.py", agent("Explore", "haiku"), log)
hook("model_router.py", agent("general-purpose", "opus"), log)
hook("require_agent_model.py", agent("Explore", None), log)
hook("require_agent_model.py", agent("Explore", "haiku"), log)
seen = [(r["hook"], r["decision"], r["reason"], r["subject"]) for r in rows(log)]
check(
    "hooks record every Agent decision, allow and deny",
    seen
    == [
        ("model_router", "deny", "tier-mismatch", "Explore/opus"),
        ("model_router", "allow", "tier-match", "Explore/haiku"),
        ("model_router", "allow", "unlisted", "general-purpose/opus"),
        ("require_agent_model", "deny", "model-missing", "Explore/-"),
        ("require_agent_model", "allow", "model-stated", "Explore/haiku"),
    ],
    str(seen),
)
check(
    "a non-Agent tool call is not recorded",
    hook(
        "model_router.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        fresh("other"),
    )[0]
    == 0
    and rows(SCRATCH / "other.jsonl") == [],
)

unwritable = blocker / "nope" / "events.jsonl"
for script, payload in (
    ("model_router.py", agent("Explore", "opus")),
    ("require_agent_model.py", agent("Explore", None)),
):
    rc, out = hook(script, payload, unwritable)
    check(
        f"{script} still denies when the log cannot be written",
        rc == 0
        and json.loads(out or "{}")
        .get("hookSpecificOutput", {})
        .get("permissionDecision")
        == "deny",
        f"rc={rc} {out}",
    )

# --- verify_commit_landed records each commit it checks -------------------------
with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", "-b", "work"], cwd=repo, env=genv, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        env=genv,
        check=True,
    )
    base_head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        env=genv,
        capture_output=True,
        text=True,
    ).stdout.strip()
    log = fresh("commits")
    hook_env = {**genv, "HARNESS_EVENTS_LOG": str(log)}

    def commit_event(name, command, response=None):
        event = {
            "hook_event_name": name,
            "session_id": "s",
            "tool_input": {"command": command},
        }
        if response is not None:
            event["tool_response"] = {"stdout": response}
        subprocess.run(
            [sys.executable, str(HOOKS / "verify_commit_landed.py")],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            cwd=repo,
            env=hook_env,
        )

    commit_event("PreToolUse", "git commit -m x")
    commit_event("PostToolUse", "git commit -m x", "nothing to commit")
    commit_event("PreToolUse", "git commit -m y")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "y",
        ],
        cwd=repo,
        env=genv,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        env=genv,
        capture_output=True,
        text=True,
    ).stdout.strip()
    commit_event("PostToolUse", "git commit -m y", "")
    commit_event("PostToolUse", "ls", "")
    seen = [(r["decision"], r["reason"], r["subject"]) for r in rows(log)]
    check(
        "verify_commit_landed records a blocked and a verified commit, and nothing else",
        seen == [("block", "not-landed", base_head), ("pass", "landed", head)],
        str(seen),
    )

# --- the record CLI used by land.sh exits 0 whatever happens -----------------------
done = subprocess.run(
    [
        sys.executable,
        str(ROOT / "scripts" / "harness_events.py"),
        "record",
        "--hook",
        "land",
        "--decision",
        "landed",
        "--subject",
        "b@1 -> 2",
    ],
    capture_output=True,
    text=True,
    env={**os.environ, "HARNESS_EVENTS_LOG": str(unwritable)},
)
check("record CLI exits 0 even when it cannot write", done.returncode == 0, done.stderr)
check("record CLI prints nothing to stdout", done.stdout == "", done.stdout)

# --- report: fire rates over the live log and its .1 backup ----------------------
# Fixtures carry literal timestamps so every expected count and week label below
# is a literal too. 2026-03-02 is a Monday: ISO 2026-W10 runs 03-02..03-08 and
# 2026-W11 runs 03-09..03-15.
SCRIPT = ROOT / "scripts" / "harness_events.py"
NO_PYTHONPATH = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def event_line(ts, hook, decision):
    return json.dumps(
        {
            "v": 1,
            "ts": ts,
            "hook": hook,
            "decision": decision,
            "reason": "",
            "subject": "",
            "session": "",
            "agent_id": "",
            "agent_type": "",
        },
        sort_keys=True,
    )


def registration(name, kind, script):
    # The real registrations wrap the script path in a crash-closing shell
    # command; the wrapper names the script a second time without a directory.
    stem = script.rsplit("/", 1)[1]
    return (
        "[[hook]]\n"
        f'name = "{name}"\nevent = "PreToolUse"\nkind = "{kind}"\n'
        "command = '''python3 \"${CLAUDE_PROJECT_DIR}/" + script + '" || { rc=$?; '
        'echo "' + stem + " crashed (rc=$rc); failing closed\" >&2; exit 2; }'''\n\n"
    )


FIXTURE_TOML = "".join(
    registration(*args)
    for args in (
        ("loop_guard_PreToolUse", "gate", ".claude/hooks/loop_guard.py"),
        ("loop_guard_PostToolUse", "gate", ".claude/hooks/loop_guard.py"),
        ("model_router_PreToolUse", "gate", ".claude/hooks/model_router.py"),
        (
            "require_agent_model_PreToolUse",
            "gate",
            ".claude/hooks/require_agent_model.py",
        ),
        (
            "verify_commit_landed_PreToolUse",
            "advisory",
            ".claude/hooks/verify_commit_landed.py",
        ),
        (
            "verify_commit_landed_PostToolUse",
            "gate",
            ".claude/hooks/verify_commit_landed.py",
        ),
        # Named for nothing the log knows: only its command ties it to turn_guard.
        ("renamed_gate_Stop", "gate", ".claude/hooks/turn_guard.py"),
        ("budget_guard_PreToolUse", "gate", ".claude/hooks/budget_guard.py"),
        ("queue_guard_PreToolUse", "advisory", ".claude/hooks/queue_guard.py"),
        ("readonly_guard_SubagentStart", "advisory", ".claude/hooks/readonly_guard.py"),
        ("harness_doctor_SessionStart", "advisory", "scripts/harness_doctor.py"),
    )
)

# 8 valid records, all in 2026-W11, and 2 malformed lines.
LIVE_LINES = [
    event_line("2026-03-09T00:00:00+00:00", "loop_guard", "block"),
    event_line("2026-03-10T10:00:00+00:00", "loop_guard", "block"),
    event_line("2026-03-10T11:00:00+00:00", "model_router", "allow"),
    event_line("2026-03-11T09:00:00+00:00", "model_router", "deny"),
    event_line("2026-03-11T09:05:00+00:00", "land", "landed"),
    event_line("2026-03-12T09:00:00+00:00", "turn_guard", "block"),
    event_line("2026-03-12T09:30:00+00:00", "queue_guard", "allow"),
    event_line("2026-03-12T10:00:00+00:00", "verify_commit_landed", "pass"),
    '{"v": 1, "ts": "2026-03-12T10:30:00+00:00", "hook": "loop_gu',
    "[1, 2]",
]
# 5 valid records, all in 2026-W10, and 1 malformed line (no ts).
BACKUP_LINES = [
    event_line("2026-03-02T08:00:00+00:00", "loop_guard", "block"),
    event_line("2026-03-08T23:00:00+00:00", "model_router", "allow"),
    event_line("2026-03-05T08:00:00+00:00", "task_loop", "reset"),
    event_line("2026-03-04T08:00:00+00:00", "require_agent_model", "allow"),
    event_line("2026-03-06T08:00:00+00:00", "verify_commit_landed", "block"),
    '{"hook": "loop_guard", "decision": "block"}',
]


def make_project(name, toml):
    """A repository-shaped folder: its own scripts/ copy decides where harness.toml is."""
    proj = SCRATCH / name
    (proj / "scripts").mkdir(parents=True)
    for module in ("harness_events.py", "paths.py"):
        shutil.copy2(ROOT / "scripts" / module, proj / "scripts")
    if toml == "DIR":
        (proj / "harness.toml").mkdir()
    elif toml is not None:
        (proj / "harness.toml").write_text(toml)
    return proj


def write_lines(path, lines):
    path.write_text("".join(line + "\n" for line in lines))


def run_report(proj, log, *args):
    # HARNESS_EVENTS_LOG stays set to an unrelated file: --log must win over it.
    return subprocess.run(
        [sys.executable, str(proj / "scripts" / "harness_events.py"), "report"]
        + ["--log", str(log)]
        + list(args),
        capture_output=True,
        text=True,
        env=NO_PYTHONPATH,
        cwd=SCRATCH,
    )


def section(text, header):
    """The indented lines under one `header:` line of the text report."""
    lines = text.splitlines()
    out = []
    for line in lines[lines.index(header) + 1 :]:
        if not line.startswith("  "):
            break
        out.append(line.strip())
    return out


def table(text):
    """The rows of the counts table as token lists, header first."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("hook "))
    rows = []
    for line in lines[start:]:
        if not line.strip():
            break
        rows.append(line.split())
    return rows


proj = make_project("report", FIXTURE_TOML)
log = proj / "events.jsonl"
write_lines(log, LIVE_LINES)
write_lines(log.with_name("events.jsonl.1"), BACKUP_LINES)
before = {p.name: p.read_bytes() for p in proj.iterdir() if p.is_file()}
done = run_report(proj, log)
after = {p.name: p.read_bytes() for p in proj.iterdir() if p.is_file()}
text = done.stdout
check("report exits 0 on a readable log", done.returncode == 0, done.stderr)
check(
    "report never writes, rotates or creates a lockfile",
    before == after
    and sorted(p.name for p in proj.iterdir())
    == ["events.jsonl", "events.jsonl.1", "harness.toml", "scripts"],
    str(sorted(p.name for p in proj.iterdir())),
)
check(
    "report counts per hook and decision per ISO week, from the live log and the .1 backup",
    table(text)
    == [
        ["hook", "decision", "2026-W10", "2026-W11", "total"],
        ["land", "landed", "-", "1", "1"],
        ["loop_guard", "block", "1", "2", "3"],
        ["model_router", "allow", "1", "1", "2"],
        ["model_router", "deny", "-", "1", "1"],
        ["queue_guard", "allow", "-", "1", "1"],
        ["require_agent_model", "allow", "1", "-", "1"],
        ["task_loop", "reset", "1", "-", "1"],
        ["turn_guard", "block", "-", "1", "1"],
        ["verify_commit_landed", "block", "1", "-", "1"],
        ["verify_commit_landed", "pass", "-", "1", "1"],
    ],
    text,
)
check(
    "report states how many records it counted and how many lines were malformed",
    "records in window: 13; malformed lines: 3" in text.splitlines(),
    text,
)
check(
    "report names the files it read, backup first",
    f"read: {log}.1, {log}" in text.splitlines(),
    text,
)
check(
    "report lists registered hooks with no record, mapped from the command path",
    section(text, "registered, no records in window:")
    == [
        "budget_guard_PreToolUse  (budget_guard)",
        "readonly_guard_SubagentStart  (readonly_guard)",
        "harness_doctor_SessionStart  (harness_doctor)",
    ],
    text,
)
check(
    "report lists a gate that has records but no deny or block, and one with no records",
    section(text, "registered gates with no deny or block in window:")
    == [
        "require_agent_model_PreToolUse  (require_agent_model)",
        "budget_guard_PreToolUse  (budget_guard)",
    ],
    text,
)

done = run_report(proj, log, "--json")
data = json.loads(done.stdout)
check(
    "report --json prints one object with a stable key order",
    list(data)
    == ["files", "malformed", "records", "registered", "rows", "weeks", "window"],
    done.stdout,
)
check(
    "report --json carries the same counts as the text report",
    (data["records"], data["malformed"], data["weeks"])
    == (13, 3, ["2026-W10", "2026-W11"])
    and data["rows"][1]
    == {
        "hook": "loop_guard",
        "decision": "block",
        "weeks": {"2026-W10": 1, "2026-W11": 2},
        "total": 3,
    },
    done.stdout,
)
check(
    "report --json maps a registration to the script its command names, not its name",
    {r["name"]: r["script"] for r in data["registered"]["no_records"]}
    == {
        "budget_guard_PreToolUse": "budget_guard",
        "readonly_guard_SubagentStart": "readonly_guard",
        "harness_doctor_SessionStart": "harness_doctor",
    }
    and "renamed_gate_Stop"
    not in [r["name"] for r in data["registered"]["gates_without_deny_or_block"]],
    done.stdout,
)

# The window needs a fixed clock, so it is driven through the function the CLI calls.
assert hasattr(harness_events, "report_data"), "report_data is the seam under test"
noon = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)  # a Thursday in 2026-W11
week = harness_events.report_data(log, proj / "harness.toml", weeks=1, now=noon)
check(
    "report --weeks 1 opens at Monday 00:00 UTC of the current ISO week",
    (week["records"], week["weeks"], week["window"])
    == (
        8,
        ["2026-W11"],
        {"weeks": 1, "since": "2026-03-09T00:00:00+00:00"},
    ),
    str(week),
)
check(
    "report --weeks keeps a record at the opening instant and drops one an hour before it",
    {(r["hook"], r["decision"]): r["total"] for r in week["rows"]}
    == {
        ("land", "landed"): 1,
        ("loop_guard", "block"): 2,
        ("model_router", "allow"): 1,
        ("model_router", "deny"): 1,
        ("queue_guard", "allow"): 1,
        ("turn_guard", "block"): 1,
        ("verify_commit_landed", "pass"): 1,
    },
    str(week["rows"]),
)
check(
    "report --weeks 2 reaches back to Monday of the previous ISO week",
    harness_events.report_data(log, proj / "harness.toml", weeks=2, now=noon)["records"]
    == 13,
)
check(
    "the text report says where a --weeks window opens",
    "decision log: --weeks 1, since 2026-03-09T00:00:00+00:00"
    in harness_events.render_report(week).splitlines(),
)
check(
    "report with no --weeks has no window",
    harness_events.report_data(log, proj / "harness.toml", now=noon)["window"]
    == {"weeks": None, "since": None},
)
check(
    "a window changes the registered lists: a gate whose only block is older is listed",
    [r["name"] for r in week["registered"]["gates_without_deny_or_block"]]
    == [
        "require_agent_model_PreToolUse",
        "verify_commit_landed_PostToolUse",
        "budget_guard_PreToolUse",
    ]
    and [r["name"] for r in week["registered"]["no_records"]]
    == [
        "require_agent_model_PreToolUse",
        "budget_guard_PreToolUse",
        "readonly_guard_SubagentStart",
        "harness_doctor_SessionStart",
    ],
    str(week["registered"]),
)
utc_now = datetime.now(timezone.utc)
clock_log = SCRATCH / "clock.jsonl"
write_lines(
    clock_log,
    [
        event_line(utc_now.isoformat(timespec="seconds"), "h", "allow"),
        event_line(
            (utc_now - timedelta(days=60)).isoformat(timespec="seconds"), "h", "allow"
        ),
    ],
)
done = run_report(proj, clock_log, "--weeks", "2", "--json")
check(
    "report --weeks on the command line uses the real clock",
    json.loads(done.stdout)["records"] == 1,
    done.stdout + done.stderr,
)
check(
    "report refuses --weeks 0",
    run_report(proj, clock_log, "--weeks", "0").returncode == 2,
)

edge_log = SCRATCH / "edge.jsonl"
write_lines(
    edge_log,
    [
        event_line("2025-12-31T12:00:00+00:00", "h", "allow"),
        event_line("2026-03-08T23:30:00-05:00", "h", "allow"),
        # No UTC offset: the week is unknowable, so it is malformed, not guessed.
        event_line("2026-03-09T12:00:00", "h", "allow"),
    ],
)
edge = json.loads(run_report(proj, edge_log, "--json").stdout)
check(
    "report buckets by UTC ISO week: the ISO year, and an offset time after the UTC week turns",
    edge["weeks"] == ["2026-W01", "2026-W11"] and edge["records"] == 2,
    str(edge),
)
check(
    "report counts a timestamp with no UTC offset as malformed",
    edge["malformed"] == 1,
    str(edge),
)

absent = run_report(proj, SCRATCH / "absent.jsonl", "--json")
check(
    "report on a log that does not exist counts zero records and exits 0",
    absent.returncode == 0
    and json.loads(absent.stdout)["records"] == 0
    and json.loads(absent.stdout)["files"] == [],
    absent.stdout + absent.stderr,
)

# An unreadable harness.toml still gives the counts.
for label, toml in (
    ("missing", None),
    ("a directory", "DIR"),
    ("not TOML", "[[hook\nname = "),
    ("a hook entry with no command", '[[hook]]\nname = "x"\nkind = "gate"\n'),
):
    bad = make_project("bad-" + label.replace(" ", "-"), toml)
    badlog = bad / "events.jsonl"
    write_lines(badlog, LIVE_LINES)
    done = run_report(bad, badlog)
    check(
        f"report with harness.toml {label} prints the counts and says the registered list is unavailable",
        done.returncode == 0
        and "records in window: 8; malformed lines: 2" in done.stdout.splitlines()
        and any(
            line.startswith("registered hooks: unavailable")
            for line in done.stdout.splitlines()
        )
        and "registered, no records in window:" not in done.stdout,
        done.stdout + done.stderr,
    )
    data = json.loads(run_report(bad, badlog, "--json").stdout)
    check(
        f"report --json with harness.toml {label} has registered.available false and no lists",
        data["registered"]["available"] is False
        and bool(data["registered"]["error"])
        and data["registered"]["no_records"] is None
        and data["registered"]["gates_without_deny_or_block"] is None
        and data["records"] == 8,
        str(data["registered"]),
    )

# Attempt a real writer rotation exactly between report file reads, without sleeps.
for has_sidecar in (True, False):
    racing = fresh(f"report-race-{has_sidecar}")
    racing_backup = racing.with_name(racing.name + ".1")
    sidecar = racing.with_name(racing.name + ".lock")
    write_lines(
        racing_backup, [event_line("2026-03-09T00:00:00+00:00", "backup", "allow")]
    )
    write_lines(racing, [event_line("2026-03-09T00:00:00+00:00", "live", "allow")])
    if has_sidecar:
        sidecar.touch()
    original_read = Path.read_bytes
    attempted, deferred = [], []

    def rotate_between_reads(path):
        data = original_read(path)
        if path == racing_backup and not attempted:
            attempted.append(True)
            if sidecar.exists():
                with sidecar.open("rb") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        deferred.append(True)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            if not deferred:
                assert harness_events.record("new", "allow")
        return data

    old_cap = harness_events.MAX_BYTES
    harness_events.MAX_BYTES = 1
    try:
        with patch.object(Path, "read_bytes", rotate_between_reads):
            snapshot = harness_events.report_data(racing, proj / "harness.toml")
        if deferred:
            assert harness_events.record("new", "allow")
    finally:
        harness_events.MAX_BYTES = old_cap
    hooks = {row["hook"] for row in snapshot["rows"]}
    check(
        f"report keeps the live generation when rotation crosses its reads (sidecar={has_sidecar})",
        attempted == [True] and hooks in ({"backup", "live"}, {"live", "new"}),
        str(snapshot),
    )
    check(
        f"the competing writer actually rotates (sidecar={has_sidecar})",
        [row["hook"] for row in rows(racing_backup) + rows(racing)] == ["live", "new"],
    )

# A log over the rotation cap is only read: rotating is record()'s job.
big = make_project("big", FIXTURE_TOML)
biglog = big / "events.jsonl"
write_lines(
    biglog, [event_line("2026-03-09T00:00:00+00:00", "h", "allow") + " " * 1000] * 5300
)
size = biglog.stat().st_size
done = run_report(big, biglog, "--json")
check(
    "report leaves a log over MAX_BYTES in place and creates no backup",
    size >= harness_events.MAX_BYTES
    and biglog.is_file()
    and biglog.stat().st_size == size
    and not biglog.with_name("events.jsonl.1").exists()
    and json.loads(done.stdout)["records"] == 5300,
    f"{size} {done.stderr}",
)

# Hooks import this module from a bare copy: no harness_config, paths or tomllib.
lone = SCRATCH / "lone"
lone.mkdir()
shutil.copy2(SCRIPT, lone)
probe = subprocess.run(
    [
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import harness_events; "
        "print(sorted({'harness_config', 'paths', 'tomllib'} & set(sys.modules)))",
        str(lone),
    ],
    capture_output=True,
    text=True,
)
check(
    "the module imports from a bare copy without harness_config, paths or tomllib",
    probe.returncode == 0 and probe.stdout.strip() == "[]",
    probe.stdout + probe.stderr,
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
