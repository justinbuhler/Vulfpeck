"""worktree_status.sh checks. Throwaway repos in tmp only, GIT_* scrubbed on every
subprocess for the same reason test_land.py scrubs it: this suite runs inside the
pre-commit hook, which exports GIT_DIR and GIT_INDEX_FILE, and an unscrubbed call
would act on the repository being committed to rather than the fixture.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "worktree_status.sh"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


GIT_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX",
           "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
           "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def scrubbed_env(**extra):
    e = {k: v for k, v in os.environ.items() if k not in GIT_ENV}
    e.update(extra)
    return e


def git(cwd, *args, check_rc=True):
    r = subprocess.run(("git",) + args, cwd=str(cwd), capture_output=True, text=True,
                       env=scrubbed_env())
    if check_rc and r.returncode:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout.strip()


def assert_contained(d):
    """Refuse to touch anything unless git resolves to this fixture's own repo."""
    got = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=str(d),
                         capture_output=True, text=True, env=scrubbed_env()).stdout.strip()
    want = str((Path(d) / ".git").resolve())
    if got != want:
        raise AssertionError(f"fixture escaped its sandbox: git resolves to {got!r}, expected {want!r}.")


def status(cwd, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=str(cwd), capture_output=True,
                          text=True, env=scrubbed_env())


# An origin remote so origin/main means something - a bare clone of the fixture
# repo itself, fetched into it, exactly what land.sh and this script both expect.
d = Path(tempfile.mkdtemp(prefix="harness-wt-status."))
origin = Path(tempfile.mkdtemp(prefix="harness-wt-status-origin.")) / "origin.git"
git(d, "init", "-q", "-b", "main")
git(d, "config", "user.email", "t@t")
git(d, "config", "user.name", "t")
git(d, "config", "commit.gpgsign", "false")
assert_contained(d)
(d / "base.txt").write_text("base\n")
git(d, "add", "-A")
git(d, "commit", "-qm", "base")
git(d, "init", "-q", "--bare", str(origin))
git(d, "remote", "add", "origin", str(origin))
git(d, "push", "-q", "origin", "main")

# Landed: branched off main, fast-forward merged back and pushed - so the branch
# carries zero commits origin/main lacks. This is the shape the script must prune.
git(d, "checkout", "-qb", "landed", "main")
(d / "landed.txt").write_text("shipped\n")
git(d, "add", "-A")
git(d, "commit", "-qm", "shipped work")
git(d, "checkout", "-q", "main")
git(d, "merge", "-q", "--ff-only", "landed")
git(d, "push", "-q", "origin", "main")
git(d, "checkout", "-q", "main")
wt_landed = d / "wt-landed"
git(d, "worktree", "add", "-q", str(wt_landed), "landed")

# Live: unique commits origin/main does not have yet.
wt_live = d / "wt-live"
git(d, "worktree", "add", "-q", "-b", "live", str(wt_live), "main")
(wt_live / "live.txt").write_text("in progress\n")
git(wt_live, "add", "-A")
git(wt_live, "commit", "-qm", "still working")

# Dirty-but-landed: zero unique commits, but an uncommitted edit sitting in it.
git(d, "checkout", "-qb", "landed2", "main")
(d / "landed2.txt").write_text("also shipped\n")
git(d, "add", "-A")
git(d, "commit", "-qm", "also shipped")
git(d, "checkout", "-q", "main")
git(d, "merge", "-q", "--ff-only", "landed2")
git(d, "push", "-q", "origin", "main")
git(d, "checkout", "-q", "main")
wt_dirty = d / "wt-dirty"
git(d, "worktree", "add", "-q", str(wt_dirty), "landed2")
(wt_dirty / "scratch.txt").write_text("uncommitted\n")

r_report = status(d)
check("report mode exits 0", r_report.returncode == 0, r_report.stdout + r_report.stderr)
check("a fully-landed worktree is flagged STALE",
      "wt-landed" in r_report.stdout and "STALE" in r_report.stdout.split("wt-landed")[1].splitlines()[0])
check("a live worktree with unpushed commits is not flagged",
      "STALE" not in r_report.stdout.split("wt-live")[1].splitlines()[0])
check("a landed-but-dirty worktree is not flagged",
      "STALE" not in r_report.stdout.split("wt-dirty")[1].splitlines()[0])
check("report mode does not remove anything", wt_landed.is_dir())

# Without lsof every worktree would read as idle, so --prune must remove none.
nolsof = Path(tempfile.mkdtemp(prefix="harness-nolsof."))
for _dir in os.environ["PATH"].split(os.pathsep):
    for _exe in Path(_dir).glob("*") if Path(_dir).is_dir() else ():
        if _exe.name != "lsof" and not os.path.lexists(nolsof / _exe.name):
            (nolsof / _exe.name).symlink_to(_exe)
r_nolsof = subprocess.run(["bash", str(SCRIPT), "--prune"], cwd=str(d), capture_output=True,
                          text=True, env=scrubbed_env(PATH=str(nolsof)))
check("prune without lsof exits 0", r_nolsof.returncode == 0, r_nolsof.stdout + r_nolsof.stderr)
check("prune without lsof removes nothing", wt_landed.is_dir(), r_nolsof.stdout)

# lsof exits 1 for both "idle" and its own errors; only exit 1 with no output is idle.
badlsof = Path(tempfile.mkdtemp(prefix="harness-badlsof."))
(badlsof / "lsof").write_text("#!/bin/sh\necho 'lsof: cannot read process info' >&2\nexit 1\n")
(badlsof / "lsof").chmod(0o755)
r_bad = subprocess.run(["bash", str(SCRIPT), "--prune"], cwd=str(d), capture_output=True,
                       text=True, env=scrubbed_env(PATH=f"{badlsof}{os.pathsep}{os.environ['PATH']}"))
check("prune with an erroring lsof exits 0", r_bad.returncode == 0, r_bad.stdout + r_bad.stderr)
check("prune with an erroring lsof removes nothing", wt_landed.is_dir(), r_bad.stdout)

r_prune = status(d, "--prune")
check("prune mode exits 0", r_prune.returncode == 0, r_prune.stdout + r_prune.stderr)
check("the fully-landed worktree is removed", not wt_landed.exists(), r_prune.stdout)
check("the live worktree survives", wt_live.is_dir())
check("the landed-but-dirty worktree survives (git refuses to remove a dirty tree)",
      wt_dirty.is_dir())
check("the live branch is untouched", git(d, "rev-parse", "live"))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
