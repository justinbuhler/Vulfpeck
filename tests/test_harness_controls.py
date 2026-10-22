"""The harness's own hooks are registered, run as registered, and fail as declared.

A hook can be broken three quiet ways: unregistered (deleted from settings.json),
mis-registered (a path or quoting error in the command), or crashing (an
exception exits 1, which Claude Code treats as non-blocking, so the tool call
goes ahead). Each looks exactly like a gate with nothing to deny.

So every check here runs the command string from .claude/settings.json through
a shell, the way Claude Code does, with CLAUDE_PROJECT_DIR set:
  - the inventory below is a literal (CLAUDE.md Rule 11) and must match
    settings.json exactly; adding a hook means adding it here, by hand;
  - every gate denies a known-bad input (its canary) and allows a known-good one;
  - every gate fails CLOSED when its script crashes (exit 2, which blocks);
  - every advisory hook fails OPEN (exit 0) when its script crashes or is missing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / ".claude" / "settings.json"

# These tests run real hooks, which record decisions (scripts/harness_events.py).
# Point the record at a scratch file so fixtures never enter the real one.
os.environ["HARNESS_EVENTS_LOG"] = str(Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


# (event, matcher, script, kind). Hand-written; never read from settings.json.
EXPECTED = {
    ("PreToolUse", "Agent", "require_agent_model.py", "gate"),
    ("PreToolUse", "Agent", "model_router.py", "gate"),
    ("PreToolUse", "Edit|Write", "blast_radius_advisory.py", "advisory"),
    ("PostToolUse", "Bash", "verify_commit_landed.py", "gate"),
    ("PostToolUse", "Edit|Write", "test_for_edited_module.py", "gate"),
    ("UserPromptSubmit", "", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "harness_doctor.py", "advisory"),
    ("PreToolUse", "Bash", "queue_guard.py", "advisory"),
    ("PreToolUse", "Bash", "verify_commit_landed.py", "gate"),
    ("SubagentStart", "", "readonly_guard.py", "advisory"),
    ("SubagentStop", "", "readonly_guard.py", "advisory"),
    ("Stop", "", "session_guard.py", "advisory"),
    ("PreToolUse", "Edit|Write", "adr_gate.py", "gate"),
    ("PostToolUse", "Edit|Write", "adr_gate.py", "gate"),
    ("SessionEnd", "", "session_guard.py", "advisory"),
}
GATES = {script for _, _, script, kind in EXPECTED if kind == "gate"}

GIT_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX",
           "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
           "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def script_of(command):
    found = re.findall(r"[\w.-]+\.py\b", command)
    return found[0] if found else ""


def registered():
    """Return {(event, matcher, script): (command, timeout)} from settings.json."""
    out = {}
    for event, groups in json.loads(SETTINGS.read_text())["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                key = (event, group.get("matcher", ""), script_of(hook["command"]))
                out[key] = (hook["command"], hook.get("timeout"))
    return out


def run(command, payload, project=ROOT, cwd=None):
    """Run one registered command as Claude Code would; return (rc, stdout, stderr, secs)."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    began = time.monotonic()
    done = subprocess.run(["bash", "-c", command], input=payload, capture_output=True,
                          text=True, env=env, cwd=str(cwd or project), timeout=180)
    return done.returncode, done.stdout, done.stderr, time.monotonic() - began


def denied(stdout):
    try:
        out = json.loads(stdout)
    except ValueError:
        return False
    return (out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            or out.get("decision") == "block")


REG = registered()
for event, matcher in (("UserPromptSubmit", ""), ("SessionStart", "startup")):
    command_text = REG.get((event, matcher, "codegraph_freshness.py"), ("", None))[0]
    check(f"{event} CodeGraph registration resolves the event checkout", "--hook" in command_text)
check("settings.json registers exactly the expected hooks",
      set(REG) == {(e, m, s) for e, m, s, _ in EXPECTED},
      f"unexpected: {sorted(set(REG) - {(e, m, s) for e, m, s, _ in EXPECTED})}; "
      f"missing: {sorted({(e, m, s) for e, m, s, _ in EXPECTED} - set(REG))}")
for event, matcher, script, kind in sorted(EXPECTED):
    path = (ROOT / ".claude" / "hooks" / script) if (ROOT / ".claude" / "hooks" / script).is_file() \
        else ROOT / "scripts" / script
    check(f"{script} exists", path.is_file(), str(path))
    _, timeout = REG.get((event, matcher, script), ("", None))
    check(f"{event} {script} declares a timeout", isinstance(timeout, int) and timeout > 0)
    definition = next(h for g in json.loads(SETTINGS.read_text())["hooks"][event]
                      if g.get("matcher", "") == matcher for h in g["hooks"]
                      if script_of(h["command"]) == script)
    check(f"{event} {script} declares runtime failure direction",
          definition.get("onFailure") == "block" if kind == "gate" else "onFailure" not in definition)


