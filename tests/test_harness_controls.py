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

SCRATCH = Path(tempfile.mkdtemp(prefix="harness-controls."))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


# (event, matcher, script, kind). Hand-written; never read from settings.json.
EXPECTED = {
    ("PreToolUse", "Agent", "require_agent_model.py", "gate"),
    ("PreToolUse", "Agent", "model_router.py", "gate"),
    ("PreToolUse", "Edit|Write", "blast_radius_advisory.py", "advisory"),
    ("PreToolUse", "Bash", "verify_commit_landed.py", "advisory"),
    ("PostToolUse", "Bash", "verify_commit_landed.py", "gate"),
    ("PostToolUse", "Edit|Write", "test_for_edited_module.py", "gate"),
    ("UserPromptSubmit", "", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "harness_doctor.py", "advisory"),
    ("PreToolUse", "Bash", "queue_guard.py", "advisory"),
    ("SubagentStart", "", "readonly_guard.py", "advisory"),
    ("SubagentStop", "", "readonly_guard.py", "advisory"),
    ("Stop", "", "session_guard.py", "advisory"),
    ("SessionEnd", "", "session_guard.py", "advisory"),
    ("PreToolUse", "Bash", "loop_guard.py", "gate"),
    ("PostToolUse", "Bash", "loop_guard.py", "gate"),
    ("PostToolUseFailure", "Bash", "loop_guard.py", "gate"),
    ("PostToolUse", "Edit|Write", "loop_guard.py", "gate"),
    ("PreToolUse", "Edit|Write", "loop_guard.py", "gate"),
    ("PreToolUse", "Edit|Write", "scope_guard.py", "gate"),
    ("PreToolUse", "", "budget_guard.py", "gate"),
    ("Stop", "", "turn_guard.py", "gate"),
}
GATES = {(event, script) for event, _, script, kind in EXPECTED if kind == "gate"}

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
            or out.get("decision") in ("block", "deny"))


REG = registered()
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



# --- Codex and Gemini registrations: hand-written, never read from harness.toml ----
# Rendered by scripts/render_hooks.py. Codex hosts every hook (tool matchers renamed);
# Gemini has no Agent tool matcher and no subagent events, so three guards are absent
# there (recorded gap, ADR 0015).
CODEX_EXPECTED = {
    ("PreToolUse", "spawn_agent", "require_agent_model.py", "gate"),
    ("PreToolUse", "spawn_agent", "model_router.py", "gate"),
    ("PreToolUse", "apply_patch|Bash", "blast_radius_advisory.py", "advisory"),
    ("PreToolUse", "Bash", "verify_commit_landed.py", "advisory"),
    ("PostToolUse", "Bash", "verify_commit_landed.py", "gate"),
    ("PostToolUse", "apply_patch|Bash", "test_for_edited_module.py", "gate"),
    ("UserPromptSubmit", "", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "harness_doctor.py", "advisory"),
    ("PreToolUse", "Bash", "queue_guard.py", "advisory"),
    ("SubagentStart", "", "readonly_guard.py", "advisory"),
    ("SubagentStop", "", "readonly_guard.py", "advisory"),
    ("Stop", "", "session_guard.py", "advisory"),
    ("SessionEnd", "", "session_guard.py", "advisory"),
    ("PreToolUse", "Bash", "loop_guard.py", "gate"),
    ("PostToolUse", "Bash", "loop_guard.py", "gate"),
    ("PostToolUse", "apply_patch|Bash", "loop_guard.py", "gate"),
    ("PreToolUse", "apply_patch|Bash", "loop_guard.py", "gate"),
    ("PreToolUse", "apply_patch|Bash", "scope_guard.py", "gate"),
    ("PreToolUse", "", "budget_guard.py", "gate"),
    ("Stop", "", "turn_guard.py", "gate"),
}
GEMINI_EXPECTED = {
    ("BeforeTool", "replace|write_file", "blast_radius_advisory.py", "advisory"),
    ("BeforeTool", "run_shell_command", "verify_commit_landed.py", "advisory"),
    ("AfterTool", "run_shell_command", "verify_commit_landed.py", "gate"),
    ("AfterTool", "replace|write_file", "test_for_edited_module.py", "gate"),
    ("BeforeAgent", "", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "codegraph_freshness.py", "advisory"),
    ("SessionStart", "startup", "harness_doctor.py", "advisory"),
    ("BeforeTool", "run_shell_command", "queue_guard.py", "advisory"),
    ("AfterAgent", "", "session_guard.py", "advisory"),
    ("SessionEnd", "", "session_guard.py", "advisory"),
    ("BeforeTool", "run_shell_command", "loop_guard.py", "gate"),
    ("AfterTool", "run_shell_command", "loop_guard.py", "gate"),
    ("AfterTool", "replace|write_file", "loop_guard.py", "gate"),
    ("BeforeTool", "replace|write_file", "loop_guard.py", "gate"),
    ("BeforeTool", "replace|write_file", "scope_guard.py", "gate"),
    ("BeforeTool", "", "budget_guard.py", "gate"),
    ("AfterAgent", "", "turn_guard.py", "gate"),
}
# Expected timeouts are the Claude ones: seconds for Codex, milliseconds for Gemini.
CLAUDE_TIMEOUT = {(e, m, s_): t for (e, m, s_), (_, t) in registered().items()}


