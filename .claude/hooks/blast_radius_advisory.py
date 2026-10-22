#!/usr/bin/env python3
"""Say what a shared-control edit can break, before the edit lands.

A change to a shared control is easy to call cheap before its blast radius is
measured, and the measurement is one command away. Repointing one constant in
paths.py looks like a one-line change and can leave path literals behind across
a dozen tracked files, found one failure at a time. Changing what
.githooks/pre-commit runs looks free, but agent_harness.py lands every queued
agent commit through that hook and sizes its timeout on it.

So this runs the measurement rather than trusting anyone to remember it. It is
advisory: it prints and exits 0, and never blocks an edit. The point is that the
number arrives before the claim, not that the edit is hard to make.

Scope is deliberately a handful of paths. A hook that fires on every edit is one nobody
reads, and these are the files whose reach is not visible from the edit itself.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Used when scripts/harness_config.py is absent or harness.toml is unreadable;
# otherwise harness.toml [hooks] watched_files / watched_dirs decide.
WATCHED_FILES = ("harness.toml", "scripts/paths.py", "scripts/harness_config.py",
                 "scripts/gate.sh", "scripts/land.sh", "scripts/run.sh",
                 ".claude/settings.json")
WATCHED_DIRS = (".githooks/",)
CODEGRAPH_TIMEOUT = 8
GIT_TIMEOUT = 3

# A path literal worth grepping: quoted, and either containing a separator or
# carrying a suffix. "sources" alone is noise; "data/sources" is not.
LITERAL = re.compile(r"""["']([^"'\n]{4,120})["']""")
CONSTANT = re.compile(r"\b([A-Z][A-Z_0-9]{3,})\b")


def watched_paths(root):
    """Return (files, dirs) this repository treats as shared controls.

    Advisory, so an unreadable config falls back to the built-in list rather
    than going quiet on the edits that matter most.
    """
    try:
        sys.path.insert(0, str(root / "scripts"))
        import harness_config
        hooks = harness_config.load(root)["hooks"]
        return tuple(hooks["watched_files"]), tuple(hooks["watched_dirs"])
    except Exception:
        return WATCHED_FILES, WATCHED_DIRS


def watched(rel, root=None):
    """Return whether one repository-relative path is a shared control.

    Args:
        rel (str): repository-relative path of the file being edited.
        root (Path | None): repository root whose harness.toml decides.

    Returns:
        (bool): True when an edit to it reaches past the file.
    """
    files, dirs = watched_paths(root) if root else (WATCHED_FILES, WATCHED_DIRS)
    return rel in files or rel.startswith(dirs)


def candidates(text):
    """Return the path literals and constant names a change touches.

    Args:
        text (str): the text being replaced, or the whole file for a Write.

    Returns:
        (tuple): (path literals, constant names), each ordered and deduplicated.
    """
    literals = [v for v in dict.fromkeys(LITERAL.findall(text))
                if "/" in v or re.search(r"\.\w{2,5}$", v)]
    constants = [c for c in dict.fromkeys(CONSTANT.findall(text))
                 if c not in ("TRUE", "FALSE", "NULL", "NONE")]
    return literals[:4], constants[:5]


def tracked_hits(value, root, exclude):
    """Return how many tracked files spell one literal, and which.

    git grep rather than rg on purpose: the question is which TRACKED files carry
    a second spelling, because those are the ones that go stale in a commit.

    Args:
        value (str): the literal to search for.
        root (Path): repository root.
        exclude (str): the file being edited, which does not count against itself.

    Returns:
        (tuple): (code and declaration paths, prose paths), excluding the edited file.
    """
    try:
        out = subprocess.run(["git", "grep", "-l", "-F", value, "--", "."],
                             cwd=root, capture_output=True, text=True,
                             timeout=GIT_TIMEOUT).stdout.split()
    except (subprocess.TimeoutExpired, OSError):
        return [], []
    hits = [f for f in out if f != exclude]
    # Prose and code go stale differently. A path named in .claude/review/ or an
    # AGENTS.md is a judgement somebody updates; a path in a .py, .json or .sh is
    # a second spelling that breaks a run. Reporting them in one number buried the
    # one that matters: paths.py showed 47 files, 31 of them audit notes.
    code = [f for f in hits if not f.endswith(".md")]
    prose = [f for f in hits if f.endswith(".md")]
    return code, prose


def blast_radius(names, root):
    """Return CodeGraph's caller and test summary for some symbols.

    Args:
        names (list): symbol names to ask about.
        root (Path): repository root.

    Returns:
        (list): the summary lines CodeGraph produced, empty when unavailable.
    """
    if not names or not (root / ".codegraph").is_dir() or (root / ".codegraph").is_symlink():
        return []
    sys.path.insert(0, str(root / "scripts"))
    from codegraph_freshness import evaluate, git_env, read_status
    status = read_status(root)
    if status is None or evaluate(status, root):
        return []
    try:
        out = subprocess.run(["codegraph", "explore", "--path", str(root), " ".join(names)],
                             cwd=root, env=git_env(), capture_output=True, text=True,
                             timeout=CODEGRAPH_TIMEOUT).stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []
    wanted = {n.lower() for n in names}
    kept = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-") or ("caller" not in line and "tests:" not in line):
            continue
        symbol = re.search(r"`([^`]+)`", stripped)
        if symbol and symbol.group(1).lower() in wanted:
            kept.append(stripped)
    return kept[:6]


def main():
    """Print the blast radius of a shared-control edit.

    Returns:
        (int): always 0. This advises; it never blocks.
    """
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path")
    if not raw:
        return 0
    root = Path(payload.get("cwd") or os.getcwd())
    try:
        rel = Path(raw).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return 0
    if not watched(rel, root):
        return 0

    changing = tool_input.get("old_string") or ""
    if not changing and Path(raw).is_file():
        changing = Path(raw).read_text(encoding="utf-8", errors="replace")[:4000]
    literals, constants = candidates(changing)
    # The control's own name first: who reaches this file is the question an edit
    # inside it cannot answer, and it is the one that was missed on .githooks/.
    literals = [rel, Path(rel).name] + [v for v in literals if v not in (rel, Path(rel).name)]

    lines = [f"BLAST RADIUS — {rel} is a shared control; measure before you call this cheap."]
    for value in literals:
        code, prose = tracked_hits(value, root, rel)
        if code:
            shown = ", ".join(code[:4]) + (f" +{len(code) - 4} more" if len(code) > 4 else "")
            tail = f"  (+{len(prose)} in prose)" if prose else ""
            lines.append(f"  {len(code):>3} code/decl file(s) also spell {value!r}: {shown}{tail}")
        elif prose:
            lines.append(f"  {len(prose):>3} prose file(s) mention {value!r}; no code spells it")
    for line in blast_radius(constants, root):
        lines.append(f"  {line}")

    lines.append("  Before claiming a cost: git grep -cF '<current value>' for every literal you move.")
    lines.append("  Start now, not after:  ./scripts/run.sh --tests   (a subset is not a result)")
    advisory = "\n".join(lines)
    # stderr on exit 0 reaches the transcript but not the model, which makes an
    # advisory nobody acts on. additionalContext is the channel that puts it in
    # front of the reader who is about to make the claim; systemMessage keeps it
    # visible to the human too. No permissionDecision: this must not approve an
    # edit that would otherwise have been asked about.
    # Emitted on both channels because which one a given Claude Code version
    # surfaces to the model is not something this hook can assume: stderr always
    # reaches the transcript, additionalContext is what puts it in front of the
    # reader about to make the claim. No permissionDecision - this must never
    # approve an edit that would otherwise have been asked about.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": advisory,
        },
        "systemMessage": advisory,
    }))
    print(advisory, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
