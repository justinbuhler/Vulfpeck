"""Verify native Codex profiles and real spawn-policy hook decisions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude/hooks/model_router.py"
PASS, FAIL = [], []
# Independent of both profile files and the hook's runtime table.
EXPECTED = {
    "explorer": ("gpt-6-luna", "high"),
    "executor": ("gpt-6.1-sol", "medium"),
    "reviewer": ("gpt-6-astra", "high"),
    "planner": ("gpt-6-astra", "high"),
}


def check(name, cond, detail=""):
    """Report whether the named routing property holds."""
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


def invoke(arguments, env, command=None, cwd=ROOT, raw=None):
    """Run the router with native spawn arguments and return its denial reason."""
    event = {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent", "cwd": str(ROOT),
             "tool_input": arguments}
    args = ["bash", "-c", command] if command else [sys.executable, str(HOOK), "--runtime", "codex"]
    done = subprocess.run(args, input=json.dumps(event) if raw is None else raw,
                          cwd=cwd, env=env, text=True, capture_output=True)
    output = json.loads(done.stdout) if done.stdout.strip() else {}
    decision = output.get("hookSpecificOutput", {})
    assert done.returncode == 0, done.stderr
    return decision.get("permissionDecisionReason") if decision.get("permissionDecision") == "deny" else None


profiles = ROOT / ".codex/agents"
check("the native profile inventory contains exactly the approved roles",
      {p.stem for p in profiles.glob("*.toml")} == set(EXPECTED))
for role, (model, effort) in EXPECTED.items():
    path = profiles / f"{role}.toml"
    profile = tomllib.loads(path.read_text()) if path.is_file() else {}
    check(f"{role} declares required native metadata and only routing settings",
          set(profile) == {"name", "description", "developer_instructions", "model", "model_reasoning_effort"}
          and profile.get("name") == role and bool(profile.get("description"))
          and bool(profile.get("developer_instructions")))
    check(f"{role} pins the approved GPT model and effort",
          (profile.get("model"), profile.get("model_reasoning_effort")) == (model, effort))

registry = ROOT / ".codex/hooks.json"
hooks = json.loads(registry.read_text()).get("hooks", {}) if registry.is_file() else {}
groups = hooks.get("PreToolUse", [])
entries = [hook for group in groups for hook in group.get("hooks", [])]
check("Codex registers only the native spawn routing hook",
      set(hooks) == {"PreToolUse"} and len(groups) == 1
      and groups[0].get("matcher") == "^spawn_agent$" and len(entries) == 1)
command = entries[0].get("command", "") if entries else ""
check("the registration resolves the Git root and selects Codex mode",
      "git rev-parse --show-toplevel" in command
      and '/.claude/hooks/model_router.py" --runtime codex' in command)
check("the registration blocks script crashes before its native timeout",
      bool(entries) and entries[0].get("timeout") == 5 and "exit 2" in command)

with tempfile.TemporaryDirectory(prefix="codex-routing-") as tmp:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HARNESS_EVENTS_LOG"] = str(Path(tmp) / "events.jsonl")
    for role, (model, effort) in EXPECTED.items():
        check(f"native {role} uses its pinned profile when overrides are omitted",
              invoke({"agent_type": role}, env) is None)
        check(f"native {role} accepts its exact model and effort",
              invoke({"agent_type": role, "model": model, "reasoning_effort": effort}, env) is None)
        for key, value in (("model", "gpt-unapproved"), ("reasoning_effort", "low")):
            check(f"native {role} rejects a mismatched {key}",
                  invoke({"agent_type": role, key: value}, env) is not None)
    for name, arguments in (
        ("a missing role", {}),
        ("an unknown role", {"agent_type": "default"}),
        ("a task name masquerading as a role", {"task_name": "reviewer"}),
        ("Claude's role argument", {"subagent_type": "reviewer"}),
        ("an explicit inherit model", {"agent_type": "executor", "model": "inherit"}),
        ("an empty effort override", {"agent_type": "executor", "reasoning_effort": ""}),
        ("non-object arguments", []),
    ):
        check(f"native routing denies {name}", invoke(arguments, env) is not None)
    for raw in ("not json", "[]", "{}"):
        check(f"native routing reports unavailable input {raw!r}",
              invoke({}, env, raw=raw) is not None)
    if command:
        check("the registered command works from a repository subdirectory",
              invoke({"agent_type": "executor"}, env, command=command, cwd=ROOT / "tests") is None)
        check("the registered command denies an unapproved native spawn",
              invoke({"task_name": "executor"}, env, command=command, cwd=ROOT / "tests") is not None)
        project = Path(tmp) / "project"
        project.mkdir()
        created = subprocess.run(["git", "init", "-q", str(project)], env=env,
                                 capture_output=True, text=True)
        assert created.returncode == 0, created.stderr
        for name, content in (("missing script", None), ("crashing script", "raise SystemExit(1)\n")):
            script = project / ".claude/hooks/model_router.py"
            if content is not None:
                script.parent.mkdir(parents=True)
                script.write_text(content)
            result = subprocess.run(["bash", "-c", command], input="{}", cwd=project,
                                    env=env, text=True, capture_output=True)
            check(f"the registered command fails closed on a {name}",
                  result.returncode == 2 and "failing closed" in result.stderr, result.stderr)
    else:
        check("the registered command can be exercised", False)

    for name, ignored in ((".codex/hooks.json", False), (".codex/auth.json", True),
                          (".codex/agents/private.toml", True),
                          (".codex/agents/nested/auth.json", True),
                          *((f".codex/agents/{role}.toml", False) for role in EXPECTED)):
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", name],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        check(f"Git {'ignores' if ignored else 'can track'} {name}",
              result.returncode == (0 if ignored else 1), result.stderr)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