def adapted_registrations(path, runtime):
    """Return {(event, matcher, script): (command, timeout)} for a rendered adapter file."""
    out = {}
    for event, groups in json.loads(path.read_text())["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                scripts = [x for x in re.findall(r"[\w.-]+\.py\b", hook["command"]) if x != "hook_adapter.py"]
                out[(event, group.get("matcher", ""), scripts[0])] = (hook["command"], hook["timeout"])
    return out


def registration_list(path):
    """Return every (event, matcher, script) in a rendered file as a list, duplicates kept."""
    out = []
    for event, groups in json.loads(path.read_text())["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                scripts = [x for x in re.findall(r"[\w.-]+\.py\b", hook["command"]) if x != "hook_adapter.py"]
                out.append((event, group.get("matcher", ""), scripts[0]))
    return sorted(out)


for label, listed, expected in (("settings.json", registration_list(SETTINGS), EXPECTED),
                                ("hooks.json", registration_list(ROOT / ".codex" / "hooks.json"), CODEX_EXPECTED),
                                ("gemini settings.json", registration_list(ROOT / ".gemini" / "settings.json"),
                                 GEMINI_EXPECTED)):
    check(f"{label} lists each expected hook exactly once (a duplicate or missing entry fails)",
          listed == sorted((e, m, s_) for e, m, s_, _ in expected), str(listed))

for runtime, path, expected in (("codex", ROOT / ".codex" / "hooks.json", CODEX_EXPECTED),
                                ("gemini", ROOT / ".gemini" / "settings.json", GEMINI_EXPECTED)):
    reg = adapted_registrations(path, runtime)
    want = {(e, m, s_) for e, m, s_, _ in expected}
    check(f"{path.name} registers exactly the expected {runtime} hooks", set(reg) == want,
          f"unexpected: {sorted(set(reg) - want)}; missing: {sorted(want - set(reg))}")
    for event, matcher, script, kind in sorted(expected):
        if (event, matcher, script) not in reg:
            continue
        cmd, timeout = reg[(event, matcher, script)]
        check(f"{runtime} {event} {script} runs through hook_adapter for {runtime}",
              f"hook_adapter.py\" --timeout {timeout // (1 if runtime == 'codex' else 1000)}"
              f"{' --edits' if runtime == 'codex' and matcher == 'apply_patch|Bash' else ''} "
              f"--runtime {runtime} --event {event} -- " in cmd, cmd)
        tail = cmd.rsplit("' ", 1)[-1]
        if kind == "gate":
            check(f"{runtime} {event} gate {script} fails closed", tail.endswith("exit 2; }") and "|| true" not in tail, tail)
        else:
            check(f"{runtime} {event} advisory {script} fails open", tail == "|| true", tail)
    hosted = [t for (e_, _, s_), t in CLAUDE_TIMEOUT.items()
              if e_ != "PostToolUseFailure" and (runtime == "codex" or s_ not in ("require_agent_model.py", "model_router.py", "readonly_guard.py"))]
    scale = 1 if runtime == "codex" else 1000
    check(f"{runtime} timeouts are the Claude timeouts x {scale}",
          sorted(t for _, t in reg.values()) == sorted(t * scale for t in hosted))

sys.path.insert(0, str(ROOT / "scripts"))
import render_hooks  # noqa: E402

check("the rendered hook files are fresh (python3 scripts/render_hooks.py)", render_hooks.stale() == [],
      str(render_hooks.stale()))
check("Gemini leaves out exactly the Agent gates, the subagent guard and the PostToolUseFailure guard; Codex only the latter",
      sorted(render_hooks.gaps(__import__("harness_config").load(ROOT)["hook"]))
      == [("loop_guard_PostToolUseFailure", "codex has no PostToolUseFailure event"),
          ("loop_guard_PostToolUseFailure", "gemini has no PostToolUseFailure event"),
          ("model_router_PreToolUse", "gemini has no tool for matcher 'Agent'"),
          ("readonly_guard_SubagentStart", "gemini has no SubagentStart event"),
          ("readonly_guard_SubagentStop", "gemini has no SubagentStop event"),
          ("require_agent_model_PreToolUse", "gemini has no tool for matcher 'Agent'")])
check("settings.json keeps its non-hook keys", "permissions" in json.loads(SETTINGS.read_text()))
check(".gemini/settings.json keeps its context.fileName", json.loads((ROOT / ".gemini" / "settings.json").read_text())
      .get("context") == {"fileName": ["AGENTS.md"]})


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
    pre_cmd, _ = command("PreToolUse", "Bash", "verify_commit_landed.py")
    cmd, timeout = command("PostToolUse", "Bash", "verify_commit_landed.py")

    def bash_event(event, response=""):
        return json.dumps({"hook_event_name": event, "session_id": "canary", "tool_name": "Bash",
                           "tool_input": {"command": "git commit -q -m x"},
                           "tool_response": {"stdout": response}})

    run(pre_cmd, bash_event("PreToolUse"), cwd=repo)
    rc, out, err, secs = run(cmd, bash_event("PostToolUse", "[work 1234567] base"), cwd=repo)
    check("canary: registered verify_commit_landed.py blocks a commit that did not move HEAD, whatever git printed",
          rc == 0 and denied(out), out + err)
    run(pre_cmd, bash_event("PreToolUse"), cwd=repo)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "commit", "-q", "--allow-empty", "-m", "two"], cwd=repo, env=env, check=True)
    rc, out, err, _ = run(cmd, bash_event("PostToolUse", ""), cwd=repo)
    check("canary: registered verify_commit_landed.py passes a `git commit -q` that moved HEAD and printed nothing",
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
    # adr_gate.py is unregistered here (adr.require_for is empty); this is the command a
    # project that opts in registers, so the canaries below still prove the guard.
    pre = post = ('python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/adr_gate.py" || { rc=$?; [ "$rc" = 2 ] || '
                  'echo "adr_gate.py crashed (rc=$rc); failing closed" >&2; exit 2; }')

    def write_event(event, name, tool="Write", where=None):
        where = where or project
        return json.dumps({"hook_event_name": event, "tool_name": tool,
                           "tool_input": {"file_path": str(where / name)}, "cwd": str(where)})

    def as_project(cmd, payload):
        """Run the hook with the temp repository as the session's project."""
        return run(cmd, payload, project=project)

    rc, out, err, secs = as_project(pre, write_event("PreToolUse", "hooks/new.py"))
    check("canary: opt-in adr_gate.py denies a governed file no ADR names", rc == 0 and denied(out), out + err)
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
    check("canary: opt-in adr_gate.py allows it once an ADR names it", rc == 0 and not denied(out), out + err)
    (project / "docs" / "adr" / "0002-bad.md").write_text("# 0002. Bad\n\nStatus: maybe\n")
    rc, out, err, _ = as_project(post, write_event("PostToolUse", "docs/adr/0002-bad.md"))
    check("canary: opt-in adr_gate.py exits 2 on a malformed ADR", rc == 2 and "Status must be" in err,
          f"rc={rc} {err}")
    rc, out, err, _ = as_project(post, write_event("PostToolUse", "docs/adr/0001-new-hook.md"))
    check("canary: opt-in adr_gate.py passes a well-formed ADR", rc == 0, f"rc={rc} {err}")

# --- loop, scope, budget and turn guards: a deny and an allow canary per runtime -------------------
# Each case runs the registered command (the adapter command under Codex and Gemini) in a throwaway
# git repository that holds copies of the hooks and scripts, so the adapter's REPO is that copy.
CODEX_REG = adapted_registrations(ROOT / ".codex" / "hooks.json", "codex")
GEMINI_REG = adapted_registrations(ROOT / ".gemini" / "settings.json", "gemini")
EDIT_PATCH = "*** Begin Patch\n*** Add File: {name}\n+x = 1\n*** End Patch"


def guard_project(budget=""):
    """Return a git repository on branch work with the hooks, scripts and a [budget] copied in."""
    proj = Path(tempfile.mkdtemp(prefix="guards.", dir=SCRATCH))
    shutil.copytree(ROOT / ".claude" / "hooks", proj / ".claude" / "hooks")
    shutil.copytree(ROOT / "scripts", proj / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (proj / ".gitignore").write_text("__pycache__/\n")
    (proj / "harness.toml").write_text(f"[budget]\n{budget}\n" if budget else "")
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t",
                 "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"], ["checkout", "-q", "-b", "work"]):
        subprocess.run(["git", *args], cwd=proj, env=env, check=True, capture_output=True)
    return proj


