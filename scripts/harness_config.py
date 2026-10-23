#!/usr/bin/env python3
"""Read `harness.toml`, the one place a project tells the harness how it builds.

Every control that used to spell a Python-and-uv command reads it from here:
scripts/gate.sh (setup, lint), scripts/run.sh (test command and its pass count),
scripts/land.sh (project name, untracked inputs to copy), and the Claude hooks
(watched files, mapped tests). A missing file means the defaults below, which
are this harness's own Python layout, so a repository adopts the harness by
copying it and writing only the keys that differ.

An unknown key is an error, not ignored: a misspelt `tset = "pytest"` would
otherwise leave the default test command in force and pass a gate nobody
configured.

    python3 scripts/harness_config.py --shell              # KEY=value lines for bash eval
    python3 scripts/harness_config.py --json               # the merged config
    python3 scripts/harness_config.py --count OUTPUT_FILE  # checks a test run reported

Reading a harness.toml needs Python 3.11+ (tomllib). The defaults need nothing.
"""

import argparse
import copy
import json
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path

FILE = "harness.toml"

DEFAULTS = {
    "project": {
        # Empty means the main checkout's directory name (land.sh resolves it).
        "name": "",
    },
    "commands": {
        "setup": "uv sync --frozen --quiet",
        "lint": "uvx ruff check .",
        "test": "./scripts/standalone_tests.sh",
        "pipeline": "",
    },
    "tests": {
        # Python regex; the last match's first group is the number of checks the
        # test command ran. Empty disables the check and trusts the exit code.
        "count_pattern": r"(\d+) checks total",
    },
    "land": {
        # Repository-relative paths (files or directories) the gate needs but
        # git does not carry. Copied from the main checkout into the lander.
        "copy_untracked": [],
    },
    "roles": {
        # Agent types (fnmatch patterns) that must leave the main checkout as
        # they found it. .claude/hooks/readonly_guard.py snapshots the checkout
        # when one starts and compares when it stops.
        "read_only": [
            "dual-derive",
            "live-proof-auditor",
            "reviewer-*",
            "Explore",
            "Plan",
        ],
    },
    "adr": {
        # Architecture decision records: docs/adr/NNNN-slug.md (scripts/adr.py).
        "dir": "docs/adr",
        # Repository-relative globs ('*' stays inside one path segment, '**'
        # spans any number). A file matching one must be named, literally, in
        # the `Components:` of a proposed or accepted ADR at every land
        # (tests/test_adr.py). Empty: ADRs are written for real decisions, and
        # `scripts/adr.py check` still validates the ones that exist.
        "require_for": [],
        "exempt": [],  # literal paths, each one named
    },
    "jev": {
        # scripts/jev.py: JEV_DISABLE=1 also turns it off. timeout is seconds per request.
        "enabled": True,
        "timeout": 8.0,
    },
    "tiers": {
        # Floor globs per consequence tier (scripts/jev.py consequence). A path
        # matching none is Medium; a change's floor is its highest path. Checked
        # critical, high, medium, low. Rules and machinery files are High.
        "low": ["README.md", "ENGINEERING_LOG.md", "HARNESS.md", "docs/**"],
        "medium": ["docs/adr/**"],
        "high": [
            ".claude/**",
            ".agents/**",
            ".codex/**",
            ".gemini/**",
            ".github/**",
            ".githooks/**",
            "AGENTS.md",
            "CLAUDE.md",
            "**/AGENTS.md",
            "**/CLAUDE.md",
            "harness.toml",
            "scripts/paths.py",
            "scripts/harness_config.py",
            "scripts/jev.py",
            "scripts/plan_tests.py",
            "scripts/standalone_tests.sh",
            "scripts/land.sh",
            "scripts/gate.sh",
            "scripts/run.sh",
            "mutants/**",
            "tests/**",
            "scripts/harness_env.sh",
            "scripts/adr.py",
            "docs/agents/**",
            "docs/mutation-testing.md",
        ],
        "critical": [],
    },
    "loop": {
        # scripts/task_loop.py. `label` marks the open issues the loop may take; `priority`
        # lists the labels from most to least urgent (an issue with none ranks last);
        # `base` is the branch tasks start from and their pull requests target.
        "label": "ready-for-agent",
        "runtime": "claude",
        "base": "main",
        "priority": ["priority:high", "priority:medium", "priority:low"],
        # Headless worker command per runtime; the brief is appended as the last argument.
        "workers": {
            "claude": "claude -p",
            "codex": "codex exec",
            "gemini": "gemini -p",
        },
    },
    "budget": {
        # Per session, checked by .claude/hooks/budget_guard.py: warn at 80%, deny at 100%. 0 turns a limit off.
        "max_tool_calls": 400,
        # Minutes of active time: a gap over 30 minutes between tool calls is not counted.
        "max_minutes": 180,
        # Files an agent may still Write or Edit once the budget is exhausted, to leave its status.
        "status_glob": ["**/STATUS.md", "**/status.md", "tmp/**/status*.md"],
    },
    "hooks": {
        "watched_files": [
            "harness.toml",
            "scripts/paths.py",
            "scripts/harness_config.py",
            "scripts/gate.sh",
            "scripts/land.sh",
            "scripts/run.sh",
            ".claude/settings.json",
        ],
        "watched_dirs": [".githooks/"],
        "skip_dirs": [".venv", "__pycache__", "node_modules", ".git"],
    },
    # The one hook registration table (scripts/render_hooks.py renders it into
    # .claude/settings.json, .codex/hooks.json and .gemini/settings.json). Each
    # entry: name, event (Claude Code's name), matcher, kind (gate|advisory),
    # timeout (seconds), command (the Claude command; Codex and Gemini run it
    # through scripts/hook_adapter.py). Named `hook`, not `hooks`: TOML cannot
    # hold the [hooks] table above and an array of the same name.
    "hook": [],
    # First rule whose `match` fits the edited file's name wins. `self` marks a
    # file that is itself a test. `tests` are candidate paths, first existing one
    # runs. Placeholders: {dir} {name} {stem} {ext}; `command` also gets {test}.
    "mapped_tests": [
        {
            "match": "*.py",
            "self": "*tests/test_*.py",
            "tests": ["{dir}/tests/test_{name}", "tests/test_{name}"],
            "command": "uv run --no-sync python3 {test}",
        },
    ],
}

