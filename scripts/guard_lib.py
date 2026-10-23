"""What the loop, scope, budget and turn guards share (ADR 0017).

State is local and never tracked, under <git common dir>/harness/:
    loop/<session_id>.json     loop_guard and turn_guard counters
    loop/<session_id>.budget.json   budget_guard counters
    scope/<branch>.json        the scope lease (scripts/scope.py)
A missing or corrupt file is reset with a one-line notice, never a crash.

The rule texts below are quoted in block messages; tests/test_loop_guards.py
checks each one appears verbatim in AGENTS.md.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]

RULE_FINGERPRINT = (
    "A failure fingerprint (same failing check or error) seen 3 times, meaning the original failure plus two "
    "failed repairs: stop, write what was tried and why it failed, then change approach or escalate."
)
RULE_NO_PROGRESS = (
    "Two consecutive edit-and-run cycles with no new passing check: stop."
)
RULE_UNCHANGED = (
    "Never re-run an unchanged failing command expecting a different result."
)
RULE_TURN = (
    "Do not end a turn announcing work you have not done; do it or name the blocker."
)
ATTEMPTS_KEPT = 3
TRANSCRIPT_TAIL = 200_000


def way_out(session):
    """Return the recovery text a block message ends with."""
    return (
        "Way out: write what was tried and why it failed to the task's status, then change approach or escalate "
        "to the owner. Only the owner clears this trip: "
        f"`python3 scripts/loop_reset.py --session {session} --reason \"...\"`. Never reset it yourself to keep going."
    )

# The one list of commands that count as a test or lint run, besides harness.toml
# commands.lint and commands.test.
TEST_PATTERNS = (
    r"(?:^|[\s;&|])(?:\./)?scripts/(?:run|gate)\.sh\b",
    r"\bpython3?\s+(?:\S+/)?tests/",
    r"\buv\s+run\b.*\btests/",
    r"\bscripts/plan_tests\.py\b.*--run\b",
)
EXIT_KEYS = ("exit_code", "exitCode", "returncode", "exit_status")
EXIT_LINE = re.compile(
    r"^\s*(?:exit code|exit status|process exited with code)\s*[:=]?\s*(-?\d+)\s*$",
    re.I | re.M,
)
ERROR_LINE = re.compile(r"error|fail|traceback|exception|not found|no such", re.I)
FILTER_TAIL = re.compile(r"\s*\|\s*(?:tail|head)\b[^|]*$")

ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def record(*args, **kwargs):
    """Log one decision (scripts/harness_events.py); a failure here never changes it."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_events

        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def git(cwd, *args):
    """Run git in cwd with GIT_* scrubbed; return stdout stripped, or None on any failure."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, env=ENV
        )
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def state_file(kind, cwd, name):
    """Return <git common dir>/harness/<kind>/<name>.json for the repository at cwd, or None outside git."""
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    return Path(common) / "harness" / kind / (quote(name, safe="") + ".json")


def load_state(path, fresh):
    """Return (state, notice). A missing file is `fresh()`; a corrupt one is `fresh()` plus a notice."""
    if not path.is_file():
        return fresh(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == 1:
            return data, None
    except OSError, ValueError:
        pass
    return (
        fresh(),
        f"{path.name} was unreadable and was reset; this session's counters restart from zero.",
    )


def save_state(path, state):
    """Write state atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def loop_path(payload):
    """Return the loop state path for this payload's session, or None outside git."""
    return state_file("loop", cwd_of(payload), session_of(payload))


def fresh_loop():
    """Return the empty loop state: the only counters loop_guard and turn_guard keep."""
    return {"v": 1, "fp": {}, "failing": {}, "last_failed": None, "edited": False, "pending_edits": 0,
            "prev_len": None, "stalled": 0, "tripped": None, "turn_blocked": ""}


def read_payload():
    """Return the hook payload dict from stdin, or None when it is not a JSON object."""
    try:
        payload = json.load(sys.stdin)
    except ValueError, UnicodeDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def session_of(payload):
    """Return the payload's session id, or a fixed name when the runtime sent none."""
    return str(payload.get("session_id") or "nosession")


def cwd_of(payload):
    """Return the directory the event happened in."""
    return (
        payload.get("cwd")
        if isinstance(payload.get("cwd"), str) and payload["cwd"]
        else os.getcwd()
    )