def lease(proj, globs, max_files=5, max_lines=100):
    args = [a for g_ in globs for a in ("--glob", g_)]
    subprocess.run([sys.executable, str(proj / "scripts" / "scope.py"), "set", *args, "--max-files", str(max_files),
                    "--max-lines", str(max_lines), "--task", "canary", "--base", "main"], cwd=proj, check=True,
                   capture_output=True, env={k: v for k, v in os.environ.items() if k not in GIT_ENV})


def invoke(runtime, event, matcher, script, payload, proj):
    """Run one guard for one runtime; return (rc, stdout, stderr)."""
    if runtime == "claude":
        cmd = REG[(event, matcher, script)][0]
    else:
        cmd = (CODEX_REG if runtime == "codex" else GEMINI_REG)[(event, matcher, script)][0]
    rc, out, err, _ = run(cmd, json.dumps({**payload, "cwd": str(proj)}), project=proj, cwd=proj)
    return rc, out, err


# (runtime, (pre event, bash matcher, edit-guard matcher), (post event, bash matcher), stop event, bash tool name)
RUNTIMES = (("claude", ("PreToolUse", "Bash", "Edit|Write"), ("PostToolUseFailure", "Bash"), "Stop", "Bash"),
            ("codex", ("PreToolUse", "Bash", "apply_patch|Bash"), ("PostToolUse", "Bash"), "Stop", "Bash"),
            ("gemini", ("BeforeTool", "run_shell_command", "replace|write_file"), ("AfterTool", "run_shell_command"),
             "AfterAgent", "run_shell_command"))
