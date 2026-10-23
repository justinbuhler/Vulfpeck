"""Harness hooks and scripts parse as Python 3.11 (harness_doctor accepts 3.11+, hooks run bare python3).

A hook that is a SyntaxError under an older python3 makes its gate crash, and the registered
`|| exit 2` wrapper then denies every tool call. ast.parse(feature_version=(3, 11)) rejects the
3.12+ syntax it knows: unparenthesised `except A, B:`, `type X = ...`, `def f[T]()`. It does not
reject nested same-quote f-strings (3.12); that is a known limit of the check.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


def parses(source):
    try:
        ast.parse(source, feature_version=(3, 11))
    except SyntaxError as exc:
        return exc.msg
    return None


# The checker itself must reject what it exists to catch.
for label, source in (("unparenthesised except A, B", "try:\n    pass\nexcept A, B:\n    pass\n"),
                      ("type statement", "type X = int\n"),
                      ("PEP 695 generic function", "def f[T](x):\n    pass\n")):
    check(f"the 3.11 parse rejects {label}", parses(source) is not None)
check("the 3.11 parse accepts a parenthesised except", parses("try:\n    pass\nexcept (A, B):\n    pass\n") is None)

env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
tracked = subprocess.run(["git", "ls-files", "scripts", ".claude/hooks", "mutants"], cwd=ROOT, capture_output=True,
                         text=True, env=env, check=True).stdout.split()
files = [f for f in tracked if f.endswith(".py") and (f.startswith(("scripts/", ".claude/hooks/")) or f.count("/") == 1)]
check("the tracked scripts, hooks and mutants files are found", any(f == "scripts/guard_lib.py" for f in files)
      and any(f == ".claude/hooks/loop_guard.py" for f in files) and any(f.startswith("mutants/") for f in files), str(files[:5]))
for name in files:
    problem = parses((ROOT / name).read_text(encoding="utf-8"))
    check(f"{name} parses as Python 3.11", problem is None, str(problem))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