def command(event, matcher, script):
    return REG[(event, matcher, script)]


# --- canaries: each gate denies a bad input and allows a good one -------------
def agent_call(subagent_type, model):
    tool_input = {"description": "x", "prompt": "y", "subagent_type": subagent_type}
    if model is not None:
        tool_input["model"] = model
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input})


for script, bad, good in (
        ("require_agent_model.py", agent_call("python-general-engineer", None),
         agent_call("python-general-engineer", "sonnet")),
        ("model_router.py", agent_call("Explore", "opus"), agent_call("Explore", "haiku"))):
    cmd, timeout = command("PreToolUse", "Agent", script)
    rc, out, err, secs = run(cmd, bad)
    check(f"canary: registered {script} denies a bad Agent call", rc == 0 and denied(out), out + err)
    check(f"canary: {script} answers well inside its timeout", secs < timeout / 2, f"{secs:.1f}s")
    rc, out, err, _ = run(cmd, good)
    check(f"canary: registered {script} allows a good Agent call", rc == 0 and not denied(out), out + err)

with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    for args in (["init", "-q", "-b", "work"], ["-c", "user.name=t", "-c", "user.email=t@t",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, env=env,
                          capture_output=True, text=True).stdout.strip()
    cmd, timeout = command("PostToolUse", "Bash", "verify_commit_landed.py")

    def bash_event(response):
        return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "git commit -m x"},
                           "tool_response": {"stdout": response}})

    rc, out, err, secs = run(cmd, bash_event("nothing to commit"), cwd=repo)
    check("canary: registered verify_commit_landed.py blocks a commit that did not land",
          rc == 0 and denied(out), out + err)
    rc, out, err, _ = run(cmd, bash_event(f"[work {head}] base"), cwd=repo)
    check("canary: registered verify_commit_landed.py passes a commit at HEAD",
          rc == 0 and not denied(out), out + err)

with tempfile.TemporaryDirectory() as raw:
    project = Path(raw)
    (project / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "harness_config.py", project / "scripts")
    (project / "harness.toml").write_text(
        '[[mapped_tests]]\nmatch = "*.txt"\ntests = ["{dir}/check_{stem}.sh"]\ncommand = "bash {test}"\n')
    (project / "a.txt").write_text("x\n")
    (project / "check_a.sh").write_text("echo canary-failed; exit 1\n")
    (project / "b.txt").write_text("x\n")
    (project / "check_b.sh").write_text("exit 0\n")
    (project / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy2(ROOT / ".claude" / "hooks" / "test_for_edited_module.py", project / ".claude" / "hooks")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True,
                   env={k: v for k, v in os.environ.items() if k not in GIT_ENV})
    cmd, timeout = command("PostToolUse", "Edit|Write", "test_for_edited_module.py")

    def edit_event(name):
        return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                           "tool_input": {"file_path": str(project / name)}, "cwd": str(project)})

    rc, out, err, _ = run(cmd, edit_event("a.txt"))
    check("a repository other than the session's project is not run", rc == 0 and "canary-failed" not in err,
          f"rc={rc} {err}")
    rc, out, err, _ = run(cmd, edit_event("a.txt"), project=project)
    check("canary: registered test_for_edited_module.py reports a failing mapped test (exit 2)",
          rc == 2 and "canary-failed" in err, f"rc={rc} {out}{err}")
    check("a mapped test failure is not mislabelled as a hook crash", "failing closed" not in err, err)
    rc, out, err, _ = run(cmd, edit_event("b.txt"), project=project)
    check("canary: registered test_for_edited_module.py passes a passing mapped test", rc == 0, err)

