#!/usr/bin/env python3
"""Cap a session's tool calls and wall time (harness.toml [budget]).

    PreToolUse (every tool)   count the call. At 80% of max_tool_calls or max_minutes,
        add one warning as context; at 100% deny every further call with
        "budget exhausted: write status (done, left, blockers) and stop".

0 turns a limit off. The clock counts only active time: a gap over 30 minutes between tool
calls is not counted. At 100% a Write or Edit of a file matching [budget] status_glob still
passes, so the agent can leave its status. State:
<git common dir>/harness/loop/<session_id>[.<agent_id>].budget.json (scripts/guard_lib.py);
`scripts/loop_reset.py --budget` is the owner's way to start a fresh budget.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]).
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import adr  # noqa: E402
import guard_lib as g  # noqa: E402
import harness_config  # noqa: E402

WARN_AT = 0.8
IDLE_GAP = 30 * 60


def fresh():
    """Return an empty budget state."""
    return {"v": 1, "active": 0.0, "last": None, "calls": 0, "warned": False}


def status_write(payload, globs):
    """Return whether this call writes a file that matches [budget] status_glob."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    target = tool_input.get("file_path")
    if payload.get("tool_name") not in ("Edit", "Write") or not isinstance(target, str) or not target:
        return False
    cwd = g.cwd_of(payload)
    path = Path(os.path.realpath(Path(cwd) / target))
    top = g.git(cwd, "rev-parse", "--show-toplevel")
    try:
        rel = path.relative_to(Path(top).resolve()).as_posix() if top else path.as_posix().lstrip("/")
    except ValueError:
        rel = path.as_posix().lstrip("/")
    return any(adr.glob_match(pattern, rel) for pattern in globs)


def main():
    payload = g.read_payload()
    if not payload or payload.get("hook_event_name") != "PreToolUse":
        return 0
    path = g.state_file("loop", g.cwd_of(payload), g.session_key(payload) + ".budget")
    if path is None:
        return 0
    limits = harness_config.load(ROOT)["budget"]
    max_calls, max_minutes = limits["max_tool_calls"], limits["max_minutes"]
    state, notice = g.load_state(path, fresh)
    now = time.time()
    if state["last"] is not None and 0 < now - state["last"] <= IDLE_GAP:
        state["active"] += now - state["last"]
    state["last"] = now
    used = [(state["calls"], max_calls, "tool calls"), (state["active"] / 60, max_minutes, "minutes")]
    share = max((have / cap for have, cap, _ in used if cap > 0), default=0.0)
    detail = ", ".join(f"{have:.0f}/{cap} {unit}" for have, cap, unit in used if cap > 0)
    if share >= 1:
        g.save_state(path, state)
        if status_write(payload, limits["status_glob"]):
            return 0
        g.record("budget_guard", "deny", "exhausted", detail, payload)
        g.pre_deny(f"budget exhausted: write status (done, left, blockers) and stop. Used {detail} "
                   "(harness.toml [budget]). Only the owner extends it.")
        return 0
    state["calls"] += 1
    warn = share >= WARN_AT and not state["warned"]
    state["warned"] = state["warned"] or warn
    g.save_state(path, state)
    if warn:
        g.context("PreToolUse", f"budget_guard: {share:.0%} of this session's budget used ({detail}). "
                                "Wrap up: finish the task or write status (done, left, blockers).")
    elif notice:
        g.context("PreToolUse", "budget_guard: " + notice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