FAILED_COMMAND = "python3 tests/test_canary.py"
for runtime, (pre, bash_m, edit_m), (post, post_m), stop_event, bash_tool in RUNTIMES:
    proj = guard_project()
    sid = f"canary-{runtime}"
    # loop_guard: a command fails, then the identical command is denied; a different command passes.
    if runtime == "claude":
        failure = {"hook_event_name": post, "tool_name": "Bash", "session_id": sid,
                   "tool_input": {"command": FAILED_COMMAND}, "error": "Exit code 1\nE: canary boom"}
    elif runtime == "codex":
        # Codex 0.164 sends the output text alone; the exit code is in its rollout (probed live).
        rollout = SCRATCH / "codex-rollout.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"item": {"id": "exec-canary", "exit_code": 1}}}) + "\n")
        failure = {"hook_event_name": post, "tool_name": "Bash", "session_id": sid, "tool_input": {"command": FAILED_COMMAND},
                   "tool_response": "E: canary boom\n", "tool_use_id": "exec-canary", "transcript_path": str(rollout)}
    else:
        failure = {"hook_event_name": post, "tool_name": bash_tool, "session_id": sid,
                   "tool_input": {"command": FAILED_COMMAND},
                   "tool_response": {"llmContent": "Output: E: canary boom\nExit Code: 1", "returnDisplay": ""}}
    rc, out, err = invoke(runtime, post, post_m, "loop_guard.py", failure, proj)
    check(f"{runtime}: loop_guard records a failed command without blocking the first time", rc == 0 and not denied(out), out + err)
    pre_payload = lambda cmd: {"hook_event_name": pre, "tool_name": bash_tool, "session_id": sid, "tool_input": {"command": cmd}}  # noqa: E731
    rc, out, err = invoke(runtime, pre, bash_m, "loop_guard.py", pre_payload(FAILED_COMMAND), proj)
    check(f"{runtime}: canary: loop_guard denies an unchanged re-run of the failed command",
          rc == 0 and denied(out) and "never re-run an unchanged failing command" in out.lower(), f"rc={rc} {out}{err}")
    rc, out, err = invoke(runtime, pre, bash_m, "loop_guard.py", pre_payload("python3 tests/test_other.py"), proj)
    check(f"{runtime}: canary: loop_guard allows a different command", rc == 0 and not denied(out), f"rc={rc} {out}{err}")
    # scope_guard: out of the lease's globs is denied, inside passes.
    lease(proj, ["allowed/**"])
    if runtime == "codex":
        edits = lambda name: {"hook_event_name": pre, "tool_name": "apply_patch", "tool_input": {"command": EDIT_PATCH.format(name=name)}}  # noqa: E731
    else:
        tool = "write_file" if runtime == "gemini" else "Write"
        edits = lambda name: {"hook_event_name": pre, "tool_name": tool, "tool_input": {"file_path": str(proj / name), "content": "x\n"}}  # noqa: E731
    rc, out, err = invoke(runtime, pre, edit_m, "scope_guard.py", edits("elsewhere/x.py"), proj)
    check(f"{runtime}: canary: scope_guard denies an edit outside the lease", rc == 0 and denied(out) and "out of scope: elsewhere/x.py" in out,
          f"rc={rc} {out}{err}")
    rc, out, err = invoke(runtime, pre, edit_m, "scope_guard.py", edits("allowed/x.py"), proj)
    check(f"{runtime}: canary: scope_guard allows an edit inside the lease", rc == 0 and not denied(out), f"rc={rc} {out}{err}")
    # budget_guard: one call of one is allowed, the next is denied.
    proj = guard_project("max_tool_calls = 1\nmax_minutes = 0")
    tool = "Read" if runtime != "gemini" else "read_file"
    budget_payload = {"hook_event_name": pre, "tool_name": tool, "session_id": sid, "tool_input": {}}
    rc, out, err = invoke(runtime, pre, "", "budget_guard.py", budget_payload, proj)
    check(f"{runtime}: canary: budget_guard allows a call inside the budget", rc == 0 and not denied(out), f"rc={rc} {out}{err}")
    rc, out, err = invoke(runtime, pre, "", "budget_guard.py", budget_payload, proj)
    check(f"{runtime}: canary: budget_guard denies a call past the budget", rc == 0 and denied(out)
          and "budget exhausted: write status (done, left, blockers) and stop" in out, f"rc={rc} {out}{err}")
    # turn_guard: an announcing last message blocks once; a finished one passes.
    stop = lambda text, sid_: {"hook_event_name": stop_event, "session_id": sid_,  # noqa: E731
                               ("prompt_response" if runtime == "gemini" else "last_assistant_message"): text}
    proj = guard_project()
    rc, out, err = invoke(runtime, stop_event, "", "turn_guard.py", stop("Next I will run the full suite.", sid), proj)
    check(f"{runtime}: canary: turn_guard blocks a final message that announces unstarted work", rc == 0 and denied(out)
          and "do it or name the blocker" in out, f"rc={rc} {out}{err}")
    rc, out, err = invoke(runtime, stop_event, "", "turn_guard.py", stop("Next I will run the full suite.", sid), proj)
    check(f"{runtime}: turn_guard blocks the same message only once", rc == 0 and not denied(out), f"rc={rc} {out}{err}")
    rc, out, err = invoke(runtime, stop_event, "", "turn_guard.py", stop("All done; 12 checks pass.", sid + "-2"), proj)
    check(f"{runtime}: canary: turn_guard allows a finished final message", rc == 0 and not denied(out), f"rc={rc} {out}{err}")

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
        if (event, script) in GATES:
            check(f"{event} gate {script} fails closed on a crash (exit 2)", rc == 2, f"rc={rc} {err}")
            check(f"{event} gate {script} says it failed closed", "failing closed" in err, err)
        else:
            check(f"{event} advisory {script} fails open on a crash", rc == 0, f"rc={rc} {err}")
        target.unlink()
        try:
            rc, _, err, _ = run(cmd, "{}", project=project)
        finally:
            target.write_text(original)
        if (event, script) in GATES:
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
