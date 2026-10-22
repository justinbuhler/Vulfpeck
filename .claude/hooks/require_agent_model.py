#!/usr/bin/env python3
"""Deny an Agent call that omits `model`, instead of letting it inherit silently.

The failure this prevents: an implementation agent was
spawned with no `model` argument, inherited the LEAD's Opus, and was caught only
because a human read the spawn hook line "general-purpose (inherit)". Bounded
implementation belongs to Sonnet; the Opus session is LEAD.

Only tracked `.claude/workflows/*.js` `agent()` calls are covered by
tests/test_workflow_routing.py MODEL-01..03B, and a test cannot observe a runtime
tool call. This closes the rest: the failure mode is silence, so the fix is to
refuse to be silent.

Deliberately narrow. It checks presence, never which model is correct - that
judgment stays with the caller and with the consequence table in AGENTS.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# `inherit` is the harness's own word for "use the parent's model". Accepting it
# would reintroduce the exact defect, so it is refused as if absent.
INHERIT = {"", "inherit", "inherit_parent", "none", "null"}

REASON = (
    "Agent call omits `model`, so it would inherit this session's model silently.\n"
    "An implementation agent that inherits the LEAD's model is caught only if a human\n"
    "happens to read the spawn line.\n"
    "Pass `model` explicitly. Route by consequence (AGENTS.md): bounded implementation\n"
    "-> sonnet; mechanical discovery, counting and deterministic comparison -> haiku;\n"
    "review, planning and independent re-derivation -> opus; architecture, arbitration\n"
    "and adversarial gating stay with the LEAD.\n"
    "A fork inherits the parent by design - `subagent_type: \"fork\"` is exempt."
)


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def verdict(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def offending(payload: dict[str, Any]) -> bool:
    """Return True when this Agent call would inherit its model silently."""
    if payload.get("tool_name") != "Agent":
        return False
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return False
    # A fork is defined as inheriting the parent's model; `model` is ignored there.
    if str(tool_input.get("subagent_type") or "").strip().lower() == "fork":
        return False
    model = tool_input.get("model")
    return str(model or "").strip().lower() in INHERIT


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") not in (None, "PreToolUse"):
        return 0
    if payload.get("tool_name") != "Agent":
        return 0
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    subject = f"{tool_input.get('subagent_type') or '-'}/{tool_input.get('model') or '-'}"
    if offending(payload):
        print(json.dumps(verdict(REASON)))
        record("require_agent_model", "deny", "model-missing", subject, payload)
    else:
        record("require_agent_model", "allow", "model-stated", subject, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
