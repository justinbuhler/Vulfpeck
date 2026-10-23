#!/usr/bin/env python3
"""Stop a repair loop from running on (AGENTS.md "Stop and reassess").

    PostToolUse Bash, PostToolUseFailure Bash   count a failed command by fingerprint
        (sha1 of the normalised command plus the first error line). The 3rd sighting
        in a session trips the guard; so do two consecutive edit-and-run cycles with
        no progress (a passing run, or fewer failing checks than the cycle before).
    PostToolUse Edit|Write                      note that an edit happened.
    PreToolUse Bash                             deny a command byte-identical to the last
        failed one when nothing was edited since, and, once tripped, deny the failing
        command (fingerprint trip) or every test and lint command (no-progress trip).

Claude Code reports a non-zero Bash exit as PostToolUseFailure; Codex and Gemini send
PostToolUse with the exit status in the response (scripts/guard_lib.py reads both).
Only scripts/loop_reset.py clears a trip; the guard never resets itself. Jev's
reassess() recommendation is appended to the message as advice and decides nothing.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]). State: scripts/guard_lib.py.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402

STALLED_CYCLES = 2
SIGHTINGS = 3


def jev_advice(fp, attempts, errors):
    """Return one advisory line from jev.reassess(), or "" when Jev is unavailable."""
    try:
        import jev

        out = jev.reassess({"fingerprint": fp, "attempts": attempts, "last_errors": errors})
    except Exception:
        return ""
    if out.get("source") != "jev":
        return ""
    return f"Jev (advisory only; it cannot unblock) recommends {out['action']}: {out.get('reason') or 'no reason given'}."


def trip_fingerprint(state, fp, cmd, entry, session):
    """Set the fingerprint trip and return its message."""
    lines = "\n".join(f"  {i}. {a}" for i, a in enumerate(entry["attempts"], 1))
    advice = jev_advice(fp[:12], entry["n"], [a.split(" -> ", 1)[-1] for a in entry["attempts"]])
    message = "\n".join(filter(None, [
        f"loop_guard: blocked. AGENTS.md: {g.RULE_FINGERPRINT}",
        f"The {entry['n']} attempts:\n{lines}",
        advice,
        g.way_out(session),
    ]))
    state["tripped"] = {"kind": "fingerprint", "fp": fp, "cmds": [g.normalise(cmd)], "message": message}
    return message


def trip_no_progress(state, session):
    """Set the no-progress trip and return its message."""
    message = "\n".join([
        f"loop_guard: blocked. AGENTS.md: {g.RULE_NO_PROGRESS} Test and lint commands are denied until it is cleared.",
        g.way_out(session),
    ])
    state["tripped"] = {"kind": "no_progress", "message": message}
    return message


def after_command(state, payload, cmd):
    """Fold one finished Bash command into the state; return a block message or None."""
    norm = g.normalise(cmd)
    status = g.exit_status(payload)
    test = g.is_test_command(cmd)
    message = None
    if status != 0:
        error = g.first_error_line(payload)
        fp = g.fingerprint(cmd, error)
        entry = state["fp"].setdefault(fp, {"n": 0, "cmd": norm, "attempts": []})
        entry["n"] += 1
        entry["attempts"] = (entry["attempts"] + [f"{cmd[:120]} -> {error[:100]}"])[-g.ATTEMPTS_KEPT:]
        state["last_failed"], state["edited"] = cmd, False
        if test:
            state["failing"][norm] = fp
        if entry["n"] >= SIGHTINGS and not state["tripped"]:
            message = trip_fingerprint(state, fp, cmd, entry, g.session_of(payload))
            g.record("loop_guard", "block", "fingerprint-x3", fp[:12], payload)
    else:
        state["fp"] = {k: v for k, v in state["fp"].items() if v["cmd"] != norm}
        state["failing"].pop(norm, None)
        if state["last_failed"] is not None and g.normalise(state["last_failed"]) == norm:
            state["last_failed"] = None
    if test:
        size = len(state["failing"])
        if state["pending_edits"] > 0:
            if state["prev_len"] is not None:
                progress = status == 0 or size < state["prev_len"]
                state["stalled"] = 0 if progress else state["stalled"] + 1
            state["pending_edits"] = 0
            if state["stalled"] >= STALLED_CYCLES and not state["tripped"]:
                message = trip_no_progress(state, g.session_of(payload))
                g.record("loop_guard", "block", "no-progress-x2", norm[:80], payload)
        state["prev_len"] = size
    return message


def before_command(state, cmd):
    """Return (reason code, message) to deny this Bash command, or None to allow it."""
    tripped = state["tripped"]
    if tripped:
        if tripped["kind"] == "fingerprint" and g.normalise(cmd) in tripped["cmds"]:
            return "tripped-fingerprint", tripped["message"]
        if tripped["kind"] == "no_progress" and g.is_test_command(cmd):
            return "tripped-no-progress", tripped["message"]
    if state["last_failed"] is not None and not state["edited"] and g.strip_rtk(cmd) == state["last_failed"]:
        return "unchanged-rerun", (
            f"loop_guard: denied. AGENTS.md: {g.RULE_UNCHANGED} "
            "This exact command just failed and nothing was edited since: read the error, change something, then run it.")
    return None


def main():
    payload = g.read_payload()
    path = g.loop_path(payload) if payload else None
    if path is None:
        return 0
    event, tool = payload.get("hook_event_name"), payload.get("tool_name")
    state, notice = g.load_state(path, g.fresh_loop)
    command = (payload.get("tool_input") or {}).get("command")
    if tool in ("Edit", "Write") and event == "PostToolUse":
        state["edited"], state["pending_edits"] = True, state["pending_edits"] + 1
        g.save_state(path, state)
        return 0
    if tool != "Bash" or not isinstance(command, str):
        return 0
    if event == "PreToolUse":
        denial = before_command(state, command)
        if notice:
            g.save_state(path, state)
        if denial:
            g.record("loop_guard", "deny", denial[0], g.normalise(command)[:80], payload)
            g.pre_deny(denial[1])
        elif notice:
            g.context(event, "loop_guard: " + notice)
        return 0
    if event in ("PostToolUse", "PostToolUseFailure"):
        message = after_command(state, payload, g.strip_rtk(command))
        g.save_state(path, state)
        if message:
            print(json.dumps({"decision": "block", "reason": message,
                              "hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}))
        elif notice:
            g.context(event, "loop_guard: " + notice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
