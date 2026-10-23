"""model_router.py: an Agent call whose model does not match its type's tier is denied.

The expectations below are literal (CLAUDE.md Rule 11) and deliberately not
imported from the hook, so a table edited in the hook cannot agree with itself.
"""
import ast
import json
import tempfile
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / ".claude" / "hooks"
HOOK = HOOKS / "model_router.py"
# These tests run real hooks, which record decisions (scripts/harness_events.py).
# Point the record at a scratch file so fixtures never enter the real one.
os.environ["HARNESS_EVENTS_LOG"] = str(Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail and not cond else ""))


def run(subagent_type, model, raw=None, event="PreToolUse"):
    """Run the hook as Claude Code does; return (exit code, parsed stdout or None)."""
    tool_input = {"description": "x", "prompt": "y", "subagent_type": subagent_type}
    if model is not None:
        tool_input["model"] = model
    event = raw if raw is not None else json.dumps(
        {"hook_event_name": event, "tool_name": "Agent", "tool_input": tool_input})
    done = subprocess.run([sys.executable, str(HOOK)], input=event, capture_output=True, text=True)
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None)


def denial(subagent_type, model):
    code, out = run(subagent_type, model)
    assert code == 0
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None


MATCH = [
    ("Explore", "haiku"), ("dual-derive", "opus"), ("Explore", "Haiku"),
    ("python-general-engineer", "Sonnet"), ("Plan", "OPUS"),
    ("project-coordinator-engineer", "opus"), ("research-coordinator-engineer", "opus"),
    ("toolkit-governance-engineer", "opus"), ("pipeline-orchestrator-engineer", "opus"),
    ("python-general-engineer", "sonnet"), ("hook-development-engineer", "sonnet"),
    ("code-simplifier:code-simplifier", "sonnet"),
    ("reviewer-code", "opus"), ("Plan", "opus"),
    ("pr-review-toolkit:silent-failure-hunter", "opus"),
    ("code-modernization:security-auditor", "opus"),
    ("reviewer-system", "claude-opus-4-1"),
    ("live-proof-auditor", "opus"),
    ("Plan", "fable"), ("reviewer-code", "Fable"), ("dual-derive", "claude-fable-1"),
    ("live-proof-auditor", "fable"),
    ("project-coordinator-engineer", "sonnet"), ("toolkit-governance-engineer", "haiku"),
    ("testing-automation-engineer", "sonnet"), ("reviewer-system", "opus"),
]
for kind, model in MATCH:
    check(f"{kind} on {model} is allowed", denial(kind, model) is None)

MISMATCH = [
    ("Explore", "sonnet", "haiku"), ("dual-derive", "haiku", "opus"), ("dual-derive", "sonnet", "opus"),
    ("code-simplifier:code-simplifier", "opus", "sonnet"),
    ("pr-review-toolkit:comment-analyzer", "sonnet", "opus"),
    ("pr-review-toolkit:pr-test-analyzer", "sonnet", "opus"),
    ("pr-review-toolkit:silent-failure-hunter", "sonnet", "opus"),
    ("pr-review-toolkit:type-design-analyzer", "sonnet", "opus"),
    ("code-modernization:security-auditor", "sonnet", "opus"),
    ("reviewer-code", "haiku", "opus"), ("reviewer-system", "sonnet", "opus"),
    ("reviewer-perspectives", "sonnet", "opus"),
    ("Explore", "opus", "haiku"), ("Plan", "sonnet", "opus"),
    ("Explore", "Sonnet", "haiku"),
    ("python-general-engineer", "opus", "sonnet"), ("golang-general-engineer", "haiku", "sonnet"),
    ("pr-review-toolkit:code-simplifier", "opus", "sonnet"),
    ("reviewer-domain", "sonnet", "opus"), ("Plan", "haiku", "opus"),
    ("pr-review-toolkit:code-reviewer", "sonnet", "opus"),
    ("code-modernization:architecture-critic", "haiku", "opus"),
    ("live-proof-auditor", "sonnet", "opus"),
    ("Explore", "fable", "haiku"), ("python-general-engineer", "fable", "sonnet"),
    ("testing-automation-engineer", "opus", "sonnet"), ("testing-automation-engineer", "haiku", "sonnet"),
    ("reviewer-code", "sonnet", "opus"), ("reviewer-system", "haiku", "opus"),
    ("code-simplifier:code-simplifier", "fable", "sonnet"),
]
for kind, model, expected in MISMATCH:
    why = denial(kind, model)
    check(f"{kind} on {model} is denied naming {expected}",
          why is not None and f"belongs on {expected}" in why, why)

check("the deny verdict is exactly the documented hookSpecificOutput", run("Explore", "sonnet")[1] == {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Agent type `Explore` belongs on haiku, but model is `sonnet`. "
            "CLAUDE.md Rule 11: bounded implementation -> sonnet; mechanical discovery and "
            "counting -> haiku; review, planning and independent re-derivation -> opus."),
    }})
check("a PostToolUse event gets no verdict", run("Explore", "sonnet", event="PostToolUse") == (0, None))
check("inherit is left to require_agent_model", denial("Explore", "inherit") is None)
check("an unlisted type passes on any model", denial("general-purpose", "opus") is None)
check("fork passes whatever model it carries", denial("fork", "haiku") is None)
check("a missing model is left to require_agent_model", denial("Explore", None) is None)
check("non-Agent tools are ignored",
      run(None, None, json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))[1] is None)
check("a non-Agent tool carrying a mismatched subagent_type is ignored", run(None, None, json.dumps(
    {"tool_name": "Task", "tool_input": {"subagent_type": "Explore", "model": "opus"}}))[1] is None)
for bad in ["", "not json", "[]"]:
    check(f"malformed stdin {bad!r} exits 0 without a verdict", run(None, None, bad) == (0, None))

def inherit_literal(hook):
    """Return the INHERIT set literal assigned in a hook, parsed from source without importing it."""
    for node in ast.parse((HOOKS / hook).read_text()).body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["INHERIT"]:
            return ast.literal_eval(node.value)
    return None


EXPECTED_INHERIT = {"", "inherit", "inherit_parent", "none", "null"}
check("router INHERIT is the expected literal", inherit_literal("model_router.py") == EXPECTED_INHERIT)
check("require_agent_model INHERIT is the expected literal",
      inherit_literal("require_agent_model.py") == EXPECTED_INHERIT)
check("the two INHERIT sets are equal",
      inherit_literal("model_router.py") == inherit_literal("require_agent_model.py"))
check("model_router does not import require_agent_model",
      "require_agent_model" not in {n.module for n in ast.walk(ast.parse(HOOK.read_text()))
                                    if isinstance(n, ast.ImportFrom)})

def named_in(table, name):
    """Return whether a module-level set literal `table` in the router lists `name`, parsed without importing."""
    for node in ast.parse(HOOK.read_text()).body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [table]:
            return name in ast.literal_eval(node.value)
    return False


for table, name in [("SONNET", "python-general-engineer"), ("SONNET", "testing-automation-engineer"),
                    ("OPUS", "reviewer-code"), ("OPUS", "reviewer-system")]:
    check(f"{name} is named in the {table} table", named_in(table, name))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
