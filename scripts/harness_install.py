#!/usr/bin/env python3
"""Set this checkout up for Claude Code, Codex and Gemini, as a dry run unless told otherwise.

    python3 scripts/harness_install.py                 # print what --apply would do
    python3 scripts/harness_install.py --apply         # render hooks, set core.hooksPath, uv sync
    python3 scripts/harness_install.py --apply --trust # also trust this folder in Codex and Gemini

`--apply` runs, in order: scripts/render_hooks.py, `git config core.hooksPath
.githooks`, `uv sync`. `--trust` adds the one step that writes outside the
repository: a `[projects."<path>"] trust_level = "trusted"` table appended to
~/.codex/config.toml and a TRUST_FOLDER rule added to ~/.gemini/trustedFolders.json.
Nothing is written to either file without both flags. Codex also asks you to
trust each hook (per-hook hash, `/hooks` in Codex); that is not written here.

It never installs a CLI: a missing one is reported with the command to run, and
`python3 scripts/harness_doctor.py --runtimes` shows the result. It never touches
~/.claude.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import harness_doctor
from paths import REPO

INSTALL = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "brew install --cask codex",
    "gemini": "brew install gemini-cli",
}


def toml_key(path):
    """Return `path` as a quoted TOML key."""
    return json.dumps(str(path))


def trust_codex(root, home):
    """Append a trusted-project table for `root` to Codex's config; return what happened."""
    if harness_doctor.codex_trusted(root, home):
        return f"codex already trusts {root}"
    config = harness_doctor.codex_config(home)
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    if f"[projects.{toml_key(root)}]" in text:
        return f"{config} already has a [projects] table for {root}; left as it is"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        text
        + ("" if text.endswith("\n") or not text else "\n")
        + f'\n[projects.{toml_key(root)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    return f"added {root} to {config}"


def trust_gemini(root, home):
    """Add a TRUST_FOLDER rule for `root` to Gemini's trustedFolders.json; return what happened."""
    path = home / ".gemini" / "trustedFolders.json"
    if harness_doctor.gemini_trusted(root, home):
        return f"gemini already trusts {root}"
    try:
        rules = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); fix it, then re-run"
        ) from exc
    rules[str(root)] = "TRUST_FOLDER"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    return f"added {root} to {path}"


def run(argv, cwd):
    """Run a command in `cwd`; return a one-line result, raising on failure."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, env=env, check=False
    )
    if done.returncode:
        raise SystemExit(
            f"{' '.join(argv)} failed ({done.returncode}): {(done.stderr or done.stdout).strip()}"
        )
    return f"ran {' '.join(argv)}"


def plan(root, home, trust):
    """Return [(description, callable)] for the requested steps."""
    steps = [
        (
            "render the hook files (python3 scripts/render_hooks.py)",
            lambda: run([sys.executable, "scripts/render_hooks.py"], root),
        ),
        (
            "git config core.hooksPath .githooks",
            lambda: run(["git", "config", "core.hooksPath", ".githooks"], root),
        ),
        ("uv sync", lambda: run(["uv", "sync"], root)),
    ]
    if trust:
        steps += [
            (
                f"trust {root} in Codex (~/.codex/config.toml)",
                lambda: trust_codex(root, home),
            ),
            (
                f"trust {root} in Gemini (~/.gemini/trustedFolders.json)",
                lambda: trust_gemini(root, home),
            ),
        ]
    return steps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="do the steps; without it, only print them"
    )
    parser.add_argument(
        "--trust",
        action="store_true",
        help="include the Codex and Gemini folder-trust steps",
    )
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    parser.add_argument(
        "--home", type=Path, default=Path.home(), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    for cli, command in INSTALL.items():
        if not shutil.which(cli):
            print(f"{cli} is not installed; install it yourself: {command}")
    if not args.trust:
        print(
            "folder trust for Codex and Gemini is not requested; add --trust to include it"
        )
    for description, step in plan(args.root, args.home, args.trust):
        if not args.apply:
            print(f"would: {description}")
            continue
        print(f"{description}: {step()}")
    if not args.apply:
        print("dry run; nothing was changed. Re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
