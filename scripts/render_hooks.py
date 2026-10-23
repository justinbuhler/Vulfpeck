#!/usr/bin/env python3
"""Render the harness.toml [[hook]] table into each runtime's hook registration.

    python3 scripts/render_hooks.py            # write the three files
    python3 scripts/render_hooks.py --check    # exit 1 when any file is stale

Writes only the `hooks` key of .claude/settings.json and .gemini/settings.json
(every other key is kept) and the whole of .codex/hooks.json.

Claude Code runs each entry's command as written. Codex and Gemini run it
through scripts/hook_adapter.py, with the same failure direction: a gate's
registration ends in `|| { ...; exit 2; }`, an advisory's in `|| true`.

An entry a runtime cannot host is left out of that runtime's file and is listed
by `gaps()` (Gemini has no Agent tool matcher and no subagent events); the
harness doctor and the tests name them.
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

import harness_config
from hook_adapter import CODEX_EVENTS
from paths import REPO

CLAUDE_FILE = ".claude/settings.json"
CODEX_FILE = ".codex/hooks.json"
GEMINI_FILE = ".gemini/settings.json"

CODEX_MATCH = {
    "Agent": "spawn_agent",
    "Bash": "Bash",
    "Edit": "apply_patch",
    "Write": "apply_patch",
}
GEMINI_MATCH = {"Bash": "run_shell_command", "Edit": "replace", "Write": "write_file"}
GEMINI_EVENT = {
    "PreToolUse": "BeforeTool",
    "PostToolUse": "AfterTool",
    "UserPromptSubmit": "BeforeAgent",
    "Stop": "AfterAgent",
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
}
ADAPTER = 'python3 "$(git rev-parse --show-toplevel)/scripts/hook_adapter.py"'


def translate_matcher(event, matcher, table):
    """Return the runtime's matcher for a Claude matcher, or None when nothing maps.

    An empty matcher (every call) stays empty, and a lifecycle matcher such as
    SessionStart's `startup` is not a tool name, so it passes through. A tool set
    with some unmappable tools keeps the mappable ones.
    """
    if not matcher or event not in ("PreToolUse", "PostToolUse"):
        return matcher
    mapped = [table[t] for t in matcher.split("|") if t in table]
    return "|".join(dict.fromkeys(mapped)) or None


def watches_edits(entry):
    """Return whether the entry's Claude matcher names Edit or Write."""
    return bool({"Edit", "Write"} & set(entry.get("matcher", "").split("|")))


def adapted(entry, runtime, native_event):
    """Return the registered command that runs the entry's guard through the adapter.

    Under Codex an edit guard also gets `--edits` and a Bash matcher: a patch sent
    through the shell is reported as Bash, and the adapter turns it into edit events.
    """
    flag = " --edits" if runtime == "codex" and watches_edits(entry) else ""
    command = f"{ADAPTER} --timeout {entry['timeout']}{flag} --runtime {runtime} --event {native_event} -- {shlex.quote(entry['command'])}"
    if entry["kind"] == "gate":
        return (
            command
            + ' || { rc=$?; [ "$rc" = 2 ] || echo "hook_adapter crashed (rc=$rc); failing closed" >&2;'
            " exit 2; }"
        )
    return command + " || true"


def build(entries, runtime):
    """Return {native event: [matcher group, ...]} for one runtime."""
    hooks = {}
    for entry in entries:
        if runtime == "claude":
            event, matcher, command, timeout = (
                entry["event"],
                entry["matcher"],
                entry["command"],
                entry["timeout"],
            )
            hook = {"type": "command", "command": command, "timeout": timeout}
        elif runtime == "codex":
            if entry["event"] not in CODEX_EVENTS:
                continue
            event = entry["event"]
            matcher = translate_matcher(event, entry["matcher"], CODEX_MATCH)
            if matcher and watches_edits(entry) and event in ("PreToolUse", "PostToolUse"):
                matcher += "|Bash"
            hook = {
                "type": "command",
                "command": adapted(entry, "codex", event),
                "timeout": entry["timeout"],
            }
        else:
            if entry["event"] not in GEMINI_EVENT:
                continue
            event, matcher = (
                GEMINI_EVENT[entry["event"]],
                translate_matcher(entry["event"], entry["matcher"], GEMINI_MATCH),
            )
            hook = {
                "type": "command",
                "name": entry["name"],
                "command": adapted(entry, "gemini", event),
                "timeout": entry["timeout"] * 1000,
            }
        if matcher is None:
            continue
        groups = hooks.setdefault(event, [])
        group = next((g for g in groups if g.get("matcher", "") == matcher), None)
        if group is None:
            group = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
            groups.append(group)
        group["hooks"].append(hook)
    return hooks


def gaps(entries):
    """Return (entry name, reason) for every entry a runtime leaves out."""
    out = []
    for entry in entries:
        if entry["event"] not in CODEX_EVENTS:
            out.append((entry["name"], f"codex has no {entry['event']} event"))
        if entry["event"] not in GEMINI_EVENT:
            out.append((entry["name"], f"gemini has no {entry['event']} event"))
        elif translate_matcher(entry["event"], entry["matcher"], GEMINI_MATCH) is None:
            out.append(
                (entry["name"], f"gemini has no tool for matcher {entry['matcher']!r}")
            )
    return out


def rendered(root=REPO):
    """Return {relative path: text} of the three files as they should be on disk."""
    entries = harness_config.load(root)["hook"]
    files = {}
    for runtime, name in (("claude", CLAUDE_FILE), ("gemini", GEMINI_FILE)):
        path = Path(root) / name
        document = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        files[name] = (
            json.dumps(
                {**document, "hooks": build(entries, runtime)},
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    files[CODEX_FILE] = (
        json.dumps({"hooks": build(entries, "codex")}, indent=2, ensure_ascii=False)
        + "\n"
    )
    return files


def stale(root=REPO):
    """Return the relative paths whose content differs from the rendering."""
    root = Path(root)
    return [
        name
        for name, text in rendered(root).items()
        if not (root / name).is_file()
        or (root / name).read_text(encoding="utf-8") != text
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a file is stale; write nothing",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            old = stale()
            for name in old:
                print(
                    f"stale: {name}; run python3 scripts/render_hooks.py",
                    file=sys.stderr,
                )
            return 1 if old else 0
        for name, text in rendered().items():
            path = REPO / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                print(f"wrote {name}")
    except harness_config.ConfigError as exc:
        print(f"render_hooks: harness.toml: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
