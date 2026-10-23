#!/usr/bin/env python3
"""Cap a session's tool calls and wall time (harness.toml [budget]).

    PreToolUse (every tool)   count the call. At 80% of max_tool_calls or max_minutes,
        add one warning as context; at 100% deny every further call with
        "budget exhausted: write status (done, left, blockers) and stop".

0 turns a limit off. The clock starts at the session's first tool call. State:
<git common dir>/harness/loop/<session_id>.budget.json (scripts/guard_lib.py);
`scripts/loop_reset.py --budget` is the owner's way to start a fresh budget.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]).
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402
import harness_config  # noqa: E402

WARN_AT = 0.8


def fresh():
    """Return an empty budget state."""
    return {"v": 1, "start": None, "calls": 0, "warned": False}


def main():
    payload = g.read_payload()
    if not payload or payload.get("hook_event_name") != "PreToolUse":
        return 0
    path = g.state_file("loop", g.cwd_of(payload), g.session_of(payload) + ".budget")
    if path is None:
        return 0
    limits = harness_config.load(ROOT)["budget"]
    max_calls, max_minutes = limits["max_tool_calls"], limits["max_minutes"]
    state, notice = g.load_state(path, fresh)
    now = time.time()
    state["start"] = state["start"] if state["start"] is not None else now
    used = [(state["calls"], max_calls, "tool calls"), ((now - state["start"]) / 60, max_minutes, "minutes")]
    share = max((have / cap for have, cap, _ in used if cap > 0), default=0.0)
    detail = ", ".join(f"{have:.0f}/{cap} {unit}" for have, cap, unit in used if cap > 0)
    if share >= 1:
        g.save_state(path, state)
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
