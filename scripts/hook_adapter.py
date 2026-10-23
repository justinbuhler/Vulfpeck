#!/usr/bin/env python3
"""Run a Claude Code hook guard under Codex CLI or Gemini CLI.

Every guard in .claude/hooks/ speaks Claude Code's payload and output schema.
This translates at the process boundary, so a guard is written once:

    python3 scripts/hook_adapter.py --runtime {codex|gemini} --event <native event> -- <guard command>

It reads the native payload on stdin, builds the Claude-shaped payload (one per
file for a Codex `apply_patch`), runs the guard command through `bash -c` with
CLAUDE_PROJECT_DIR set, and writes the native answer on stdout.

Failure direction (HARNESS.md "Failure direction"): the adapter exits non-zero
when it cannot translate the payload, the guard exits other than 0 or 2, or the
guard's answer has no form in the target runtime (OutputNormalizationError, for
example a deny on an event that cannot block). The registered command turns that
exit into a block for a gate (`|| { ...; exit 2; }`) and ignores it for an
advisory (`|| true`), exactly as under Claude Code.

Event map (native -> internal):
    Codex   SessionStart SessionEnd PreToolUse PostToolUse UserPromptSubmit Stop SubagentStart SubagentStop
    Gemini  SessionStart SessionEnd BeforeTool->PreToolUse AfterTool->PostToolUse
            BeforeAgent->UserPromptSubmit AfterAgent->Stop
Gemini has no subagent events, so SubagentStart and SubagentStop never reach it.

The apply_patch parser is adapted from vexjoy-agent's codex-hook-adapter.py.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from paths import REPO

CODEX_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)
GEMINI_EVENTS = {
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "BeforeTool": "PreToolUse",
    "AfterTool": "PostToolUse",
    "BeforeAgent": "UserPromptSubmit",
    "AfterAgent": "Stop",
}
GEMINI_NATIVE = {internal: native for native, internal in GEMINI_EVENTS.items()}
GEMINI_TOOLS = {"run_shell_command": "Bash", "write_file": "Write", "replace": "Edit"}
# Codex names the shell tool differently across surfaces; the guards know only "Bash".
CODEX_TOOLS = {
    "exec_command": "Bash",
    "shell_command": "Bash",
    "local_shell": "Bash",
    "spawn_agent": "Agent",
}


class AdapterError(RuntimeError):
    """The payload or the guard's result cannot be handled safely."""


class OutputNormalizationError(AdapterError):
    """The guard's result has no form the target runtime can act on."""


@dataclass
class Result:
    """One guard answer in neutral form."""

    deny: str | None = None
    context: list = field(default_factory=list)
    system: list = field(default_factory=list)


# --- apply_patch ---------------------------------------------------------------
def _path_after(line, marker):
    path = line[len(marker) :].strip()
    if not path or any(c in path for c in "\x00\r\n"):
        raise AdapterError(f"bad path after {marker.strip()}")
    return path


def _hunk_strings(lines):
    old, new = [], []
    for line in lines:
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            new.append(line[1:])
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith(" "):
            old.append(line[1:])
            new.append(line[1:])
        else:
            raise AdapterError(f"invalid update line: {line!r}")
    return "\n".join(old) + ("\n" if old else ""), "\n".join(new) + (
        "\n" if new else ""
    )


