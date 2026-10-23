#!/usr/bin/env python3
"""Stop a repair loop from running on (AGENTS.md "Stop and reassess").

    PostToolUse Bash, PostToolUseFailure Bash   count a failed command by fingerprint
        (sha1 of the normalised command plus the first error line). The 3rd sighting
        in a session trips the guard. A test or lint run that fails with no more
        progress than the previous failing run of the same command (a passing run, or
        strictly fewer failing tests) counts as a stalled cycle; two stalled cycles
        block once with the reassess message and reset the count, and a second such
        block denies every test and lint command until the owner resets it.
    PostToolUse Edit|Write                      note that an edit happened.
    PreToolUse Bash                             deny a command byte-identical to the last
        failed one while no edit and no other Bash command has run since; once tripped,
        deny the failing command (fingerprint trip) or every test and lint command
        (second no-progress trip). Deny a command that runs scripts/loop_reset.py or
        touches the loop state.
    PreToolUse Edit|Write                       deny a write into the loop state.

Claude Code reports a non-zero Bash exit as PostToolUseFailure; Codex and Gemini send
PostToolUse with the exit status in the response, or in Codex's rollout
(scripts/guard_lib.py). An exit that stays unknown changes no counter. The guard never
resets itself, and the agent cannot: the owner or the task runner calls
scripts/loop_reset.py. Jev's reassess() recommendation is appended to a block as advice.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]). State: scripts/guard_lib.py.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402

STALLED_CYCLES = 2
SIGHTINGS = 3
NOT_A_WRITE = re.compile(r"\d*>>?\s*(?:&\d|/dev/null)")
STATE_TOUCH = re.compile(r"harness/loop\b")
STATE_MUTATION = re.compile(r"\b(?:rm|mv|truncate|unlink|tee|sed|dd|cp)\b|>")
TRIPPED_TEXT = re.compile(r'"tripped":\s*\{')


def runs_reset(cmd):
    """Return whether cmd executes scripts/loop_reset.py (not --help, and not a mere mention of it).

    The script is executed when it is a segment's command word, or the script argument of
    python, python3 or `uv run`; `cd x &&` and other segments are split on first.
    """
    for segment in re.split(r"&&|\|\||[;|\n]", cmd):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        while words and re.match(r"\w+=", words[0]):
            words.pop(0)
        if words[:2] == ["uv", "run"]:
            words = words[2:]
            while words and words[0].startswith("-"):
                words.pop(0)
        if words and re.fullmatch(r"python[\d.]*", os.path.basename(words[0])):
            words.pop(0)
            while words and words[0].startswith("-"):
                if words[0] in ("-m", "-c"):
                    words = []
                    break
                words.pop(0)
        if words and os.path.basename(words[0]) == "loop_reset.py" and not {"-h", "--help"} & set(words):
            return True
    return False


def jev_advice(fp, attempts, errors):
    """Return one advisory line from jev.reassess(), or "" when Jev is unavailable."""
    try:
        import jev

        out = jev.reassess(
            {"fingerprint": fp, "attempts": attempts, "last_errors": errors}
        )
    except Exception:
        return ""
    if out.get("source") != "jev":
        return ""
    return f"Jev (advisory only; it cannot unblock) recommends {out['action']}: {out.get('reason') or 'no reason given'}."


def trip_fingerprint(state, fp, cmd, entry, session):
    """Set the fingerprint trip and return its message."""
    lines = "\n".join(f"  {i}. {a}" for i, a in enumerate(entry["attempts"], 1))
    advice = jev_advice(
        fp[:12], entry["n"], [a.split(" -> ", 1)[-1] for a in entry["attempts"]]
    )
    message = "\n".join(
        filter(
            None,
            [
                f"loop_guard: blocked. AGENTS.md: {g.RULE_FINGERPRINT}",
                f"The {entry['n']} attempts:\n{lines}",
                advice,
                g.way_out(session),
            ],
        )
    )
    state["tripped"] = {
        "kind": "fingerprint",
        "fp": fp,
        "cmds": [g.normalise(cmd)],
        "message": message,
    }
    return message


def trip_no_progress(state, fp, session):
    """Count a no-progress trip; return its message. The second trip denies test and lint commands."""
    state["strikes"] += 1
    state["stalled"] = 0
    entry = state["fp"].get(fp) or {"n": STALLED_CYCLES + 1, "attempts": []}
    advice = jev_advice(
        fp[:12],
        max(entry["n"], SIGHTINGS),
        [a.split(" -> ", 1)[-1] for a in entry["attempts"]],
    )
    if state["strikes"] == 1:
        head = (
            f"loop_guard: blocked once. AGENTS.md: {g.RULE_NO_PROGRESS} Change approach before "
            "the next test run; a second trip denies every test and lint command."
        )
    else:
        head = (
            f"loop_guard: blocked. AGENTS.md: {g.RULE_NO_PROGRESS} This is the second trip: "
            "test and lint commands are denied until the loop state is reset."
        )
    message = "\n".join(filter(None, [head, advice, g.way_out(session)]))
    if state["strikes"] >= 2:
        state["tripped"] = {"kind": "no_progress", "message": message}
    return message


def stalled_cycle(state, norm, status, items, fp):
    """Fold one test run into the cycle count; return True when the stalled count reached its limit.

    Progress is a passing run, or strictly fewer failing tests than the previous failing run of
    the same command; with no parseable failure list, a different error counts as progress. A run
    after a pass (or the first run) starts a new count and is not a stall.
    """
    before = state["runs"].get(norm)
    if status == 0:
        state["runs"].pop(norm, None)
        state["stalled"] = 0
        state["strikes"] = 0
        return False
    state["runs"][norm] = {"items": items, "fp": fp}
    if before is None or state["pending_edits"] == 0:
        return False
    if items is not None and before["items"] is not None:
        progress = len(items) < len(before["items"])
    else:
        progress = before["fp"] != fp
    state["stalled"] = 0 if progress else state["stalled"] + 1
    return state["stalled"] >= STALLED_CYCLES


def after_command(state, payload, cmd):
    """Fold one finished Bash command into the state; return (block message, notice)."""
    norm = g.normalise(cmd)
    status = g.exit_status(payload)
    if status is None:
        g.record("loop_guard", "pass", "exit-unknown", norm[:80], payload)
        return None, (
            "loop_guard: this command's exit status is unknown (Codex rollout not written yet), "
            "so it was not counted."
        )
    test = g.is_test_command(cmd)
    message = None
    session = g.session_key(payload)
    fp = ""
    if status != 0:
        error = g.first_error_line(payload)
        fp = g.fingerprint(cmd, error)
        entry = state["fp"].setdefault(fp, {"n": 0, "cmd": norm, "attempts": []})
        entry["n"] += 1
        entry["attempts"] = (entry["attempts"] + [f"{cmd[:120]} -> {error[:100]}"])[
            -g.ATTEMPTS_KEPT :
        ]
        state["last_failed"] = cmd
        if entry["n"] >= SIGHTINGS and not state["tripped"]:
            message = trip_fingerprint(state, fp, cmd, entry, session)
            g.record("loop_guard", "block", "fingerprint-x3", fp[:12], payload)
    else:
        state["last_failed"] = None
        state["fp"] = {k: v for k, v in state["fp"].items() if v["cmd"] != norm}
    if test:
        if (
            stalled_cycle(state, norm, status, g.failure_items(payload), fp)
            and not state["tripped"]
        ):
            message = trip_no_progress(state, fp, session)
            g.record(
                "loop_guard",
                "block",
                f"no-progress-strike-{state['strikes']}",
                norm[:80],
                payload,
            )
        state["pending_edits"] = 0
    return message, None


def before_command(state, cmd):
    """Return (reason code, message) to deny this Bash command, or None to allow it."""
    if runs_reset(cmd) or (STATE_TOUCH.search(cmd) and STATE_MUTATION.search(NOT_A_WRITE.sub("", cmd))):
        return "loop-state-touch", (
            "loop_guard: denied. The loop state is reset by the owner or the task runner, never by the "
            "agent: do not run scripts/loop_reset.py or edit, move or remove the loop state."
        )
    tripped = state["tripped"]
    if tripped:
        if tripped["kind"] == "fingerprint" and g.normalise(cmd) in tripped["cmds"]:
            return "tripped-fingerprint", tripped["message"]
        if tripped["kind"] in ("no_progress", "corrupt") and g.is_test_command(cmd):
            return "tripped-" + tripped["kind"], tripped["message"]
    if state["last_failed"] is not None and g.strip_rtk(cmd) == state["last_failed"]:
        return "unchanged-rerun", (
            f"loop_guard: denied. AGENTS.md: {g.RULE_UNCHANGED} This exact command just failed and "
            "nothing was edited and no other command ran since: read the error, change something, "
            "then run it."
        )
    return None


def main():
    payload = g.read_payload()
    path = g.loop_path(payload) if payload else None
    if path is None:
        return 0
    event, tool = payload.get("hook_event_name"), payload.get("tool_name")
    tool_input = (
        payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    )
    raw = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    state, notice = g.load_state(path, g.fresh_loop)
    if notice and TRIPPED_TEXT.search(raw):
        state["tripped"] = {
            "kind": "corrupt",
            "message": "loop_guard: blocked. The loop state was corrupt while a trip was set, so it is "
            "still treated as tripped: test and lint commands are denied. "
            + g.way_out(g.session_key(payload)),
        }
        g.save_state(path, state)
    if tool in ("Edit", "Write"):
        if event == "PreToolUse":
            target = str(tool_input.get("file_path") or "").replace("\\", "/")
            if "/harness/loop/" in target:
                g.record(
                    "loop_guard", "deny", "loop-state-touch", target[-80:], payload
                )
                g.pre_deny(
                    "loop_guard: denied. The loop state is reset by the owner or the task runner, never by the agent."
                )
        elif event == "PostToolUse":
            state["last_failed"], state["pending_edits"] = (
                None,
                state["pending_edits"] + 1,
            )
            g.save_state(path, state)
        return 0
    command = tool_input.get("command")
    if tool != "Bash" or not isinstance(command, str):
        return 0
    if event == "PreToolUse":
        denial = before_command(state, command)
        if notice:
            g.save_state(path, state)
        if denial:
            g.record(
                "loop_guard", "deny", denial[0], g.normalise(command)[:80], payload
            )
            g.pre_deny(denial[1])
        elif notice:
            g.context(event, "loop_guard: " + notice)
        return 0
    if event in ("PostToolUse", "PostToolUseFailure"):
        message, unknown = after_command(state, payload, g.strip_rtk(command))
        g.save_state(path, state)
        if message:
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": message,
                        "hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": message,
                        },
                    }
                )
            )
        elif unknown or notice:
            g.context(event, unknown or "loop_guard: " + notice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
