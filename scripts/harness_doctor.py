#!/usr/bin/env python3
"""Report what this machine lacks for the harness, at session start.

A repository test can prove the harness is wired; it cannot see the machine it
will run on. This checks the prerequisites the controls assume and the
user-level Claude Code hooks that can undercut them, and says so once, at
SessionStart. It never blocks: it prints findings and exits 0.

Two kinds of user-level hook are named, because the harness cannot switch off a
hook it does not own:
  - SubagentStart hooks can inject the parent session's context into reviewers,
    which breaks "authoring and approval occur in fresh contexts" (CLAUDE.md).
  - PreToolUse Bash hooks can deny `git push` or `gh pr`, which the publish
    path needs.

Runs on Python 3.9+ so that it can report an interpreter too old for the rest.

    python3 scripts/harness_doctor.py            # findings only; silent when clean
    python3 scripts/harness_doctor.py --verbose  # also list what passed
"""
import json
import argparse
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path

from paths import REPO as ROOT


def git(*args):
    """Return git's stdout in the repository, or "" when git cannot answer."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def user_settings():
    """Return the user-level settings Claude Code reads, or {} when absent."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        return json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hook_commands(settings, event, matcher_has=None):
    """Return the commands registered for one event, optionally by matcher."""
    found = []
    for group in (settings.get("hooks") or {}).get(event) or []:
        matcher = group.get("matcher") or ""
        if matcher_has and matcher and matcher_has not in matcher.split("|") and matcher != "*":
            continue
        for hook in group.get("hooks") or []:
            if hook.get("command"):
                found.append(hook["command"])
    return found


def script_name(command):
    """Return the hook script a command runs, or the command's first word."""
    found = re.findall(r"[\w.-]+\.(?:py|sh|js)\b", command)
    return found[-1] if found else command.split()[0]