def parse_apply_patch(command):
    """Return one (tool_name, tool_input) per file operation in an apply_patch command."""
    if not isinstance(command, str):
        raise AdapterError("apply_patch command is not a string")
    lines = command.splitlines()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise AdapterError("apply_patch command lacks complete Begin/End markers")
    headers = ("*** Add File:", "*** Update File:", "*** Delete File:")
    edits, i = [], 1
    while i < len(lines) - 1:
        header = lines[i]
        marker = next((m for m in headers if header.startswith(m)), None)
        if marker is None:
            raise AdapterError(f"unknown patch operation: {header!r}")
        path = _path_after(header, marker)
        i += 1
        body = []
        while i < len(lines) - 1 and not lines[i].startswith(headers):
            body.append(lines[i])
            i += 1
        if marker == "*** Add File:":
            if any(not line.startswith("+") for line in body):
                raise AdapterError(f"add of {path!r} has a non-add line")
            content = "".join(line[1:] + "\n" for line in body)
            edits.append(("Write", {"file_path": path, "content": content}))
        elif marker == "*** Delete File:":
            if body:
                raise AdapterError(f"delete of {path!r} has unexpected content")
            edits.append(
                ("Edit", {"file_path": path, "old_string": "", "new_string": ""})
            )
        else:
            moves = [line for line in body if line.startswith("*** Move to:")]
            if len(moves) > 1:
                raise AdapterError(f"update of {path!r} has several move targets")
            hunk = [
                line
                for line in body
                if not line.startswith("*** Move to:") and line != "*** End of File"
            ]
            if any(line.startswith("*** ") for line in hunk):
                raise AdapterError(f"update of {path!r} has an unknown marker")
            if not hunk and not moves:
                raise AdapterError(f"update of {path!r} has no changes")
            old, new = _hunk_strings(hunk)
            edits.append(
                ("Edit", {"file_path": path, "old_string": old, "new_string": new})
            )
            if moves:
                edits.append(
                    (
                        "Write",
                        {
                            "file_path": _path_after(moves[0], "*** Move to:"),
                            "content": new,
                        },
                    )
                )
    if not edits:
        raise AdapterError("apply_patch command has no file operations")
    return edits


# --- native payload -> Claude payload ------------------------------------------
def _absolute(tool_input, cwd):
    path = tool_input.get("file_path")
    if isinstance(path, str) and path and not os.path.isabs(path):
        tool_input["file_path"] = str(Path(cwd) / path)
    return tool_input


HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2(?!<)")
PATCH_BLOCK = re.compile(r"\*\*\* Begin Patch.*?\*\*\* End Patch", re.S)
SHELLS = ("bash", "sh", "zsh", "dash")


def quoted_flags(text):
    """Return (flags, open): a boolean per character, True inside quotes, and whether a quote is left open."""
    flags, quote, escaped = [], "", False
    for ch in text:
        flags.append(bool(quote))
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            flags[-1] = True
    return flags, bool(quote)


def split_simple(line):
    """Split one logical line into simple-command segments at unquoted ; | & ( )."""
    (flags, _), out, last = quoted_flags(line), [], 0
    for i, ch in enumerate(line):
        if ch in ";|&()" and not flags[i]:
            out.append(line[last:i])
            last = i + 1
    out.append(line[last:])
    return out


def patch_blocks(text):
    """Return every Begin..End block in `text`; a Begin with no End raises."""
    if text.count("*** Begin Patch") != len(PATCH_BLOCK.findall(text)):
        raise AdapterError("an apply_patch command has a Begin Patch marker with no End Patch marker")
    return PATCH_BLOCK.findall(text)