STRING_LISTS = {
    ("budget", "status_glob"),
    ("land", "copy_untracked"),
    ("roles", "read_only"),
    ("adr", "require_for"),
    ("adr", "exempt"),
    ("hooks", "watched_files"),
    ("hooks", "watched_dirs"),
    ("hooks", "skip_dirs"),
    ("tiers", "low"),
    ("tiers", "medium"),
    ("tiers", "high"),
    ("tiers", "critical"),
    ("loop", "priority"),
}
RULE_KEYS = {"match": str, "self": str, "tests": list, "command": str}
HOOK_KEYS = {
    "name": str,
    "event": str,
    "matcher": str,
    "kind": str,
    "timeout": int,
    "command": str,
}
HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "Stop",
)


class ConfigError(Exception):
    """harness.toml exists but cannot be used as written."""


def safe_relative(value, where):
    """Refuse a path that is absolute or climbs out of the repository."""
    if not value or value.startswith("/") or ".." in Path(value).parts:
        raise ConfigError(
            f"{where}: {value!r} must be a relative path inside the repository"
        )
    return value


def merge_hooks(body):
    """Validate the [[hook]] registration table and return it as a list of dicts."""
    if not isinstance(body, list):
        raise ConfigError("hook must be an array of tables ([[hook]])")
    hooks, names = [], set()
    for i, entry in enumerate(body):
        where = f"hook[{i}]"
        unknown = set(entry) - set(HOOK_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
        missing = {"name", "event", "kind", "timeout", "command"} - set(entry)
        if missing:
            raise ConfigError(f"{where}: missing key(s) {sorted(missing)}")
        for key, kind in HOOK_KEYS.items():
            if key in entry and (
                not isinstance(entry[key], kind) or isinstance(entry[key], bool)
            ):
                raise ConfigError(f"{where}.{key} must be a {kind.__name__}")
        if entry["event"] not in HOOK_EVENTS:
            raise ConfigError(
                f"{where}.event {entry['event']!r} must be one of {list(HOOK_EVENTS)}"
            )
        if entry["kind"] not in ("gate", "advisory"):
            raise ConfigError(f"{where}.kind must be 'gate' or 'advisory'")
        if entry["timeout"] <= 0:
            raise ConfigError(f"{where}.timeout must be positive")
        if entry["name"] in names:
            raise ConfigError(f"{where}.name {entry['name']!r} is used twice")
        names.add(entry["name"])
        hooks.append({"matcher": "", **entry})
    return hooks


def merge(raw):
    """Return DEFAULTS overlaid with one parsed harness.toml, validated."""
    config = copy.deepcopy(DEFAULTS)
    for section, body in raw.items():
        if section not in DEFAULTS:
            raise ConfigError(f"unknown section [{section}]")
        if section == "mapped_tests":
            if not isinstance(body, list):
                raise ConfigError(
                    "mapped_tests must be an array of tables ([[mapped_tests]])"
                )
            rules = []
            for i, rule in enumerate(body):
                where = f"mapped_tests[{i}]"
                unknown = set(rule) - set(RULE_KEYS)
                if unknown:
                    raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
                missing = {"match", "tests", "command"} - set(rule)
                if missing:
                    raise ConfigError(f"{where}: missing key(s) {sorted(missing)}")
                for key, kind in RULE_KEYS.items():
                    if key in rule and not isinstance(rule[key], kind):
                        raise ConfigError(f"{where}.{key} must be a {kind.__name__}")
                if not all(isinstance(t, str) for t in rule["tests"]):
                    raise ConfigError(f"{where}.tests must be a list of strings")
                rules.append({"self": "", **rule})
            config["mapped_tests"] = rules
            continue
        if section == "hook":
            config["hook"] = merge_hooks(body)
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] must be a table")
        for key, value in body.items():
            if key not in DEFAULTS[section]:
                raise ConfigError(f"unknown key {section}.{key}")
            if (section, key) in STRING_LISTS:
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    raise ConfigError(f"{section}.{key} must be a list of strings")
            elif (section, key) == ("jev", "enabled"):
                if not isinstance(value, bool):
                    raise ConfigError("jev.enabled must be true or false")
            elif (section, key) == ("loop", "workers"):
                if not isinstance(value, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) and v.strip()
                    for k, v in value.items()
                ):
                    raise ConfigError("loop.workers must map runtime names to commands")
                value = {**DEFAULTS["loop"]["workers"], **value}
            elif section == "budget":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ConfigError(
                        f"budget.{key} must be an integer >= 0 (0 is off)"
                    )
            elif (section, key) == ("jev", "timeout"):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or value <= 0
                ):
                    raise ConfigError("jev.timeout must be a number of seconds above 0")
            elif not isinstance(value, str):
                raise ConfigError(f"{section}.{key} must be a string")
            config[section][key] = value
    loop = config["loop"]
    if loop["runtime"] not in loop["workers"]:
        raise ConfigError(
            f"loop.runtime {loop['runtime']!r} has no loop.workers command"
        )
    if not loop["priority"]:
        raise ConfigError("loop.priority needs at least one label")
    for runtime, command in loop["workers"].items():
        if "--dangerously" in command:
            raise ConfigError(
                f"loop.workers.{runtime}: a --dangerously-* flag is never allowed"
            )
    for rel in config["land"]["copy_untracked"]:
        safe_relative(rel, "land.copy_untracked")
    adr_dir = safe_relative(config["adr"]["dir"], "adr.dir").strip("/")
    if set("*?[") & set(adr_dir):
        raise ConfigError(f"adr.dir: {adr_dir!r} must be a plain directory, not a glob")
    for entry in config["adr"]["require_for"]:
        safe_relative(entry, "adr.require_for")
        if set("*?[") & set(entry.strip("/").split("/")[0]):
            raise ConfigError(
                f"adr.require_for: {entry!r} must start with a fixed directory or "
                "file name, not a glob"
            )
    for tier, globs in (
        (k, config["tiers"][k]) for k in ("low", "medium", "high", "critical")
    ):
        for entry in globs:
            safe_relative(entry, f"tiers.{tier}")
    for entry in config["adr"]["exempt"]:
        safe_relative(entry, "adr.exempt")
        if set("*?[") & set(entry):
            # A glob here would exempt files wholesale; each exemption is named.
            raise ConfigError(
                f"adr.exempt: {entry!r} must be a literal path, not a glob"
            )
    for entry in config["adr"]["require_for"]:
        if (entry.lower() + "/").startswith(adr_dir.lower() + "/"):
            # Files under adr.dir are never governed, so this would switch the rule off.
            raise ConfigError(
                f"adr.dir {adr_dir!r} contains the governed path {entry!r}"
            )
    if config["tests"]["count_pattern"]:
        try:
            if re.compile(config["tests"]["count_pattern"]).groups < 1:
                raise ConfigError(
                    "tests.count_pattern needs one capture group for the count"
                )
        except re.error as exc:
            raise ConfigError(
                f"tests.count_pattern is not a valid regex: {exc}"
            ) from exc
    return config


