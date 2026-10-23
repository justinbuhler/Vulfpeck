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
    python3 scripts/harness_doctor.py --runtimes # also check the Codex and Gemini CLIs

`--runtimes` is not part of the SessionStart run: it starts each CLI for its
version, which is slow. For Claude Code, Codex and Gemini it reports whether the
CLI is installed, whether the rendered hook files are fresh, whether this
folder is trusted and whether credentials exist. Codex hook trust is per hook
and is granted inside Codex (`/hooks`); the doctor cannot see it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def cli_version(name):
    """Return the CLI's `--version` first line, or "" when it is absent or fails."""
    if not shutil.which(name):
        return ""
    try:
        done = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else ""


def codex_config(home):
    """Return Codex's config.toml path (CODEX_HOME wins, as in Codex)."""
    return Path(os.environ.get("CODEX_HOME") or Path(home) / ".codex") / "config.toml"


def codex_trusted(root, home):
    """Return whether Codex's config.toml marks `root` as a trusted project."""
    try:
        import tomllib
        config = tomllib.loads(codex_config(home).read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return False
    return (config.get("projects") or {}).get(str(root), {}).get("trust_level") == "trusted"


def gemini_trusted(root, home):
    """Return whether ~/.gemini/trustedFolders.json trusts `root`.

    Gemini matches by ancestor: the longest rule path that contains `root` decides
    (TRUST_PARENT trusts the rule's parent), compared case-insensitively on macOS.
    """
    try:
        rules = json.loads((home / ".gemini" / "trustedFolders.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    where = os.path.normcase(str(Path(root).resolve())).lower()
    best, level = -1, ""
    for rule, trust in rules.items():
        base = str(Path(rule).parent) if trust == "TRUST_PARENT" else rule
        base = base.lower().rstrip("/")
        if (where == base or where.startswith(base + "/")) and len(rule) > best:
            best, level = len(rule), trust
    return level in ("TRUST_FOLDER", "TRUST_PARENT")


def codex_main_hooks(root):
    """Return "" when Codex in `root` runs this tree's hooks, else "missing" or "stale".

    Codex reads `.codex/hooks.json` from the main checkout for every linked
    worktree, so a worktree's own file is never loaded (it loads none when main
    has none). A standalone checkout is its own main and always passes.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                              capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    common = Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
    if common is None or common.name != ".git" or common.parent.resolve() == Path(root).resolve():
        return ""
    theirs = common.parent / ".codex" / "hooks.json"
    if not theirs.is_file():
        return "missing"
    mine = Path(root) / ".codex" / "hooks.json"
    return "" if mine.is_file() and mine.read_bytes() == theirs.read_bytes() else "stale"


def codex_auth():
    """Return whether `codex login status` succeeds (no model call)."""
    try:
        return subprocess.run(["codex", "login", "status"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def gemini_auth(home):
    """Return whether Gemini credentials exist. Eligibility of the account is not checked."""
    return bool((home / ".gemini" / "oauth_creds.json").is_file()
                or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def runtime_findings(root=ROOT, home=None):
    """Return (problems, passes) for the Claude, Codex and Gemini CLIs."""
    home = Path(home) if home else Path.home()
    problems, passes = [], []

    def need(ok, good, bad):
        (passes if ok else problems).append(good if ok else bad)

    stale = None
    if sys.version_info >= (3, 11) and (root / "scripts" / "render_hooks.py").is_file():
        sys.path.insert(0, str(root / "scripts"))
        try:
            import render_hooks
            stale = render_hooks.stale(root)
        except Exception as exc:
            problems.append(f"render_hooks: {exc}")
    for cli, install in (("claude", "npm install -g @anthropic-ai/claude-code"),
                         ("codex", "brew install --cask codex"),
                         ("gemini", "brew install gemini-cli")):
        version = cli_version(cli)
        need(bool(version), f"{cli} {version}", f"{cli} is not installed or does not run; install: {install}")
    if stale is not None:
        need(not stale, "rendered hook files are fresh",
             f"stale hook files {stale}; run: python3 scripts/render_hooks.py")
    drift = codex_main_hooks(root)
    need(not drift, "codex hooks come from this tree",
         f"Codex in this worktree runs main's hooks: {drift} (main's .codex/hooks.json is not this tree's); "
         "a hook change is only exercised under Codex from a standalone clone or after it lands on main")
    need(codex_trusted(root, home), "codex trusts this folder",
         f"codex does not trust {root}; run: python3 scripts/harness_install.py --apply --trust")
    need(gemini_trusted(root, home), "gemini trusts this folder",
         f"gemini does not trust {root}; run: python3 scripts/harness_install.py --apply --trust")
    if shutil.which("codex"):
        need(codex_auth(), "codex is logged in", "codex is not logged in; run: codex login")
    need(gemini_auth(home), "gemini credentials found (account eligibility not checked)",
         "no Gemini credentials; run gemini and sign in, or set GEMINI_API_KEY")
    return problems, passes


def findings():
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

    if "--runtimes" in sys.argv:
        more_problems, more_passes = runtime_findings()
        problems += more_problems
        passes += more_passes

    user = user_settings()
    for command in hook_commands(user, "SubagentStart"):
        problems.append("user-level SubagentStart hook may inject context into reviewers: "
                        + script_name(command))
    bash_hooks = hook_commands(user, "PreToolUse", matcher_has="Bash")
    if bash_hooks:
        names = sorted({script_name(c) for c in bash_hooks})
        problems.append(f"{len(bash_hooks)} user-level PreToolUse Bash hook(s) run here and "
                        f"can deny git push or gh pr: {', '.join(names)}")
    return problems, passes


def main():
    problems, passes = findings()
    if "--verbose" in sys.argv:
        for line in passes:
            print(f"  ok   {line}")
    if problems:
        print("harness doctor: " + str(len(problems)) + " finding(s)")
        for line in problems:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
