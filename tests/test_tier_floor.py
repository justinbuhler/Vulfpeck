"""Every file a gate can import floors at High under the shipped harness.toml [tiers].

A gate hook is only as strong as the modules it imports: a helper that floors at
Medium can be edited with a Medium review while the hooks that trust it are High.
The rule "every gate dependency is High" is written here, not read from
harness.toml (Rule 11); harness.toml supplies only the tiers under test and the
hook table that names the entry points. Imports are found with `ast`, so a lazy
import inside a function counts.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import adr  # noqa: E402
import harness_config  # noqa: E402
import jev  # noqa: E402
from paths import REPO  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


CONFIG = harness_config.load(REPO)
SHIPPED = CONFIG["tiers"]

# Every directory an entry point puts on sys.path: the hooks and agent_harness insert
# scripts/, task_loop inserts .claude/harness, and a script's own directory is
# sys.path[0]. A file found in any of them is a candidate for loading, so the
# over-approximation errs toward reporting a file.
SEARCH = (REPO / "scripts", REPO / ".claude" / "hooks", REPO / ".claude" / "harness")
HOOK_SCRIPT = re.compile(r"\$\{CLAUDE_PROJECT_DIR\}/([^\"\s]+\.py)")
SHELL_GATES = (
    "land.sh",
    "gate.sh",
    "run.sh",
    "harness_env.sh",
    "standalone_tests.sh",
)
HELPER = re.compile(r"(\w+)\.py\b")


def gate_scripts():
    """Return the repository-relative `.py` path of each `kind = "gate"` hook, and the gates that name none."""
    scripts, unnamed = set(), []
    for hook in CONFIG["hook"]:
        if hook["kind"] != "gate":
            continue
        found = HOOK_SCRIPT.findall(hook["command"])
        if len(found) == 1:
            scripts.add(found[0])
        else:
            unnamed.append(hook["name"])
    return scripts, unnamed


def shell_helpers():
    """Return the scripts/*.py files the land gate's shell scripts name outside comments."""
    helpers = set()
    for sh in SHELL_GATES:
        for line in (REPO / "scripts" / sh).read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in HELPER.findall(line):
                if (REPO / "scripts" / f"{name}.py").is_file():
                    helpers.add(f"scripts/{name}.py")
    return helpers


def imported_modules(path):
    """Return the top-level module names `path` imports anywhere, `.` for a relative import."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." if node.level else node.module.split(".")[0])
    return names


def resolve(name):
    for directory in SEARCH:
        candidate = directory / f"{name}.py"
        if candidate.is_file():
            return candidate
    return None


def walk(entries):
    """Return the repository-relative files reachable from `entries` and the local imports that did not resolve."""
    seen, unresolved, todo = set(), [], [REPO / e for e in entries]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in sorted(imported_modules(path)):
            dep = resolve(name)
            if dep:
                todo.append(dep)
            elif name not in sys.stdlib_module_names:
                unresolved.append(f"{path.relative_to(REPO)} imports {name}")
    return {p.relative_to(REPO).as_posix() for p in seen}, unresolved


GATES, UNNAMED = gate_scripts()
ENTRIES = sorted(
    GATES
    | {
        "scripts/task_loop.py",
        ".claude/harness/agent_harness.py",
        "scripts/hook_adapter.py",
    }
    | shell_helpers()
)
DEPENDENCIES, UNRESOLVED = walk(ENTRIES)


def below_high(tiers):
    """Return the files a gate or entry point can load that do not floor at High or Critical under `tiers`."""
    return sorted(
        p
        for p in DEPENDENCIES
        if jev.floor_tier([p], tiers)[0] not in ("High", "Critical")
    )


def without(tiers, path):
    """Return a copy of `tiers` whose High globs no longer match `path`."""
    return {
        **tiers,
        "high": [g for g in tiers["high"] if not adr.glob_match(g, path)],
    }


# --- the walk finds what it must (so an empty walk cannot pass) -----------------
check(
    "every gate hook command names exactly one script",
    not UNNAMED,
    f"gates without one script: {UNNAMED}",
)
check(
    "the gate table is read: loop_guard and scope_guard are gates",
    {".claude/hooks/loop_guard.py", ".claude/hooks/scope_guard.py"} <= GATES,
    str(sorted(GATES)),
)
check(
    "the land gate's shell scripts name their python helpers",
    {"scripts/harness_events.py", "scripts/harness_inputs.py", "scripts/adr.py"}
    <= shell_helpers(),
    str(sorted(shell_helpers())),
)
check(
    "a lazy import inside a function counts: scope.py reaches jev through one",
    "scripts/jev.py" in walk(["scripts/scope.py"])[0],
)
check(
    "imports are followed transitively: turn_guard reaches harness_events through guard_lib",
    "scripts/harness_events.py" in walk([".claude/hooks/turn_guard.py"])[0],
)
check(
    "hook_adapter is an entry point and reaches paths",
    {"scripts/hook_adapter.py", "scripts/paths.py"}
    <= walk(["scripts/hook_adapter.py"])[0],
)
check(
    "task_loop reaches agent_harness through the directory it adds to sys.path",
    ".claude/harness/agent_harness.py" in walk(["scripts/task_loop.py"])[0],
)
check(
    "every local import of an entry point resolves to a file",
    not UNRESOLVED,
    str(UNRESOLVED),
)

# --- the rule ------------------------------------------------------------------
check(
    "every file a gate or entry point can import floors at High under the shipped tiers",
    not below_high(SHIPPED),
    f"below High: {below_high(SHIPPED)}",
)

# --- the canary: the rule can fail ---------------------------------------------
check(
    "canary: guard_lib is among the dependencies the walk checks",
    "scripts/guard_lib.py" in DEPENDENCIES,
)
check(
    "canary: tiers that drop guard_lib are reported as below High",
    "scripts/guard_lib.py" in below_high(without(SHIPPED, "scripts/guard_lib.py")),
    str(below_high(without(SHIPPED, "scripts/guard_lib.py"))),
)
check(
    "canary: tiers that drop hook_adapter are reported as below High",
    "scripts/hook_adapter.py"
    in below_high(without(SHIPPED, "scripts/hook_adapter.py")),
)
check(
    "canary: dropping one dependency adds it and no unrelated file to the report",
    set(below_high(without(SHIPPED, "scripts/guard_lib.py"))) - set(below_high(SHIPPED))
    == {"scripts/guard_lib.py"},
    str(below_high(without(SHIPPED, "scripts/guard_lib.py"))),
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