def load(root):
    """Return the merged config for one repository root.

    Args:
        root (Path | str): the repository's top-level directory.

    Returns:
        (dict): DEFAULTS overlaid with root/harness.toml when it exists.
    """
    path = Path(root) / FILE
    if not path.is_file():
        return copy.deepcopy(DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError as exc:
        raise ConfigError(
            f"{path} needs Python 3.11+ to read (tomllib); "
            f"this is Python {sys.version.split()[0]}"
        ) from exc
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return merge(raw)


def mapped_test(config, root, edited):
    """Return (test path, shell command) for one edited file, or None.

    Args:
        config (dict): a loaded config.
        root (Path): repository root.
        edited (Path): the file just edited, absolute.

    Returns:
        (tuple | None): the test to run and the command that runs it.
    """
    from fnmatch import fnmatch

    root = Path(root).resolve()
    try:
        rel = Path(edited).resolve().relative_to(root)
    except ValueError:
        return None
    if set(rel.parts) & set(config["hooks"]["skip_dirs"]):
        return None
    name = rel.name
    for rule in config["mapped_tests"]:
        if not fnmatch(name, rule["match"]):
            continue
        # A `self` pattern with a "/" matches the repository-relative path, else the name.
        if rule["self"] and fnmatch(
            rel.as_posix() if "/" in rule["self"] else name, rule["self"]
        ):
            test = root / rel
        else:
            fields = {
                "dir": rel.parent.as_posix(),
                "name": name,
                "stem": rel.stem,
                "ext": rel.suffix,
            }
            test = None
            for pattern in rule["tests"]:
                candidate = root / posixpath.normpath(pattern.format(**fields))
                if candidate.is_file():
                    test = candidate
                    break
            if test is None:
                return None
        test_rel = test.relative_to(root).as_posix()
        return test, rule["command"].format(test=shlex.quote(test_rel))
    return None


def count(config, output):
    """Return the check count a test run reported, or None if it reported none."""
    pattern = config["tests"]["count_pattern"]
    if not pattern:
        return None
    found = re.findall(pattern, output, re.M)
    if not found:
        return None
    last = found[-1]
    return int(last[0] if isinstance(last, tuple) else last)


def shell(config):
    """Return `KEY=value` lines a bash script can eval."""
    c, lines = config, []
    scalars = {
        "HARNESS_NAME": c["project"]["name"],
        "HARNESS_SETUP": c["commands"]["setup"],
        "HARNESS_LINT": c["commands"]["lint"],
        "HARNESS_TEST": c["commands"]["test"],
        "HARNESS_PIPELINE": c["commands"]["pipeline"],
        "HARNESS_COUNT_PATTERN": c["tests"]["count_pattern"],
    }
    for key, value in scalars.items():
        lines.append(f"{key}={shlex.quote(value)}")
    copies = " ".join(shlex.quote(v) for v in c["land"]["copy_untracked"])
    lines.append(f"HARNESS_COPY_UNTRACKED=({copies})")
    return "\n".join(lines)


def default_root():
    """The git top level of the current directory, else the directory itself."""
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(done.stdout.strip()) if done.returncode == 0 else Path.cwd()


def main():
    parser = argparse.ArgumentParser(
        description="Read harness.toml for the harness scripts."
    )
    parser.add_argument("--root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shell", action="store_true")
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--count", metavar="OUTPUT_FILE")
    args = parser.parse_args()
    root = args.root or default_root()
    try:
        config = load(root)
    except ConfigError as exc:
        print(f"✗ harness.toml: {exc}", file=sys.stderr)
        return 2
    if args.shell:
        print(shell(config))
    elif args.json:
        print(json.dumps(config, indent=2))
    else:
        if not config["tests"]["count_pattern"]:
            print("count check disabled (tests.count_pattern is empty)")
            return 0
        text = Path(args.count).read_text(encoding="utf-8", errors="replace")
        n = count(config, text)
        if not n:
            print(
                f"✗ the test command reported no checks (tests.count_pattern "
                f"{config['tests']['count_pattern']!r} {'found 0' if n == 0 else 'did not match'})",
                file=sys.stderr,
            )
            return 1
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
