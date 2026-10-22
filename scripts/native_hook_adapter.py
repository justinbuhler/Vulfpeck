#!/usr/bin/env python3
"""Translate native Codex events into the existing repository hook contracts."""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from paths import REPO

ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ROLES = {"reviewer": "reviewer-codex", "planner": "Plan", "explorer": "Explore"}


def git_root(cwd):
    """Return the checkout containing the execution directory."""
    done = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, env=ENV, timeout=5)
    if done.returncode:
        raise ValueError("execution directory is not a Git checkout")
    return Path(done.stdout.strip()).resolve()


def patch_events(payload, root):
    """Expand each patch operation, including both sides of a rename."""
    raw = payload["tool_input"]
    patch = raw if isinstance(raw, str) else raw.get("command", raw.get("patch"))
    if not isinstance(patch, str):
        raise ValueError("apply_patch requires a patch string in tool_input.command")
    lines = patch.strip().splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("malformed apply_patch boundaries")
    result, i = [], 1

    def path_of(name):
        path = Path(name)
        if (not name or name != name.strip() or "\x00" in name or "\\" in name
                or ".." in path.parts or name.startswith("~")):
            raise ValueError(f"unsupported patch path: {name!r}")
        target = (Path(payload["cwd"]) / path).resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError(f"patch path escapes the checkout: {name!r}")
        return str(target)

    while i < len(lines) - 1:
        match = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", lines[i])
        if not match:
            raise ValueError(f"malformed patch file header at line {i + 1}")
        kind, name = match.groups()
        source, destination = path_of(name), None
        i += 1
        if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
            if kind != "Update":
                raise ValueError("only Update File can rename a file")
            destination = path_of(lines[i][13:])
            i += 1
        body = []
        while i < len(lines) - 1 and not re.match(r"\*\*\* (?:Add|Update|Delete) File:", lines[i]):
            body.append(lines[i])
            i += 1
        if kind == "Delete" and body:
            raise ValueError("Delete File must not contain a hunk")
        if kind == "Add" and (not body or any(not line.startswith("+") for line in body)):
            raise ValueError("Add File requires added lines")
        if kind == "Update":
            if not body and not destination:
                raise ValueError("Update File requires a hunk or rename")
            if body and not any(line == "" or line.startswith((" ", "+", "-")) for line in body):
                raise ValueError("Update File contains an empty hunk")
            for n, line in enumerate(body):
                if line == "*** End of File" and n == len(body) - 1:
                    continue
                if line == "" or line.startswith((" ", "+", "-", "@@ ")) or line == "@@":
                    continue
                raise ValueError("malformed update hunk")
        changing = "\n".join(line[1:] for line in body if line.startswith(("-", " ")))
        for target, tool in [(source, "Write" if kind == "Add" else "Edit")] + (
                [(destination, "Write")] if destination else []):
            result.append({**payload, "tool_name": tool,
                           "tool_input": {"file_path": target, "old_string": changing},
                           "native_operation": kind.lower(), "native_tool_name": "apply_patch"})
    return result