def scan_script(script):
    """Return the patch blocks of every apply_patch invocation in a shell script.

    apply_patch counts only as the command word of a simple command (start of a line,
    or after && ; | ( ), also inside `bash -c`. Its patch is its heredoc body or an
    argument. Text elsewhere (echo, rg, cat > f <<EOF, a commit message) is not a
    patch. `apply_patch < file` and `cat p | apply_patch` carry no patch here and are
    left alone. An invocation that carries a heredoc or argument with no valid patch raises.
    """
    lines, blocks, i = script.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        while quoted_flags(line)[1] and i < len(lines):  # a quoted argument spans lines
            line += "\n" + lines[i]
            i += 1
        flags = quoted_flags(line)[0]
        bodies = []
        for match in HEREDOC.finditer(line):
            if flags[match.start()]:
                continue
            body = []
            while i < len(lines) and (lines[i].strip() if match.group(1) else lines[i]) != match.group(3):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append("\n".join(body))
        used = 0
        for segment in split_simple(line):
            seg_flags = quoted_flags(segment)[0]
            mine = [m for m in HEREDOC.finditer(segment) if not seg_flags[m.start()]]
            texts, used = bodies[used:used + len(mine)], used + len(mine)
            try:
                words = shlex.split(segment, comments=True)
            except ValueError:
                continue
            while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
                words.pop(0)
            if not words:
                continue
            command = os.path.basename(words[0])
            if command in ("apply_patch", "applypatch"):
                texts = texts + [w for w in words[1:] if "*** Begin Patch" in w]
                for text in texts:
                    found = patch_blocks(text)
                    if not found:
                        raise AdapterError("an apply_patch command carries no valid patch")
                    blocks += found
            elif command in SHELLS:
                for k, word in enumerate(words[1:-1], 1):
                    if re.fullmatch(r"-[A-Za-z]*c", word):
                        blocks += scan_script(words[k + 1])
                        break
    return blocks


def shell_patches(command):
    """Return the patch blocks a shell command applies through apply_patch ([] when none).

    Codex reports `apply_patch <<'EOF' ... EOF` sent through the shell as a Bash call
    (observed on 0.164), not as an apply_patch call.
    """
    return scan_script(command) if isinstance(command, str) else []


def to_claude(runtime, event, native, edits=False):
    """Return the list of Claude-shaped payloads for one native payload.

    `edits` marks a guard registered for file edits: a Codex Bash call that carries a
    patch becomes one Edit or Write event per file, and any other Bash call yields none.
    """
    if not isinstance(native, dict):
        raise AdapterError("payload is not a JSON object")
    payload = dict(native)
    payload["hook_event_name"] = event
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else str(REPO)
    tool_input = (
        payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    )
    name = payload.get("tool_name")
    if runtime == "gemini":
        if name is not None:
            payload["tool_name"] = GEMINI_TOOLS.get(name, name)
        response = payload.get("tool_response")
        if isinstance(response, dict):
            text = response.get("llmContent")
            payload["tool_response"] = {
                "stdout": text if isinstance(text, str) else "",
                "stderr": str((response.get("error") or {}).get("message", ""))
                if isinstance(response.get("error"), dict)
                else str(response.get("error") or ""),
                **response,
            }
        if event == "Stop":
            payload["last_assistant_message"] = payload.get("prompt_response", "")
    elif runtime == "codex" and name is not None:
        if name == "spawn_agent":
            tool_input = {
                **tool_input,
                "subagent_type": tool_input.get("agent_type")
                or tool_input.get("subagent_type"),
            }
            payload["tool_input"] = {
                k: v for k, v in tool_input.items() if v is not None
            }
        if name == "apply_patch":
            if event not in ("PreToolUse", "PostToolUse"):
                raise AdapterError(f"apply_patch on {event} is not supported")
            out = []
            for tool, edit_input in parse_apply_patch(tool_input.get("command")):
                child = dict(payload)
                child["tool_name"] = tool
                child["tool_input"] = _absolute(edit_input, cwd)
                out.append(child)
            return out
        payload["tool_name"] = CODEX_TOOLS.get(name, name)
        if edits and payload["tool_name"] == "Bash":
            out = []
            for patch in shell_patches(tool_input.get("command")):
                for tool, edit_input in parse_apply_patch(patch):
                    child = dict(payload)
                    child["tool_name"] = tool
                    child["tool_input"] = _absolute(edit_input, cwd)
                    out.append(child)
            return out
    if "tool_input" in payload or name is not None:
        payload["tool_input"] = _absolute(dict(payload.get("tool_input") or {}), cwd)
    return [payload]


