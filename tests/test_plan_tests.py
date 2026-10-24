"""scripts/plan_tests.py: each consequence tier picks exactly its commands.

Plans are built against a throwaway tree, with every expected command a literal
written here (Rule 11), never read back from plan_tests.py.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import harness_config  # noqa: E402
import plan_tests  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


CONFIG = harness_config.merge({})
LINT = "uvx ruff check ."
SUITE = "./scripts/run.sh --tests"
RUN_A = "uv run --no-sync python3 tests/test_a.py"
RUN_B = "uv run --no-sync python3 tests/test_uses_b.py"

root = Path(tempfile.mkdtemp(prefix="test-plan."))
for rel, text in {
    "scripts/a.py": "def one():\n    return 1\n",
    "scripts/b.py": "def two():\n    return 2\n",
    "tests/test_a.py": "import a\n",
    "tests/test_uses_b.py": "import b\n",
    "tests/test_other.py": "print('nothing here')\n",
    "scripts/test_helper.py": "import helper\n",
    "scripts/helper.py": "x = 1\n",
    "docs/x.md": "doc\n",
    "mutants/patches/M001.patch": "# mutant_id: M001\n--- a/scripts/a.py\n+++ b/scripts/a.py\n",
    "mutants/patches/M002.patch": "# mutant_id: M002\n--- a/scripts/zzz.py\n+++ b/scripts/zzz.py\n",
    "mutants/patches/M003.patch": "# mutant_id: M003\n--- a/scripts/a.py\n+++ b/scripts/a.py\n",
}.items():
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def cmds(result):
    return [c["command"] for c in result["commands"]]


low = plan_tests.plan(root, "Low", ["scripts/a.py"], CONFIG)
check(
    "Low: lint plus the mapped test of the changed file, nothing else",
    cmds(low) == [LINT, RUN_A],
    str(cmds(low)),
)
check(
    "Low names what it skipped",
    any("full suite" in s for s in low["skipped"])
    and any("mutation" in s for s in low["skipped"]),
)
check(
    "Low docs only: lint alone",
    cmds(plan_tests.plan(root, "Low", ["docs/x.md"], CONFIG)) == [LINT],
)
check(
    "a script named test_*.py outside tests/ is never run as its own test",
    cmds(plan_tests.plan(root, "Low", ["scripts/test_helper.py"], CONFIG)) == [LINT],
)
check(
    "Low: a deleted file's mapped test still runs, a deleted test file is not run",
    cmds(plan_tests.plan(root, "Low", ["scripts/a.py", "tests/test_gone.py"], CONFIG))
    == [LINT, RUN_A],
)

med = plan_tests.plan(root, "Medium", ["scripts/a.py", "scripts/b.py"], CONFIG)
check(
    "Medium: lint, the mapped test, and the test that names a changed module",
    sorted(cmds(med)) == sorted([LINT, RUN_A, RUN_B]),
    str(cmds(med)),
)
check(
    "Medium does not run a test that names no changed module",
    "tests/test_other.py" not in " ".join(cmds(med)),
)
check(
    "Medium does not run the full suite or mutation",
    SUITE not in cmds(med) and not any("runner.py" in c for c in cmds(med)),
)

medj = plan_tests.plan(root, "Medium", ["harness.toml"], CONFIG)
check(
    "Medium with a non-.py file nothing covers runs the full suite, not lint alone",
    cmds(medj) == [LINT, SUITE],
    str(cmds(medj)),
)
medm = plan_tests.plan(root, "Medium", ["scripts/a.py", "docs/adr/0001-x.md"], CONFIG)
check(
    "Medium: one uncovered file widens the whole plan to the full suite",
    cmds(medm) == [LINT, SUITE],
    str(cmds(medm)),
)
check(
    "Medium: a deleted module is covered by the test that names it",
    cmds(plan_tests.plan(root, "Medium", ["scripts/b.py"], CONFIG)) == [LINT, RUN_B],
)
check(
    "a Medium test file under a non-tests directory is not picked up by the naming scan",
    "scripts/test_helper.py"
    not in " ".join(
        cmds(
            plan_tests.plan(
                root, "Medium", ["scripts/b.py", "scripts/helper.py"], CONFIG
            )
        )
    ),
)

high = plan_tests.plan(root, "High", ["scripts/a.py"], CONFIG)
check(
    "High: lint, the full suite, and exactly the mutants that touch a changed file",
    cmds(high) == [LINT, SUITE, "python3 mutants/runner.py --only M001 M003"],
    str(cmds(high)),
)
renamed = plan_tests.plan(
    root, "High", ["scripts/zzz.py", "scripts/zzz_new.py"], CONFIG
)
check(
    "mutant selection matches a rename's old path",
    cmds(renamed)[-1] == "python3 mutants/runner.py --only M002",
    str(cmds(renamed)),
)
check(
    "High does not run M002, whose patch touches an unchanged file",
    "M002" not in " ".join(cmds(high)),
)
high_b = plan_tests.plan(root, "High", ["scripts/b.py"], CONFIG)
check(
    "High with no matching mutant runs no mutation and says so",
    cmds(high_b) == [LINT, SUITE] and any("no mutant" in s for s in high_b["skipped"]),
    str(high_b),
)

crit = plan_tests.plan(root, "Critical", ["scripts/a.py"], CONFIG)
check("Critical runs what High runs", cmds(crit) == cmds(high))
check(
    "Critical adds the independent re-derivation requirement",
    any("re-derivation" in n for n in crit["notes"]),
)
check(
    "High does not carry the re-derivation note",
    not any("re-derivation" in n for n in high["notes"]),
)
check(
    "every plan says the land gate is unchanged",
    all(
        any("land gate is unchanged" in n for n in p["notes"])
        for p in (low, med, high, crit)
    ),
)

(root / "scripts/c+d(.py").write_text("x = 1\n")
(root / "tests/test_plus.py").write_text("uses c+d( here\n")
check(
    "a module stem with regex characters is searched as a fixed string, not a pattern",
    plan_tests.referencing_tests(root, "scripts/c+d(.py") == ["tests/test_plus.py"],
    str(plan_tests.referencing_tests(root, "scripts/c+d(.py")),
)

# --- run: stop at the first failure -------------------------------------------------
marker = root / "ran-after-failure"
status = plan_tests.run(
    root,
    [{"command": "exit 3", "why": "w"}, {"command": f"touch {marker}", "why": "w"}],
)
check(
    "--run exits with the failing command's status and runs nothing after it",
    status == 3 and not marker.exists(),
)
status = plan_tests.run(
    root, [{"command": "true", "why": "w"}, {"command": f"touch {marker}", "why": "w"}]
)
check("--run runs every command when all pass", status == 0 and marker.exists())

# --- change(): a real git repository ----------------------------------------------------
repo = Path(tempfile.mkdtemp(prefix="plan-tests-git."))
GIT_ENV = {
    "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
    "HOME": str(repo),
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV
    )


def write(rel, text):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)


BODY = "def f():\n    return 1\n" * 20
write(
    "m.py",
    "def alpha():\n    x = 1\n    y = 2\n    return x\n\n\ndef beta():\n    return 0\n",
)
write("scripts/gate.sh", "#!/bin/sh\nruff\n")
write(".claude/hooks/guard.py", BODY)
write(".claude/hooks/mover.py", BODY)
write("README.md", "x\n")
git("init", "-q", "-b", "main")
git("add", "-A")
git("commit", "-qm", "base")
git("checkout", "-q", "-b", "work")
write(
    "m.py",
    "def alpha():\n    x = 1\n    y = 3\n    return x\n\n\ndef beta():\n    return 0\n",
)
(repo / "scripts/gate.sh").unlink()
(repo / ".claude/hooks/guard.py").unlink()
(repo / "docs").mkdir()
(repo / ".claude/hooks/mover.py").rename(repo / "docs/mover.py")
write("n.md", "new\n")
write(".env", "TOKEN=abc\n")
git("add", "-A", ":!.env")
git("commit", "-qm", "edit alpha")
c = plan_tests.change(repo, "main")
check(
    "change counts deleted paths and both sides of a rename",
    c["paths"]
    == [
        ".claude/hooks/guard.py",
        ".claude/hooks/mover.py",
        ".env",
        "docs/mover.py",
        "m.py",
        "n.md",
        "scripts/gate.sh",
    ],
    str(c["paths"]),
)
check(
    "change reports the paths that no longer exist",
    c["removed"]
    == [".claude/hooks/guard.py", ".claude/hooks/mover.py", "scripts/gate.sh"],
    str(c["removed"]),
)
check(
    "change reports live files only under files",
    c["files"] == [".env", "docs/mover.py", "m.py", "n.md"],
    str(c["files"]),
)
check(
    "change reports diff stats, a rename as old => new",
    sorted(c["stats"])
    == [
        ".claude/hooks/guard.py +0 -40",
        ".claude/hooks/mover.py => docs/mover.py +0 -0",
        "m.py +1 -1",
        "n.md +1 -0",
        "scripts/gate.sh +0 -2",
    ],
    str(c["stats"]),
)
check(
    "change names the enclosing symbol, never the changed line",
    c["symbols"] == ["alpha"],
    str(c["symbols"]),
)
check(
    "change returns the commit subjects of base..HEAD", c["subjects"] == ["edit alpha"]
)

TIERS = CONFIG["tiers"]
check(
    "deleting scripts/gate.sh sets a High floor",
    plan_tests.jev.floor_tier(["scripts/gate.sh"], TIERS)[0] == "High",
)
check(
    "the floor of this change is High through the deleted hook and gate, not Low through docs/",
    plan_tests.jev.floor_tier(c["paths"], TIERS)
    == (
        "High",
        [".claude/hooks/guard.py", ".claude/hooks/mover.py", "scripts/gate.sh"],
    ),
)

# working tree, index and untracked work are seen before any commit
write("m.py", "def alpha():\n    x = 9\n    return x\n")
git("add", "m.py")
write("m.py", "def alpha():\n    x = 10\n    return x\n")
write("fresh.py", "x = 1\n")
(repo / "README.md").unlink()
c = plan_tests.change(repo, "main")
check(
    "uncommitted work counts: unstaged edit, untracked file and an unstaged delete",
    {"m.py", "fresh.py", "README.md"} <= set(c["paths"])
    and "README.md" in c["removed"],
    str(c["paths"]),
)
check("an untracked file is a live file", "fresh.py" in c["files"])
check(
    "the ignored-by-git .env is not double counted from the untracked scan",
    c["paths"].count(".env") == 1,
)

# --- main(): the floor refuses a lower --tier -------------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

os.environ["JEV_DISABLE"] = "1"


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = plan_tests.main(list(argv), root=repo)
    return status, out.getvalue(), err.getvalue()


status, out, err = cli("--base", "main", "--tier", "low")
check(
    "--tier Low on a change that deletes gate.sh is refused with a non-zero status",
    status == 2 and out == "",
    f"{status} {out}",
)
check(
    "the refusal names the floor and the paths that set it",
    "below the High floor" in err
    and "scripts/gate.sh" in err
    and "--below-floor" in err,
    err,
)
status, out, err = cli("--base", "main", "--tier", "Low", "--below-floor")
data = json.loads(out)
check(
    "--below-floor allows it and records the override in the JSON",
    status == 0
    and data["below_floor_override"] is True
    and data["tier"] == "Low"
    and data["floor"] == "High",
    out[:300],
)
check(
    "the override is also a note in the plan",
    any("OVERRIDE" in n for n in data["notes"]),
)
status, out, err = cli("--base", "main", "--tier", "high")
data = json.loads(out)
check(
    "--tier at the floor is accepted without an override",
    status == 0
    and data["below_floor_override"] is False
    and data["tier_source"] == "given",
)
status, out, err = cli("--base", "main")
data = json.loads(out)
check(
    "with no --tier and Jev off the plan uses the floor, marked fallback",
    status == 0
    and data["tier"] == "High"
    and data["tier_source"] == "fallback:fallback",
    out[:300],
)

repo = Path(tempfile.mkdtemp(prefix="plan-tests-empty."))
write("x.py", "x = 1\n")
git("init", "-q", "-b", "main")
git("add", "-A")
git("commit", "-qm", "base")
status, out, err = cli("--base", "main")
data = json.loads(out)
check(
    "an empty change plans Medium, not Low",
    status == 0
    and data["files"] == []
    and data["tier"] == "Medium"
    and data["floor"] == "Medium",
    out[:300],
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