def normalize(payload):
    """Preserve native identity and use the tool's actual execution directory."""
    if not isinstance(payload, dict) or not isinstance(payload.get("hook_event_name"), str):
        raise ValueError("hook input requires an event object")
    event = dict(payload)
    kind, name = event["hook_event_name"], event.get("tool_name")
    cwd = Path(event.get("cwd") or os.getcwd()).resolve()
    if kind in ("PreToolUse", "PostToolUse"):
        args = event.get("tool_input")
        if name == "apply_patch":
            if not isinstance(args, (str, dict)):
                raise ValueError("apply_patch input must be a string or object")
        elif not isinstance(args, dict):
            raise ValueError("tool_input must be an object")
        if isinstance(args, dict) and args.get("workdir"):
            cwd = (cwd / args["workdir"]).resolve()
        if name in ("exec_command", "shell_command", "Bash"):
            command = args.get("cmd", args.get("command"))
            if not isinstance(command, str):
                raise ValueError("shell input requires command or cmd")
            event.update(tool_name="Bash", tool_input={**args, "command": command})
    event["cwd"] = str(cwd)
    if kind in ("SubagentStart", "SubagentStop"):
        if not all(isinstance(event.get(key), str) and event[key]
                   for key in ("agent_id", "agent_type", "session_id")):
            raise ValueError("native lifecycle requires stable agent_id, agent_type and session_id")
        event["native_agent_type"] = event["agent_type"]
        event["agent_type"] = ROLES.get(event["agent_type"], event["agent_type"])
    if name == "apply_patch":
        root = git_root(cwd)
        common = lambda p: subprocess.check_output(
            ["git", "-C", str(p), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, env=ENV, timeout=5).strip()
        if common(root) != common(REPO):
            raise ValueError("patch checkout is outside the registered project")
        events = patch_events(event, root)
        for item in events:
            item["cwd"] = str(root)
        return events
    return [event]


def policies(event):
    """Select existing policy scripts without changing their policy decisions."""
    kind, name = event["hook_event_name"], event.get("tool_name")
    if kind == "PreToolUse":
        if name == "spawn_agent":
            return [(".claude/hooks/model_router.py", ["--runtime", "codex"], 5, True)]
        if name in ("Edit", "Write"):
            return [(".claude/hooks/blast_radius_advisory.py", [], 10, False),
                    (".claude/hooks/adr_gate.py", [], 10, True)]
        if name == "Bash":
            return [(".claude/hooks/queue_guard.py", [], 5, False),
                    (".claude/hooks/verify_commit_landed.py", [], 10, True)]
    if kind == "PostToolUse":
        if name in ("Edit", "Write"):
            return [(".claude/hooks/test_for_edited_module.py", [], 60, True),
                    (".claude/hooks/adr_gate.py", [], 10, True)]
        if name == "Bash":
            return [(".claude/hooks/verify_commit_landed.py", [], 10, True)]
    if kind in ("SubagentStart", "SubagentStop"):
        return [(".claude/hooks/readonly_guard.py", [], 30, False)]
    if kind in ("Stop", "SessionEnd"):
        return [(".claude/hooks/session_guard.py", [], 50 if kind == "Stop" else 1, False)]
    if kind == "UserPromptSubmit":
        return [("scripts/codegraph_freshness.py", ["--hook"], 15, False)]
    if kind == "SessionStart":
        return [("scripts/codegraph_freshness.py", ["--sync-if-stale", "--hook"], 45, False),
                ("scripts/harness_doctor.py", ["--runtime", "codex"], 10, False)]
    return []


def run_policy(path, args, event, timeout, gate):
    """Convert a gate crash, timeout, or malformed result into an explicit failure."""
    try:
        process = subprocess.Popen([sys.executable, str(REPO / path), *args],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, cwd=event["cwd"], start_new_session=True,
                                   env={**ENV, "CLAUDE_PROJECT_DIR": str(REPO), "HARNESS_RUNTIME": "codex"})
        try:
            stdout, stderr = process.communicate(json.dumps(event), timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ValueError(f"{path} timed out after {timeout:g}s") from None
        done = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if done.stderr:
            print(done.stderr, file=sys.stderr, end="")
        if done.returncode:
            raise ValueError(f"{path} exited {done.returncode}: {done.stderr.strip()}")
        if gate and path.endswith("test_for_edited_module.py") and "not run:" in done.stderr:
            raise ValueError(f"{path}: mapped test was not run")
        output = done.stdout.strip()
        if not output:
            return {}
        if not output.startswith("{") and path.endswith(("test_for_edited_module.py", "harness_doctor.py",
                                                        "codegraph_freshness.py")):
            return {"systemMessage": output}
        result = json.loads(output)
        if not isinstance(result, dict) or set(result) - {
                "hookSpecificOutput", "decision", "reason", "systemMessage", "continue", "stopReason"}:
            raise ValueError(f"{path} returned malformed hook output")
        specific = result.get("hookSpecificOutput", {})
        if (not isinstance(specific, dict) or specific.get("permissionDecision") not in
                (None, "allow", "deny", "ask") or result.get("decision") not in (None, "block")
                or specific.get("hookEventName", event["hook_event_name"]) != event["hook_event_name"]
                or (specific.get("permissionDecision") == "deny" and not specific.get("permissionDecisionReason"))):
            raise ValueError(f"{path} returned malformed decision")
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        message = f"native hook {path}: {exc}"
        if gate:
            raise ValueError(message) from exc
        print(message + "; advisory continues", file=sys.stderr)
        return {}


def emit(kind, reasons, context):
    """Emit one native decision after all applicable file policies ran."""
    result = {}
    if reasons:
        reason = "\n".join(dict.fromkeys(reasons))
        print(reason, file=sys.stderr)
        if kind == "PreToolUse":
            result["hookSpecificOutput"] = {"hookEventName": kind, "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}
        else:
            result.update(decision="block", reason=reason)
    if context:
        text = "\n".join(dict.fromkeys(context))
        result["systemMessage"] = text
        if kind in ("PreToolUse", "PostToolUse", "SessionStart", "SubagentStart", "UserPromptSubmit"):
            result.setdefault("hookSpecificOutput", {"hookEventName": kind})["additionalContext"] = text
    if result:
        print(json.dumps(result))


def main():
    """Run the registered event with an inner deadline before the runtime timeout."""
    payload, reasons, context = {}, [], []
    try:
        payload = json.load(sys.stdin)
        events = normalize(payload)
        deadline = time.monotonic() + (1.5 if payload["hook_event_name"] == "SessionEnd" else 65)
        for event in events:
            for path, args, timeout, gate in policies(event):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if gate:
                        raise ValueError("native policy deadline exceeded; failing closed")
                    continue
                result = run_policy(path, args, event, min(timeout, remaining), gate)
                specific = result.get("hookSpecificOutput", {})
                if specific.get("permissionDecision") == "deny" or result.get("decision") == "block":
                    reasons.append(specific.get("permissionDecisionReason") or result.get("reason") or path)
                context.extend(str(v) for v in (result.get("systemMessage"), specific.get("additionalContext")) if v)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        reasons.append(f"Native policy unavailable: {exc}; failing closed")
    kind = payload.get("hook_event_name", "PreToolUse") if isinstance(payload, dict) else "PreToolUse"
    emit(kind, reasons, context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