# --- guard -> neutral result -----------------------------------------------------
def _json_tail(text):
    """Return (text before, parsed dict) for stdout ending in a JSON object, else (text, None)."""
    try:
        parsed = json.loads(text)
        return "", parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        try:
            parsed = json.loads(lines[i])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return "\n".join(lines[:i]).strip(), parsed
    return text, None


def interpret(returncode, stdout, stderr):
    """Return a Result for one guard run, or raise AdapterError."""
    stdout, stderr = stdout.strip(), stderr.strip()
    if returncode == 2:
        return Result(deny=stderr or stdout or "Hook exited with status 2.")
    if returncode != 0:
        raise AdapterError(
            f"guard exited {returncode}: {stderr or stdout or 'no diagnostic'}"
        )
    result = Result()
    if not stdout:
        return result
    before, data = _json_tail(stdout)
    if before:
        result.context.append(before)
    if data is None:
        return result
    inner = (
        data.get("hookSpecificOutput")
        if isinstance(data.get("hookSpecificOutput"), dict)
        else {}
    )
    if (
        data.get("continue") is False
        or "updatedInput" in inner
        or inner.get("permissionDecision") == "ask"
    ):
        raise OutputNormalizationError(
            "guard asked for continue=false, updatedInput or ask; no hook form carries it"
        )
    if inner.get("permissionDecision") == "deny":
        result.deny = str(
            inner.get("permissionDecisionReason") or "Hook denied this event."
        )
    elif data.get("decision") in ("block", "deny"):
        result.deny = str(data.get("reason") or "Hook blocked this event.")
    for text in (data.get("additionalContext"), inner.get("additionalContext")):
        if isinstance(text, str) and text:
            result.context.append(text)
    if isinstance(data.get("systemMessage"), str) and data["systemMessage"]:
        result.system.append(data["systemMessage"])
    return result


def merge(results):
    """Combine per-file results: any deny wins, reasons and messages are kept in order."""
    merged = Result()
    denies = [r.deny for r in results if r.deny is not None]
    merged.deny = "\n".join(dict.fromkeys(denies)) if denies else None
    for r in results:
        merged.context += [c for c in r.context if c not in merged.context]
        merged.system += [s for s in r.system if s not in merged.system]
    return merged


# --- neutral result -> native output ---------------------------------------------
CODEX_CONTEXT = (
    "SessionStart",
    "SubagentStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PreToolUse",
)
CODEX_BLOCK = ("PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop")
GEMINI_CONTEXT = ("SessionStart", "UserPromptSubmit", "PostToolUse")
GEMINI_BLOCK = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")


def emit(runtime, event, result):
    """Return the native JSON object for a Result, or raise OutputNormalizationError."""
    out, system = {}, list(result.system)
    native = GEMINI_NATIVE[event] if runtime == "gemini" else event
    inner = {}
    if result.deny is not None:
        if runtime == "codex" and event == "PreToolUse":
            inner.update(
                permissionDecision="deny", permissionDecisionReason=result.deny
            )
        elif (runtime == "codex" and event in CODEX_BLOCK) or (
            runtime == "gemini" and event in GEMINI_BLOCK
        ):
            out.update(
                decision="block" if runtime == "codex" else "deny", reason=result.deny
            )
        else:
            raise OutputNormalizationError(f"{runtime} cannot block on {native}")
    if result.context:
        text = "\n".join(result.context)
        if event in (CODEX_CONTEXT if runtime == "codex" else GEMINI_CONTEXT):
            inner["additionalContext"] = text
        else:
            system.append(text)
    if inner:
        out["hookSpecificOutput"] = {"hookEventName": native, **inner}
    if system:
        out["systemMessage"] = "\n".join(system)
    return out


# --- run -------------------------------------------------------------------------
# Documented runtime limits that are shorter than the registered timeout (Codex clamps
# SessionEnd to 3s), so the adapter answers inside what the runtime will really allow.
RUNTIME_LIMITS = {("codex", "SessionEnd"): 3.0}


