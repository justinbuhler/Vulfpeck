#!/usr/bin/env python3
"""Deny an Agent call whose `model` does not match the tier of its `subagent_type`.

CLAUDE.md Rule 11: bounded implementation goes to Sonnet, mechanical discovery and
complete counting to Haiku, review, planning and independent re-derivation to Opus.
`require_agent_model.py` already denies a missing or `inherit` model; this checks a
named one.

Unlisted subagent types pass (including the coordination roles below), and so does
`fork`, which ignores `model`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Literal copy of require_agent_model.INHERIT: importing it would let a failed import disable the hook.
# tests/test_model_router_hook.py asserts the two sets are equal.
INHERIT = {"", "inherit", "inherit_parent", "none", "null"}
HAIKU = {"Explore"}
SONNET = {"code-simplifier:code-simplifier", "pr-review-toolkit:code-simplifier"}
SONNET_SUFFIX = "-engineer"  # `*-engineer` types are bounded implementation ...
COORDINATORS = {  # ... except these coordination roles, which are left unlisted
    "project-coordinator-engineer",
    "research-coordinator-engineer",
    "toolkit-governance-engineer",
    "pipeline-orchestrator-engineer",
}
OPUS = {
    "Plan",
    "dual-derive",
    "live-proof-auditor",
    "pr-review-toolkit:code-reviewer",
    "pr-review-toolkit:comment-analyzer",
    "pr-review-toolkit:pr-test-analyzer",
    "pr-review-toolkit:silent-failure-hunter",
    "pr-review-toolkit:type-design-analyzer",
    "code-modernization:architecture-critic",
    "code-modernization:security-auditor",
}
OPUS_PREFIX = "reviewer-"


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def tier(subagent_type: str) -> str | None:
    if subagent_type in COORDINATORS:
        return None
    if subagent_type in HAIKU:
        return "haiku"
    if subagent_type in SONNET or subagent_type.endswith(SONNET_SUFFIX):
        return "sonnet"
    if subagent_type in OPUS or subagent_type.startswith(OPUS_PREFIX):
        return "opus"
    return None


def reason(payload: dict[str, Any]) -> str | None:
    if payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    subagent_type = str(tool_input.get("subagent_type") or "").strip()
    expected = tier(subagent_type)
    model = str(tool_input.get("model") or "").strip().lower()
    # Missing/inherit is require_agent_model.py's denial; full ids like claude-opus-4 contain the family.
    accepted = (expected, "fable") if expected == "opus" else (expected,)  # fable is accepted wherever Opus is
    if expected is None or model in INHERIT or any(family in model for family in accepted):
        return None
    return (
        f"Agent type `{subagent_type}` belongs on {expected}, but model is `{model}`. "
        "CLAUDE.md Rule 11: bounded implementation -> sonnet; mechanical discovery and "
        "counting -> haiku; review, planning and independent re-derivation -> opus."
    )


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
    denial = reason(payload)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    kind = str(tool_input.get("subagent_type") or "").strip()
    subject = f"{kind or '-'}/{tool_input.get('model') or '-'}"
    if denial:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": denial,
        }}))
        record("model_router", "deny", "tier-mismatch", subject, payload)
    else:
        record("model_router", "allow", "tier-match" if tier(kind) else "unlisted", subject, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
