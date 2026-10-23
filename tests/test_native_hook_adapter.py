"""Exercise native payloads through registered policy commands in disposable Git checkouts."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO as ROOT  # noqa: E402
import native_hook_adapter as H  # noqa: E402
import harness_doctor as D  # noqa: E402

ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
PASS, FAIL = [], []


def check(name, condition, detail=""):
    """Record one externally visible behavior."""
    (PASS if condition else FAIL).append(name)
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"\n{detail}" if detail and not condition else ""))


def denied(output):
    """Return whether a registered command emitted an explicit blocking decision."""
    value = json.loads(output) if output.strip() else {}
    return value.get("decision") == "block" or value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


registry = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]
expected = {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart",
            "SubagentStop", "Stop", "SessionEnd"}
check("Codex registers the native governance lifecycle", set(registry) == expected)
check("gate wrapper emits a stderr denial after adapter failure",
      all("exit 2" in registry[event][0]["hooks"][0]["command"] for event in ("PreToolUse", "PostToolUse")))

with tempfile.TemporaryDirectory() as raw:
    project = Path(raw).resolve()
    subprocess.run(["git", "init", "-q", "-b", "work", str(project)], env=ENV, check=True)
    (project / "scripts").mkdir()
    for name in ("paths.py", "harness_config.py", "harness_events.py", "adr.py", "native_hook_adapter.py"):
        shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
    shutil.copytree(ROOT / ".claude/hooks", project / ".claude/hooks")
    (project / "docs/adr").mkdir(parents=True)
    (project / "harness.toml").write_text(
        '[adr]\nrequire_for=["scripts/*.py"]\n'
        '[[mapped_tests]]\nmatch="*.txt"\ntests=["{dir}/check_{stem}.sh"]\ncommand="bash {test}"\n')
    (project / "plain.py").write_text("old\n")
    command = registry["PreToolUse"][0]["hooks"][0]["command"]

    def run(event, text=None):
        """Run the command exactly as registered, without a native model turn."""
        done = subprocess.run(["bash", "-c", command], cwd=project, env={**ENV, "HARNESS_EVENTS_LOG": os.devnull},
                              input=json.dumps(event) if text is None else text,
                              capture_output=True, text=True, timeout=15)
        return done.returncode, done.stdout, done.stderr

    def patch(text, event="PreToolUse"):
        """Build the canonical native patch event."""
        return {"hook_event_name": event, "tool_name": "apply_patch", "tool_input": {"command": text},
                "cwd": str(project), "session_id": "session-a", "tool_use_id": "patch-a"}

    allowed = "*** Begin Patch\n*** Add File: note.txt\n+hello\n*** End Patch"
    rc, out, err = run(patch(allowed))
    check("a native ungoverned addition passes", rc == 0 and not denied(out), out + err)
    multi = "*** Begin Patch\n*** Add File: note.txt\n+hello\n*** Add File: scripts/new.py\n+pass\n*** End Patch"
    rc, out, err = run(patch(multi))
    check("every file in a native patch reaches the ADR gate", rc == 0 and denied(out) and "scripts/new.py" in out, out + err)
    rename = "*** Begin Patch\n*** Update File: plain.py\n*** Move to: scripts/new.py\n@@\n-old\n+new\n*** End Patch"
    rc, out, err = run(patch(rename))
    check("a rename destination cannot bypass the ADR gate", rc == 0 and denied(out) and "scripts/new.py" in out, out + err)
    (project / "docs/adr/0001-new.md").write_text(
        "# 0001. New\n\nStatus: proposed\nDate: 2026-10-22\nComponents: scripts/new.py\n\n"
        "## Context\n\nNative adapter.\n\n## Decision\n\nName the component.\n\n## Consequences\n\nCheck both paths.\n")
    rc, out, err = run(patch(rename))
    check("an ADR-covered rename passes existing policy", rc == 0 and not denied(out), out + err)
    events = H.patch_events(patch(rename), project)
    check("rename expansion preserves old and new paths and stable call identity",
          [e["tool_input"]["file_path"] for e in events] == [str(project / "plain.py"), str(project / "scripts/new.py")]
          and all(e["session_id"] == "session-a" and e["tool_use_id"] == "patch-a" for e in events))
    bad_patches = {
        "missing end": "*** Begin Patch\n*** Add File: x\n+x",
        "unknown operation": "*** Begin Patch\n*** Copy File: x\n*** End Patch",
        "invalid body": "*** Begin Patch\n*** Update File: plain.py\nnot a hunk\n*** End Patch",
        "empty update": "*** Begin Patch\n*** Update File: plain.py\n*** End Patch",
        "traversal": "*** Begin Patch\n*** Add File: ../outside.py\n+x\n*** End Patch",
        "absolute escape": "*** Begin Patch\n*** Add File: /outside.py\n+x\n*** End Patch",
        "bad delete": "*** Begin Patch\n*** Delete File: plain.py\n+unexpected\n*** End Patch",
    }
    (project / "escape").symlink_to(project.parent, target_is_directory=True)
    bad_patches["symlink escape"] = "*** Begin Patch\n*** Add File: escape/outside.py\n+x\n*** End Patch"
    for name, text in bad_patches.items():
        rc, out, err = run(patch(text))
        check(f"native patch rejects {name}", rc == 0 and denied(out), out + err)
    for bad in ("not json", "[]", '{"hook_event_name":"PreToolUse","tool_name":"apply_patch","tool_input":null}'):
        rc, out, err = run({}, bad)
        check(f"malformed native event is explicitly denied: {bad}", rc == 0 and denied(out), out + err)

    for name, code in (("a", "exit 0\n"), ("b", "echo failed-b; exit 1\n")):
        (project / f"{name}.txt").write_text("x\n")
        (project / f"check_{name}.sh").write_text(code)
    post = "*** Begin Patch\n*** Update File: a.txt\n@@\n-x\n+y\n*** Update File: b.txt\n@@\n-x\n+y\n*** End Patch"
    rc, out, err = run(patch(post, "PostToolUse"))
    check("mapped tests run for every changed native file", rc == 0 and denied(out) and "failed-b" in out + err, out + err)

    shell = {"hook_event_name": "PreToolUse", "tool_name": "exec_command", "cwd": str(project.parent),
             "tool_input": {"cmd": "git status", "workdir": str(project)}, "session_id": "s", "tool_use_id": "c"}
    normalized = H.normalize(shell)[0]
    check("exec_command uses its actual workdir and preserves native IDs",
          normalized["cwd"] == str(project) and normalized["tool_name"] == "Bash"
          and normalized["tool_input"]["command"] == "git status" and normalized["tool_use_id"] == "c")
    for event in ("SubagentStart", "SubagentStop"):
        native = {"hook_event_name": event, "agent_id": "agent-stable", "agent_type": "reviewer", "session_id": "s"}
        normalized = H.normalize(native)[0]
        check(f"{event} preserves the native agent ID and applies read-only role policy",
              normalized["agent_id"] == "agent-stable" and normalized["agent_type"] == "reviewer-codex")
    spawn = {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent", "cwd": str(project),
             "tool_input": {"task_name": "reviewer"}}
    rc, out, err = run(spawn)
    check("a task name cannot impersonate native role selection", denied(out) and "agent_type" in out, out + err)
    spawn["tool_input"] = {"agent_type": "executor"}
    rc, out, err = run(spawn)
    check("approved native profile selection passes", rc == 0 and not denied(out), out + err)

    target = project / ".claude/hooks/adr_gate.py"
    original = target.read_text()
    for label, replacement in (("crash", "raise RuntimeError('canary crash')\n"),
                               ("malformed output", "print('{not-json')\n"), ("missing", None)):
        if replacement is None:
            target.unlink()
        else:
            target.write_text(replacement)
        rc, out, err = run(patch(allowed))
        check(f"registered native gate fails closed after {label}", rc == 0 and denied(out), out + err)
        target.write_text(original)
    assert hasattr(H, "REPO")
    previous = H.REPO
    H.REPO = project
    target.write_text("import time\ntime.sleep(2)\n")
    try:
        H.run_policy(".claude/hooks/adr_gate.py", [], patch(allowed), 0.02, True)
        timeout_denied = False
    except ValueError as exc:
        timeout_denied = "timed out" in str(exc)
    finally:
        H.REPO = previous
        target.write_text(original)
    check("inner native gate timeout becomes an explicit failure before the runtime timeout", timeout_denied)
    advisory = project / ".claude/hooks/blast_radius_advisory.py"
    advisory.write_text("raise RuntimeError('advisory crash')\n")
    rc, out, err = run(patch(allowed))
    check("native advisory crash continues without approving or denying the edit", rc == 0 and not denied(out), out + err)

    # Discovery fixtures vary native trust; the expected event inventory above is independent.
    hooks = []
    for event, groups in registry.items():
        for group in groups:
            for hook in group["hooks"]:
                hooks.append({"eventName": event[0].lower() + event[1:], "command": hook["command"],
                              "matcher": group.get("matcher"), "sourcePath": str(ROOT / ".codex/hooks.json"),
                              "enabled": True, "trustStatus": "trusted", "currentHash": "sha256:fixture"})
    report = {"layers": [], "hooks": {"data": [{"hooks": hooks, "errors": [], "warnings": []}]}}
    problems, passes = D.codex_findings(report)
    check("doctor distinguishes effective trusted hooks from declarations", any("effective hook enabled" in p for p in passes))
    hooks[0]["trustStatus"] = "untrusted"
    problems, _ = D.codex_findings(report)
    check("doctor detects revoked exact-definition trust", any("untrusted" in p for p in problems))
    hooks[0].update(trustStatus="trusted", enabled=False)
    problems, _ = D.codex_findings(report)
    check("doctor detects a disabled native gate", any("disabled or exact" in p for p in problems))
    hooks[0].update(enabled=True, sourcePath=str(project / ".codex/hooks.json"))
    problems, _ = D.codex_findings(report)
    check("doctor detects primary-checkout hook discovery instead of this checkout", any("not discovered from this checkout" in p for p in problems))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