def native_probe(root):
    """Read effective Codex configuration and hook trust without starting a turn."""
    process = subprocess.Popen(["codex", "app-server", "--stdio"], cwd=root,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    selector, pending = selectors.DefaultSelector(), b""
    selector.register(process.stdout, selectors.EVENT_READ)

    def request(number, method, params):
        nonlocal pending
        process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == number:
                    if "error" in message:
                        raise ValueError(f"{method}: {message['error']}")
                    return message["result"]
            if selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("native config probe ended without a response")
                pending += chunk
        raise ValueError(f"{method} timed out; native activation NOT checked")

    try:
        request(1, "initialize", {"clientInfo": {"name": "vulfpeck-doctor", "version": "1"},
                                  "capabilities": {"experimentalApi": True}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        config = request(2, "config/read", {"cwd": str(root), "includeLayers": True})
        hooks = request(3, "hooks/list", {"cwds": [str(root)]})
        # Effective config can contain credentials; return activation metadata only.
        return {"layers": [{"name": layer.get("name"), "disabledReason": layer.get("disabledReason")}
                           for layer in config.get("layers") or []], "hooks": hooks}
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def codex_findings(report=None):
    """Compare declarations with native discovery, enabled state, and exact trust."""
    problems, passes = [], []
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for path in (home / "hooks.json", home / "config.toml"):
        if path.is_file():
            problems.append(f"Codex user configuration is additive: {path}; inspect effective hook sources")
    try:
        declared = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]
    except (OSError, ValueError, KeyError):
        return problems + ["Codex project hooks.json is absent or malformed"], passes
    if report is None:
        return problems + ["Codex effective config and exact hook trust NOT checked; run doctor --runtime codex --probe-native"], passes
    for layer in report.get("layers", []):
        if layer.get("disabledReason"):
            problems.append(f"Codex config layer disabled: {layer['disabledReason']}")
    entries = [hook for item in report.get("hooks", {}).get("data", []) for hook in item.get("hooks", [])]
    for item in report.get("hooks", {}).get("data", []):
        problems.extend(f"Codex hook discovery: {message}" for message in item.get("errors", []) + item.get("warnings", []))
    source = (ROOT / ".codex/hooks.json").resolve()
    for event, groups in declared.items():
        wire_event = event[0].lower() + event[1:]
        for group in groups:
            for hook in group["hooks"]:
                matches = [h for h in entries if h.get("eventName") == wire_event
                           and h.get("command") == hook["command"]
                           and h.get("matcher") == group.get("matcher")]
                own = [h for h in matches if Path(h.get("sourcePath", "")).resolve() == source]
                if not own:
                    problems.append(f"Codex {event} project hook not discovered from this checkout; linked worktrees can load primary declarations")
                elif not all(h.get("enabled") and h.get("trustStatus") == "trusted" and h.get("currentHash") for h in own):
                    problems.append(f"Codex {event} hook disabled or exact definition untrusted; registration is not activation")
                else:
                    passes.append(f"Codex {event} effective hook enabled and exact definition trusted")
    for hook in entries:
        if Path(hook.get("sourcePath", "")).resolve() == source:
            continue
        if hook.get("eventName") in ("subagentStart", "preToolUse"):
            problems.append(f"Codex additive {hook.get('source', 'unknown')} {hook['eventName']} hook may affect fresh review or tool policy: "
                            + script_name(hook.get("command") or "unknown"))
    return problems, passes


def findings(runtime="claude", report=None):
    """Return (problems, passes) as lists of one-line strings."""
    problems, passes = [], []

    def need(ok, good, bad):
        (passes if ok else problems).append(good if ok else bad)

    need(sys.version_info >= (3, 11), f"python3 {sys.version.split()[0]}",
         f"python3 is {sys.version.split()[0]}; hooks reading harness.toml need 3.11+")
    for tool in ("git", "uv", "rsync"):
        need(shutil.which(tool) is not None, f"{tool} found", f"{tool} is not on PATH")
    need(bool(shutil.which("flock") or shutil.which("lockf")), "flock/lockf found",
         "neither flock nor lockf is on PATH; scripts/run.sh cannot serialise the suite")
    hooks_path = git("config", "--get", "core.hooksPath")
    need(hooks_path == ".githooks", "core.hooksPath is .githooks",
         f"core.hooksPath is {hooks_path or 'unset'}; run: git config core.hooksPath .githooks")
    if (ROOT / "harness.toml").is_file() and sys.version_info >= (3, 11):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import harness_config
            harness_config.load(ROOT)
            passes.append("harness.toml reads cleanly")
        except Exception as exc:
            problems.append(f"harness.toml: {exc}")

    if runtime in ("claude", "both"):
        user = user_settings()
        for command in hook_commands(user, "SubagentStart"):
            problems.append("user-level SubagentStart hook may inject context into reviewers: "
                            + script_name(command))
        bash_hooks = hook_commands(user, "PreToolUse", matcher_has="Bash")
        if bash_hooks:
            names = sorted({script_name(c) for c in bash_hooks})
            problems.append(f"{len(bash_hooks)} user-level PreToolUse Bash hook(s) run here and "
                            f"can deny git push or gh pr: {', '.join(names)}")
        try:
            version = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            found = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
            if not found or tuple(map(int, found.groups())) < (2, 1, 295):
                problems.append("Claude onFailure:block requires 2.1.295+; installed version unsupported or unknown")
            else:
                passes.append("Claude supports onFailure:block; event execution still requires a live canary")
        except (OSError, subprocess.SubprocessError):
            problems.append("Claude unavailable; gate failure semantics NOT checked")
        try:
            settings = json.loads((ROOT / ".claude/settings.json").read_text())
            for event, groups in settings["hooks"].items():
                for group in groups:
                    for hook in group["hooks"]:
                        if "failing closed" in hook.get("command", "") and hook.get("onFailure") != "block":
                            problems.append(f"Claude {event} gate lacks onFailure:block")
        except (OSError, ValueError, KeyError):
            problems.append("Claude project settings absent or malformed")
        if (ROOT / ".claude/settings.local.json").is_file():
            problems.append("Claude project-local settings are additive; inspect /hooks and retained live canary before trusting activation")
    if runtime in ("codex", "both"):
        bad, good = codex_findings(report)
        problems += bad
        passes += good
        problems.append("Codex 0.163.0 outer hook failures/timeouts do not fail closed; inner explicit denies cannot enforce an untrusted or stalled adapter")
    return problems, passes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("claude", "codex", "both"), default="claude")
    parser.add_argument("--probe-native", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report, failed = None, None
    if args.probe_native:
        try:
            report = native_probe(ROOT)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            failed = f"Codex native probe unavailable: {exc}"
    problems, passes = findings(args.runtime, report)
    if failed:
        problems.append(failed)
    if args.json:
        print(json.dumps({"runtime": args.runtime, "problems": problems, "passes": passes,
                          "native_activation": report}, indent=2))
        return 0
    if args.verbose:
        for line in passes:
            print(f"  ok   {line}")
    if problems:
        print("harness doctor: " + str(len(problems)) + " finding(s)")
        for line in problems:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
