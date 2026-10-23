"""scripts/hook_adapter.py runs the Claude-shaped guards unchanged under Codex and Gemini.

Unit checks cover the translation (apply_patch, tool and event names, results the
target cannot express). End-to-end checks run the command string rendered into
.codex/hooks.json and .gemini/settings.json through a shell, as each CLI does,
with that CLI's payload on stdin, and look for that CLI's own deny shape.
Every expectation is a literal written here (CLAUDE.md Rule 11).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import hook_adapter as ha  # noqa: E402

os.environ["HARNESS_EVENTS_LOG"] = str(
    Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl"
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


def raises(fn, kind=ha.AdapterError):
    try:
        fn()
    except kind:
        return True
    except Exception:
        return False
    return False


GIT_ENV = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


def patch(*ops):
    return "*** Begin Patch\n" + "\n".join(ops) + "\n*** End Patch"


# --- translation ----------------------------------------------------------------
two = patch("*** Add File: a.py", "+x", "*** Update File: b.py", "@@", "-old", "+new")
events = ha.to_claude(
    "codex",
    "PreToolUse",
    {"tool_name": "apply_patch", "cwd": "/w", "tool_input": {"command": two}},
)
check("apply_patch with 2 files yields 2 events", len(events) == 2, str(events))
check(
    "apply_patch events are one Write and one Edit, in patch order",
    [e["tool_name"] for e in events] == ["Write", "Edit"],
)
check(
    "apply_patch file paths are made absolute from cwd",
    [e["tool_input"]["file_path"] for e in events] == ["/w/a.py", "/w/b.py"],
)
check(
    "an Edit event carries the old and new text",
    events[1]["tool_input"]["old_string"] == "old\n"
    and events[1]["tool_input"]["new_string"] == "new\n",
)
moved = ha.to_claude(
    "codex",
    "PreToolUse",
    {
        "tool_name": "apply_patch",
        "cwd": "/w",
        "tool_input": {
            "command": patch(
                "*** Update File: a.py", "*** Move to: c.py", "@@", "-x", "+y"
            )
        },
    },
)
check(
    "a move is an Edit of the old path and a Write of the new one",
    [(e["tool_name"], Path(e["tool_input"]["file_path"]).name) for e in moved]
    == [("Edit", "a.py"), ("Write", "c.py")],
)
check(
    "a patch without End marker is refused",
    raises(lambda: ha.parse_apply_patch("*** Begin Patch\n*** Add File: a\n+x")),
)
check(
    "an unknown patch operation is refused",
    raises(lambda: ha.parse_apply_patch(patch("*** Frob File: a"))),
)
check(
    "apply_patch on a Stop event is refused",
    raises(
        lambda: ha.to_claude(
            "codex",
            "Stop",
            {"tool_name": "apply_patch", "tool_input": {"command": two}},
        )
    ),
)
shell = ha.to_claude(
    "codex",
    "PreToolUse",
    {"tool_name": "exec_command", "tool_input": {"command": "ls"}},
)[0]
check(
    "a Codex shell tool becomes Bash",
    shell["tool_name"] == "Bash" and shell["tool_input"]["command"] == "ls",
)
spawn = ha.to_claude(
    "codex",
    "PreToolUse",
    {"tool_name": "spawn_agent", "tool_input": {"agent_type": "Explore"}},
)[0]
check(
    "spawn_agent becomes Agent with subagent_type",
    spawn["tool_name"] == "Agent" and spawn["tool_input"]["subagent_type"] == "Explore",
)
for native, claude in (
    ("run_shell_command", "Bash"),
    ("write_file", "Write"),
    ("replace", "Edit"),
):
    got = ha.to_claude(
        "gemini",
        "PreToolUse",
        {"tool_name": native, "tool_input": {"file_path": "/w/x"}},
    )[0]
    check(f"Gemini {native} becomes {claude}", got["tool_name"] == claude)
after = ha.to_claude(
    "gemini",
    "PostToolUse",
    {
        "tool_name": "run_shell_command",
        "tool_input": {"command": "git commit"},
        "tool_response": {"llmContent": "[work abc123] msg"},
    },
)[0]
check(
    "a Gemini tool result becomes a Claude tool_response with stdout",
    after["tool_response"]["stdout"] == "[work abc123] msg",
)

# --- results ----------------------------------------------------------------------
check(
    "guard exit 2 is a deny carrying stderr", ha.interpret(2, "", "no\n").deny == "no"
)
check(
    "guard exit 1 raises (a crash is not an answer)",
    raises(lambda: ha.interpret(1, "", "boom")),
)
deny_json = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "why",
        }
    }
)
check(
    "a permissionDecision deny is a deny", ha.interpret(0, deny_json, "").deny == "why"
)
check(
    "a decision block is a deny",
    ha.interpret(0, json.dumps({"decision": "block", "reason": "r"}), "").deny == "r",
)
check("plain stdout is context", ha.interpret(0, "hello\n", "").context == ["hello"])
check(
    "text before a JSON line is kept as context",
    ha.interpret(0, 'note\n{"systemMessage": "m"}', "").context == ["note"],
)
check(
    "updatedInput cannot be carried and raises",
    raises(
        lambda: ha.interpret(
            0, json.dumps({"hookSpecificOutput": {"updatedInput": {}}}), ""
        ),
        ha.OutputNormalizationError,
    ),
)
check(
    "continue=false cannot be carried and raises",
    raises(
        lambda: ha.interpret(0, json.dumps({"continue": False}), ""),
        ha.OutputNormalizationError,
    ),
)
check(
    "a deny on SessionStart raises for Codex",
    raises(
        lambda: ha.emit("codex", "SessionStart", ha.Result(deny="x")),
        ha.OutputNormalizationError,
    ),
)
check(
    "a deny on SessionStart raises for Gemini",
    raises(
        lambda: ha.emit("gemini", "SessionStart", ha.Result(deny="x")),
        ha.OutputNormalizationError,
    ),
)
check(
    "a deny on SubagentStart raises for Codex",
    raises(
        lambda: ha.emit("codex", "SubagentStart", ha.Result(deny="x")),
        ha.OutputNormalizationError,
    ),
)
check(
    "Codex PreToolUse deny is a permissionDecision",
    ha.emit("codex", "PreToolUse", ha.Result(deny="x"))["hookSpecificOutput"][
        "permissionDecision"
    ]
    == "deny",
)
check(
    "Codex Stop deny is a block",
    ha.emit("codex", "Stop", ha.Result(deny="x"))
    == {"decision": "block", "reason": "x"},
)
check(
    "Gemini AfterAgent deny is a deny",
    ha.emit("gemini", "Stop", ha.Result(deny="x"))
    == {"decision": "deny", "reason": "x"},
)
check(
    "Gemini BeforeTool context goes to systemMessage (no additionalContext there)",
    ha.emit("gemini", "PreToolUse", ha.Result(context=["c"])) == {"systemMessage": "c"},
)
check(
    "Gemini SessionStart context is additionalContext",
    ha.emit("gemini", "SessionStart", ha.Result(context=["c"]))["hookSpecificOutput"]
    == {"hookEventName": "SessionStart", "additionalContext": "c"},
)
check(
    "an event the runtime lacks is refused",
    raises(lambda: ha.handle("gemini", "SubagentStart", "{}", "true")),
)
check(
    "a payload that is not JSON is refused",
    raises(lambda: ha.handle("codex", "Stop", "not json", "true")),
)
check(
    "any deny among per-file results wins",
    ha.merge([ha.Result(), ha.Result(deny="d"), ha.Result()]).deny == "d",
)


# --- registered commands, run as each CLI runs them ---------------------------------
def load(runtime):
    """Return {(event, matcher, script): (command, timeout)} from the rendered file."""
    path = ROOT / (
        ".codex/hooks.json" if runtime == "codex" else ".gemini/settings.json"
    )
    out = {}
    for event, groups in json.loads(path.read_text())["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                scripts = [
                    s
                    for s in __import__("re").findall(r"[\w.-]+\.py\b", hook["command"])
                    if s != "hook_adapter.py"
                ]
                out[(event, group.get("matcher", ""), scripts[0])] = hook["command"]
    return out


REG = {"codex": load("codex"), "gemini": load("gemini")}


def run(runtime, event, matcher, script, native, project=ROOT, cwd=None):
    """Run one rendered registration; return (rc, stdout, stderr).

    `matcher` is only the caller's label: the CLIs name tools differently, so the
    registration is found by event and script.
    """
    command = next(c for (e, _, sc), c in REG[runtime].items() if (e, sc) == (event, script))
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    done = subprocess.run(
        ["bash", "-c", command],
        input=json.dumps(native),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or project),
        timeout=180,
    )
    return done.returncode, done.stdout, done.stderr


def denied(runtime, stdout):
    try:
        out = json.loads(stdout)
    except ValueError:
        return False
    if runtime == "codex":
        return (
            out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            or out.get("decision") == "block"
        )
    return out.get("decision") == "deny"


def codex_patch(cwd, *ops):
    return {
        "hook_event_name": "x",
        "tool_name": "apply_patch",
        "cwd": str(cwd),
        "tool_input": {"command": patch(*ops)},
    }


# Agent gates: Codex only (Gemini has no Agent tool matcher; a recorded gap).
def spawn(agent, model):
    tool_input = {"agent_type": agent, "message": "y"}
    if model:
        tool_input["model"] = model
    return {"tool_name": "spawn_agent", "tool_input": tool_input, "cwd": str(ROOT)}


for script, bad, good in (
    (
        "require_agent_model.py",
        spawn("python-general-engineer", None),
        spawn("python-general-engineer", "sonnet"),
    ),
    ("model_router.py", spawn("Explore", "opus"), spawn("Explore", "haiku")),
):
    rc, out, err = run("codex", "PreToolUse", "spawn_agent", script, bad)
    check(
        f"codex: {script} denies a bad spawn_agent",
        rc == 0 and denied("codex", out),
        out + err,
    )
    rc, out, err = run("codex", "PreToolUse", "spawn_agent", script, good)
    check(
        f"codex: {script} allows a good spawn_agent",
        rc == 0 and not denied("codex", out),
        out + err,
    )

# adr_gate on the real repository: governed and ungoverned paths, both runtimes.
for event in ("PreToolUse",):
    rc, out, err = run(
        "codex",
        event,
        "apply_patch",
        "adr_gate.py",
        codex_patch(ROOT, "*** Add File: scripts/zzz_canary.py", "+x"),
    )
    check(
        "codex: adr_gate denies an apply_patch adding a governed file no ADR names",
        rc == 0 and denied("codex", out),
        out + err,
    )
    rc, out, err = run(
        "codex",
        event,
        "apply_patch",
        "adr_gate.py",
        codex_patch(ROOT, "*** Add File: notes_canary.md", "+x"),
    )
    check(
        "codex: adr_gate allows an apply_patch adding an ungoverned file",
        rc == 0 and not denied("codex", out),
        out + err,
    )
    rc, out, err = run(
        "codex",
        event,
        "apply_patch",
        "adr_gate.py",
        codex_patch(
            ROOT,
            "*** Add File: notes_canary.md",
            "+x",
            "*** Add File: scripts/zzz_canary.py",
            "+y",
        ),
    )
    check(
        "codex: one denied file in a two-file patch denies the patch",
        rc == 0 and denied("codex", out),
        out + err,
    )
    for path, want, label in (
        ("scripts/zzz_canary.py", True, "denies a governed file no ADR names"),
        ("notes_canary.md", False, "allows an ungoverned file"),
    ):
        native = {
            "tool_name": "write_file",
            "cwd": str(ROOT),
            "tool_input": {"file_path": str(ROOT / path), "content": "x"},
        }
        rc, out, err = run("gemini", "BeforeTool", "write_file", "adr_gate.py", native)
        check(
            f"gemini: adr_gate {label}",
            rc == 0 and denied("gemini", out) == want,
            out + err,
        )

# verify_commit_landed (PostToolUse Bash), both runtimes, in a scratch repository.
with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    for args in (
        ["init", "-q", "-b", "work"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
    ):
        subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True
        )
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for runtime, event, name, response in (
        ("codex", "PostToolUse", "Bash", lambda text: {"stdout": text}),
        ("gemini", "AfterTool", "run_shell_command", lambda text: {"llmContent": text}),
    ):
        for text, want, label in (
            ("nothing to commit", True, "blocks a commit that did not land"),
            (f"[work {head}] base", False, "passes a commit at HEAD"),
        ):
            native = {
                "tool_name": name,
                "cwd": str(repo),
                "tool_input": {"command": "git commit -m x"},
                "tool_response": response(text),
            }
            rc, out, err = run(
                runtime,
                event,
                "Bash" if runtime == "codex" else "run_shell_command",
                "verify_commit_landed.py",
                native,
            )
            check(
                f"{runtime}: verify_commit_landed {label}",
                rc == 0 and denied(runtime, out) == want,
                out + err,
            )

# test_for_edited_module, both runtimes, in a scratch project that carries the adapter.
with tempfile.TemporaryDirectory() as raw:
    project = Path(raw)
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    shutil.copytree(
        ROOT / "scripts",
        project / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        ROOT / ".claude" / "hooks",
        project / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (project / "harness.toml").write_text(
        '[[mapped_tests]]\nmatch = "*.txt"\ntests = ["{dir}/check_{stem}.sh"]\ncommand = "bash {test}"\n'
    )
    (project / "a.txt").write_text("x\n")
    (project / "check_a.sh").write_text("echo canary-failed; exit 1\n")
    (project / "b.txt").write_text("x\n")
    (project / "check_b.sh").write_text("exit 0\n")
    subprocess.run(["git", "init", "-q"], cwd=project, env=env, check=True)
    for runtime, event, matcher, native in (
        (
            "codex",
            "PostToolUse",
            "apply_patch",
            lambda n: codex_patch(project, f"*** Update File: {n}", "@@", "-x", "+y"),
        ),
        (
            "gemini",
            "AfterTool",
            "write_file|replace",
            lambda n: {
                "tool_name": "write_file",
                "cwd": str(project),
                "tool_input": {"file_path": str(project / n), "content": "y"},
                "tool_response": {"llmContent": "ok"},
            },
        ),
    ):
        rc, out, err = run(
            runtime,
            event,
            matcher,
            "test_for_edited_module.py",
            native("a.txt"),
            project=project,
        )
        check(
            f"{runtime}: test_for_edited_module blocks on a failing mapped test",
            rc == 0 and denied(runtime, out) and "canary-failed" in out,
            f"rc={rc} {out}{err}",
        )
        rc, out, err = run(
            runtime,
            event,
            matcher,
            "test_for_edited_module.py",
            native("b.txt"),
            project=project,
        )
        check(
            f"{runtime}: test_for_edited_module passes a passing mapped test",
            rc == 0 and not denied(runtime, out),
            f"rc={rc} {out}{err}",
        )

    # Failure direction: a guard that crashes blocks (gate) on every runtime; an advisory fails open.
    gates = {
        "require_agent_model.py",
        "model_router.py",
        "verify_commit_landed.py",
        "test_for_edited_module.py",
        "adr_gate.py",
    }
    for runtime in ("codex", "gemini"):
        for (event, matcher, script), command in sorted(REG[runtime].items()):
            target = project / ".claude" / "hooks" / script
            if not target.is_file():
                target = project / "scripts" / script
            original = target.read_text()
            target.write_text("raise RuntimeError('simulated crash')\n")
            try:
                rc, out, err = run(
                    runtime,
                    event,
                    matcher,
                    script,
                    {"cwd": str(project)},
                    project=project,
                )
            finally:
                target.write_text(original)
            if script in gates:
                check(
                    f"{runtime} {event} gate {script} blocks when the guard crashes",
                    denied(runtime, out) or rc == 2,
                    f"rc={rc} {out}{err}",
                )
            else:
                check(
                    f"{runtime} {event} advisory {script} does not block when the guard crashes",
                    rc == 0 and not denied(runtime, out),
                    f"rc={rc} {out}{err}",
                )
        adapter = project / "scripts" / "hook_adapter.py"
        original = adapter.read_text()
        adapter.write_text("raise RuntimeError('adapter crash')\n")
        try:
            for (event, matcher, script), command in sorted(REG[runtime].items()):
                rc, out, err = run(
                    runtime,
                    event,
                    matcher,
                    script,
                    {"cwd": str(project)},
                    project=project,
                )
                if script in gates:
                    check(
                        f"{runtime} {event} gate {script} exits 2 when the adapter crashes",
                        rc == 2,
                        f"rc={rc} {err}",
                    )
                else:
                    check(
                        f"{runtime} {event} advisory {script} exits 0 when the adapter crashes",
                        rc == 0,
                        f"rc={rc} {err}",
                    )
        finally:
            adapter.write_text(original)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