with tempfile.TemporaryDirectory() as raw:
    project = Path(raw)
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=project, env=env, check=True)
    (project / "harness.toml").write_text('[adr]\nrequire_for = ["hooks/*.py"]\n')
    (project / "docs" / "adr").mkdir(parents=True)
    (project / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy2(ROOT / ".claude" / "hooks" / "adr_gate.py", project / ".claude" / "hooks")
    (project / "scripts").mkdir()
    for name in ("adr.py", "harness_config.py", "harness_events.py"):
        shutil.copy2(ROOT / "scripts" / name, project / "scripts")
    pre, _ = command("PreToolUse", "Edit|Write", "adr_gate.py")
    post, _ = command("PostToolUse", "Edit|Write", "adr_gate.py")

    def write_event(event, name, tool="Write", where=None):
        where = where or project
        return json.dumps({"hook_event_name": event, "tool_name": tool,
                           "tool_input": {"file_path": str(where / name)}, "cwd": str(where)})

    def as_project(cmd, payload):
        """Run the hook with the temp repository as the session's project."""
        return run(cmd, payload, project=project)

    rc, out, err, secs = as_project(pre, write_event("PreToolUse", "hooks/new.py"))
    check("canary: registered adr_gate.py denies a governed file no ADR names", rc == 0 and denied(out), out + err)
    check("canary: adr_gate.py answers well inside its timeout", secs < 5, f"{secs:.1f}s")
    rc, out, err, _ = as_project(pre, write_event("PreToolUse", "hooks/new.py", tool="Edit"))
    check("canary: an Edit that would create a governed file is denied too", rc == 0 and denied(out), out + err)
    foreign = Path(tempfile.mkdtemp(dir=raw, prefix="foreign."))
    subprocess.run(["git", "init", "-q"], cwd=foreign, env=env, check=True)
    rc, out, err, _ = as_project(pre, write_event("PreToolUse", ".claude/hooks/new.py", where=foreign))
    check("a Write into a repository other than the session's project passes", rc == 0 and not denied(out),
          out + err)
    (foreign / "harness.toml").write_text('[tool]\nname = "x"\n')
    rc, out, err, _ = as_project(post, write_event("PostToolUse", "README.md", where=foreign))
    check("another repository's own harness.toml schema does not block its Writes", rc == 0, f"rc={rc} {err}")
    (project / "docs" / "adr" / "0001-new-hook.md").write_text(
        "# 0001. New hook\n\nStatus: proposed\nDate: 2026-10-22\nComponents: hooks/new.py\n\n"
        "## Context\n\nc\n\n## Decision\n\nd\n\n## Consequences\n\nq\n")
    rc, out, err, _ = as_project(pre, write_event("PreToolUse", "hooks/new.py"))
    check("canary: registered adr_gate.py allows it once an ADR names it", rc == 0 and not denied(out), out + err)
    (project / "docs" / "adr" / "0002-bad.md").write_text("# 0002. Bad\n\nStatus: maybe\n")
    rc, out, err, _ = as_project(post, write_event("PostToolUse", "docs/adr/0002-bad.md"))
    check("canary: registered adr_gate.py exits 2 on a malformed ADR", rc == 2 and "Status must be" in err,
          f"rc={rc} {err}")
    rc, out, err, _ = as_project(post, write_event("PostToolUse", "docs/adr/0001-new-hook.md"))
    check("canary: registered adr_gate.py passes a well-formed ADR", rc == 0, f"rc={rc} {err}")

# --- failure direction: gates fail closed, advisories fail open ---------------
with tempfile.TemporaryDirectory() as raw:
    project = Path(raw)
    shutil.copytree(ROOT / ".claude", project / ".claude")
    shutil.copytree(ROOT / "scripts", project / "scripts")
    for (event, matcher, script), (cmd, _) in sorted(REG.items()):
        target = project / ".claude" / "hooks" / script
        if not target.is_file():
            target = project / "scripts" / script
        original = target.read_text()
        target.write_text("raise RuntimeError('simulated crash')\n")
        try:
            rc, out, err, _ = run(cmd, "{}", project=project)
        finally:
            target.write_text(original)
        if script in GATES:
            check(f"{event} gate {script} fails closed on a crash (exit 2)", rc == 2, f"rc={rc} {err}")
            check(f"{event} gate {script} says it failed closed", "failing closed" in err, err)
        else:
            check(f"{event} advisory {script} fails open on a crash", rc == 0, f"rc={rc} {err}")
        target.unlink()
        try:
            rc, _, err, _ = run(cmd, "{}", project=project)
        finally:
            target.write_text(original)
        if script in GATES:
            check(f"{event} gate {script} fails closed when its file is missing", rc == 2, f"rc={rc}")
        else:
            check(f"{event} advisory {script} fails open when its file is missing", rc == 0, f"rc={rc} {err}")

# --- harness_doctor names interfering user-level hooks, never blocks ----------
DOCTOR = ROOT / "scripts" / "harness_doctor.py"
with tempfile.TemporaryDirectory() as raw:
    config = Path(raw)
    (config / "settings.json").write_text(json.dumps({"hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "python3 /x/warmstart.py"}]}],
        "PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "python3 /x/push-gate.py"}]},
                       {"matcher": "Read", "hooks": [{"type": "command", "command": "python3 /x/read-only.py"}]}],
    }}))
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    done = subprocess.run([sys.executable, str(DOCTOR)], capture_output=True, text=True,
                          env={**env, "CLAUDE_CONFIG_DIR": str(config)})
    check("doctor exits 0 with findings", done.returncode == 0, done.stderr)
    check("doctor names a user SubagentStart hook", "warmstart.py" in done.stdout, done.stdout)
    check("doctor names a user PreToolUse Bash hook", "push-gate.py" in done.stdout, done.stdout)
    check("doctor ignores a user hook on an unrelated tool", "read-only.py" not in done.stdout, done.stdout)
    (config / "settings.json").write_text("{not json")
    done = subprocess.run([sys.executable, str(DOCTOR)], capture_output=True, text=True,
                          env={**env, "CLAUDE_CONFIG_DIR": str(config)})
    check("doctor survives unreadable user settings", done.returncode == 0, done.stderr)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