def effective_timeout(runtime, event, timeout):
    """Return the registered timeout, capped at the runtime's own limit for that event."""
    cap = RUNTIME_LIMITS.get((runtime, event))
    return timeout if timeout is None or cap is None else min(timeout, cap)


def budget(timeout):
    """Return the seconds the adapter may use of a registered timeout: about 1s under it.

    A runtime that kills a hook at its timeout lets the call proceed, so a gate must
    answer first; None (no --timeout) means no deadline.
    """
    return None if timeout is None else max(timeout - 1.0, timeout / 2)


def read_stdin(deadline):
    """Read stdin to EOF within `deadline` seconds (None: no limit) or raise AdapterError."""
    if deadline is None:
        return sys.stdin.read()
    box = []
    reader = threading.Thread(target=lambda: box.append(sys.stdin.read()), daemon=True)
    reader.start()
    reader.join(deadline)
    if reader.is_alive():
        raise AdapterError(f"stdin was not closed within {deadline:g}s")
    return box[0]


CHILDREN = []


def kill_children(signum=None, frame=None):
    """Kill every guard process group still running; as a signal handler, then exit non-zero.

    The guard has its own session, so the runtime killing the adapter would not reach
    it. Exit status 128+signal is non-zero: a gate's registration turns it into a block.
    """
    for proc in CHILDREN:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    if signum is not None:
        sys.exit(128 + signum)


def run_guard(command, payload, deadline=None):
    """Run the guard command on one Claude-shaped payload; return a Result.

    `deadline` is a time.monotonic() instant; past it the guard's process group is
    killed and AdapterError is raised.
    """
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and os.path.isdir(payload["cwd"]) else str(REPO)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(REPO)}
    proc = subprocess.Popen(["bash", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cwd, env=env, start_new_session=True)
    CHILDREN.append(proc)
    try:
        stdout, stderr = proc.communicate(json.dumps(payload),
                                          timeout=None if deadline is None else max(deadline - time.monotonic(), 0.01))
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise AdapterError("guard did not answer before the hook timeout") from exc
    if stderr and proc.returncode == 0:
        print(stderr, file=sys.stderr, end="")
    return interpret(proc.returncode, stdout, stderr)


def handle(runtime, native_event, raw, command, deadline=None, edits=False):
    """Translate, run the guard and return the native output object."""
    if runtime == "gemini":
        if native_event not in GEMINI_EVENTS:
            raise AdapterError(f"Gemini event {native_event!r} is not mapped")
        event = GEMINI_EVENTS[native_event]
    elif native_event in CODEX_EVENTS:
        event = native_event
    else:
        raise AdapterError(f"Codex event {native_event!r} is not mapped")
    try:
        native = json.loads(raw)
    except ValueError as exc:
        raise AdapterError(f"payload is not JSON: {exc}") from exc
    result = merge([run_guard(command, p, deadline) for p in to_claude(runtime, event, native, edits)])
    return emit(runtime, event, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", choices=("codex", "gemini"), required=True)
    parser.add_argument("--event", required=True, help="the runtime's own event name")
    parser.add_argument("--edits", action="store_true", help="the guard watches file edits (see to_claude)")
    parser.add_argument("--timeout", type=float, default=None, help="the registered hook timeout, in seconds")
    parser.add_argument("guard", nargs=argparse.REMAINDER, help="-- <guard command>")
    args = parser.parse_args(argv)
    guard = args.guard[1:] if args.guard[:1] == ["--"] else args.guard
    if not guard:
        print("hook_adapter: no guard command after --", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, kill_children)
    signal.signal(signal.SIGINT, kill_children)
    started = time.monotonic()
    limit = budget(effective_timeout(args.runtime, args.event, args.timeout))
    try:
        raw = read_stdin(limit)
        deadline = None if limit is None else started + limit
        output = handle(args.runtime, args.event, raw, " ".join(guard), deadline, args.edits)
    except AdapterError as exc:
        print(f"hook_adapter: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