def pre_deny(reason):
    """Print a PreToolUse deny for Claude's schema."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def context(event, text):
    """Print additionalContext for event."""
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        )
    )


def text_of(value):
    """Flatten a tool response of any shape into text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(text_of(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(text_of(v) for v in value)
    return ""


def normalise(command):
    """Return a command in fingerprint form: no rtk prefix, collapsed whitespace, no trailing tail/head filter."""
    command = re.sub(r"^\s*rtk\s+", "", command)
    command = " ".join(command.split())
    while FILTER_TAIL.search(command):
        command = FILTER_TAIL.sub("", command)
    return command


def strip_rtk(command):
    """Return command without the `rtk ` prefix a user-level hook adds after PreToolUse."""
    return re.sub(r"^\s*rtk\s+", "", command)


def transcript_exit_code(payload):
    """Return the exit code Codex logged for this tool call in its rollout, or None when it is not there (yet).

    Codex's PostToolUse `tool_response` is the output text alone; the rollout holds an
    `item_completed` event whose item id is the payload's `tool_use_id` and carries `exit_code`.
    """
    path, call = payload.get("transcript_path"), payload.get("tool_use_id")
    if not isinstance(path, str) or not isinstance(call, str) or not call:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(handle.tell() - TRANSCRIPT_TAIL, 0))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if call not in line:
            continue
        try:
            item = (json.loads(line).get("payload") or {}).get("item") or {}
        except ValueError:
            continue
        if item.get("id") == call and isinstance(item.get("exit_code"), int):
            return item["exit_code"]
    return None


def exit_status(payload):
    """Return a Bash result's exit status, or 0 when the response shows none.

    Claude Code sends a failed command as PostToolUseFailure with `error` "Exit code N\\n...";
    PostToolUse is only ever a success there. Codex and Gemini send PostToolUse for both, so a
    response that is not Claude's {stdout, stderr} shape is read for a numeric field, then (Codex
    sends the output text alone) the Codex rollout, then an "Exit Code: N" line.
    """
    if payload.get("hook_event_name") == "PostToolUseFailure":
        found = re.match(r"\s*Exit code (-?\d+)", str(payload.get("error") or ""))
        return int(found.group(1)) if found else 1
    response = payload.get("tool_response")
    if isinstance(response, dict):
        for holder in (
            response,
            response.get("metadata")
            if isinstance(response.get("metadata"), dict)
            else {},
        ):
            for key in EXIT_KEYS:
                if isinstance(holder.get(key), int) and not isinstance(
                    holder.get(key), bool
                ):
                    return holder[key]
        if "stdout" in response and "llmContent" not in response:
            return 0
    if isinstance(response, str):
        logged = transcript_exit_code(payload)
        if logged is not None:
            return logged
    found = EXIT_LINE.findall(text_of(response))
    return int(found[-1]) if found else 0


def first_error_line(payload):
    """Return the first line of a failed Bash result that reads as an error, else its first non-empty line."""
    if payload.get("hook_event_name") == "PostToolUseFailure":
        text = re.sub(r"^\s*Exit code -?\d+\s*", "", str(payload.get("error") or ""))
    else:
        response = payload.get("tool_response")
        stderr = response.get("stderr") if isinstance(response, dict) else None
        text = (
            stderr if isinstance(stderr, str) and stderr.strip() else text_of(response)
        )
    lines = [
        ln.strip() for ln in text.splitlines() if ln.strip() and not EXIT_LINE.match(ln)
    ]
    return (
        next((ln for ln in lines if ERROR_LINE.search(ln)), lines[0] if lines else "")
    )[:200]


def fingerprint(command, error_line):
    """Return the sha1 of the normalised command plus the first error line."""
    return hashlib.sha1(f"{normalise(command)}\0{error_line}".encode()).hexdigest()


def is_test_command(command, root=ROOT):
    """Return whether command is a test or lint run: a harness.toml command or one of TEST_PATTERNS."""
    command = normalise(command)
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_config

        commands = harness_config.load(root)["commands"]
        configured = [commands["lint"], commands["test"]]
    except Exception:
        configured = []
    return any(c and c in command for c in configured) or any(
        re.search(p, command) for p in TEST_PATTERNS
    )
