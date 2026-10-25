"""Rule 3's write half: `codegraph sync` is automated in the main checkout and nowhere else.

Two mechanisms, one contract. `.githooks/post-merge` syncs in the background when main
moves; the SessionStart hook runs `codegraph_freshness.py --sync-if-stale`. Both must act
only in the main checkout, only when there is something to sync, and never fail or block
the git operation or the session.

Nothing here touches the real index. A fake `codegraph` on PATH records its argv, and the
fixture repositories live under a temporary directory. Expectations are literal (CLAUDE.md
Rule 11): the settings.json timeouts, the one-sync-per-stale-session count and the
main-checkout guard are not read back from the files they validate.

A guard test is only worth having if the guard is the reason for the outcome, so every skip
case is built so that removing its guard would make the sync happen (the linked worktree
HAS a `.codegraph/`, the repository HAS the hook, and so on).
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# A git hook or `git rebase --exec` exports these, and they override cwd for every git call below.
for _var in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[_var]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codegraph_freshness as cg

HOOK = ROOT / ".githooks" / "post-merge"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


TMP = Path(tempfile.mkdtemp(prefix="cg-sync-test-")).resolve()
SYSTEM_PATH = "/usr/bin:/bin"
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(
    GIT_CONFIG_GLOBAL="/dev/null",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@t",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@t",
)

FAKE = """#!/bin/bash
# fake codegraph: records argv; `status` prints $FAKE_STATUS; `sync` can fix or fail.
echo "$@" >> "$FAKE_LOG"
case "$1" in
  status) [ -f "$FAKE_STATUS" ] && cat "$FAKE_STATUS"; exit "${FAKE_STATUS_RC:-0}" ;;
  sync)   [ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
          [ -n "$FAKE_FIXED_STATUS" ] && cp "$FAKE_FIXED_STATUS" "$FAKE_STATUS"
          exit "${FAKE_RC:-0}" ;;
esac
exit 0
"""


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True
    )


def make_bin(name):
    """A directory holding a fake `codegraph`; returns (bin dir, call log path)."""
    d = TMP / name
    d.mkdir()
    exe = d / "codegraph"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return d, d / "calls.log"


def make_repo(name, with_hook=True, with_index=True):
    """A repository on `trunk` plus a linked worktree, both carrying `.codegraph/`."""
    main = TMP / name
    main.mkdir()
    git("init", "-q", "-b", "trunk", cwd=main)
    (main / "f.txt").write_text("1\n")
    if with_hook:
        (main / ".githooks").mkdir()
        shutil.copy2(HOOK, main / ".githooks" / "post-merge")
        git("config", "core.hooksPath", ".githooks", cwd=main)
    git("add", "-A", cwd=main)
    git("commit", "-q", "-m", "base", cwd=main)
    if with_index:
        (main / ".codegraph").mkdir()
    linked = TMP / (name + "-linked")
    git("worktree", "add", "-q", "-b", "feat", str(linked), cwd=main)
    if with_index:
        (linked / ".codegraph").mkdir()
    return main.resolve(), linked.resolve()


def env_with(bin_dir=None, **extra):
    path = f"{bin_dir}:{SYSTEM_PATH}" if bin_dir else SYSTEM_PATH
    env = dict(GIT_ENV, PATH=path)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run_hook(cwd, env):
    t0 = time.monotonic()
    done = subprocess.run(
        ["bash", str(HOOK)], cwd=cwd, env=env, capture_output=True, text=True
    )
    return done.returncode, time.monotonic() - t0, done


def wait_for(path, seconds=5.0, contains=""):
    """Wait until path is non-empty and, if given, contains the text.

    The background sync appends to its log in several writes, so non-empty is
    not the same as finished: a check of the last line must wait for that line.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip() and contains in path.read_text():
            return True
        time.sleep(0.05)
    return False


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


SETTLE = 1.5  # a background sync that was going to run has started well inside this (0.6 flaked under load)


print("\nthe hook file itself")
check("post-merge is executable, or git silently ignores it", os.access(HOOK, os.X_OK))
check(
    "post-merge passes bash -n",
    subprocess.run(["bash", "-n", str(HOOK)], capture_output=True).returncode == 0,
)
_src = HOOK.read_text()
check(
    "post-merge never uses `set -e` (a failing step must not become a failing pull)",
    re.search(r"^\s*set\s+-[a-z]*e", _src, re.M) is None,
)
check(
    "post-merge ends in an unconditional exit 0",
    _src.rstrip().splitlines()[-1] == "exit 0",
)
_tracked = subprocess.run(
    ["git", "-C", str(ROOT), "ls-files", "-s", ".githooks/post-merge"],
    capture_output=True,
    text=True,
).stdout
check(
    "post-merge is tracked with the executable bit, so a fresh clone is armed",
    _tracked.startswith("100755"),
    _tracked,
)
check(
    "the existing hooks are untouched in kind: pre-commit and pre-merge-commit still present",
    (ROOT / ".githooks" / "pre-commit").is_file()
    and (ROOT / ".githooks" / "pre-merge-commit").is_file(),
)

print("\npost-merge: main checkout syncs, in the background")
BIN, LOG = make_bin("bin-ok")
main1, linked1 = make_repo("r1")
rc, _, _ = run_hook(main1, env_with(BIN, FAKE_LOG=LOG))
check("main checkout: hook exits 0", rc == 0)
check(
    "main checkout: codegraph sync ran, quiet, against the main checkout path",
    wait_for(LOG) and calls(LOG) == [f"sync --quiet {main1}"],
    str(calls(LOG)),
)
check(
    "main checkout: the run is logged under .omc/, which is gitignored",
    wait_for(main1 / ".omc" / "codegraph-sync.log")
    and subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", ".omc/codegraph-sync.log"]
    ).returncode
    == 0,
)

BIN_SLOW, LOG_SLOW = make_bin("bin-slow")
rc, took, done = run_hook(main1, env_with(BIN_SLOW, FAKE_LOG=LOG_SLOW, FAKE_SLEEP=3))
check(
    "a 3 s sync does not hold the hook: it returns in under 2 s",
    rc == 0 and took < 2.0,
    f"{took:.2f}s",
)
check(
    "the hook leaves nothing on stdout/stderr for git to relay",
    done.stdout == "" and done.stderr == "",
)
wait_for(LOG_SLOW)

print("\npost-merge: main-checkout-only guard")
BIN_L, LOG_L = make_bin("bin-linked")
rc, _, done = run_hook(linked1, env_with(BIN_L, FAKE_LOG=LOG_L))
time.sleep(SETTLE)
check("linked worktree (with its own .codegraph/): exits 0", rc == 0)
check(
    "linked worktree (with its own .codegraph/): no sync, so the guard is the reason",
    calls(LOG_L) == [] and (linked1 / ".codegraph").is_dir(),
    str(calls(LOG_L)),
)
check("linked worktree: skips silently", done.stdout == "" and done.stderr == "")
check("linked worktree: writes no .omc log", not (linked1 / ".omc").exists())

print("\npost-merge: one sync at a time (.omc/codegraph-sync.lock)")
BIN_K, LOG_K = make_bin("bin-lock")
main_k, _lk = make_repo("rk")
(main_k / ".omc" / "codegraph-sync.lock").mkdir(parents=True)
rc, _, done = run_hook(main_k, env_with(BIN_K, FAKE_LOG=LOG_K))
time.sleep(SETTLE)
check(
    "lock held by another sync: exits 0, silent, and does not sync",
    rc == 0 and calls(LOG_K) == [] and done.stdout == "" and done.stderr == "",
    str(calls(LOG_K)),
)
check(
    "lock held by another sync: the holder's lock is left in place",
    (main_k / ".omc" / "codegraph-sync.lock").is_dir(),
)
_old = time.time() - 600
os.utime(main_k / ".omc" / "codegraph-sync.lock", (_old, _old))
rc, _, _ = run_hook(main_k, env_with(BIN_K, FAKE_LOG=LOG_K))
check(
    "lock older than 5 minutes (dead holder): broken, and the sync runs",
    rc == 0 and wait_for(LOG_K) and calls(LOG_K) == [f"sync --quiet {main_k}"],
    str(calls(LOG_K)),
)
_deadline = time.monotonic() + 5
while (
    main_k / ".omc" / "codegraph-sync.lock"
).exists() and time.monotonic() < _deadline:
    time.sleep(0.05)
check(
    "the lock is released when the background sync ends",
    not (main_k / ".omc" / "codegraph-sync.lock").exists(),
)

BIN_K2, LOG_K2 = make_bin("bin-lock-overlap")
main_k2, _lk2 = make_repo("rk2")
run_hook(main_k2, env_with(BIN_K2, FAKE_LOG=LOG_K2, FAKE_SLEEP=3))
wait_for(LOG_K2)
run_hook(main_k2, env_with(BIN_K2, FAKE_LOG=LOG_K2, FAKE_SLEEP=3))
time.sleep(SETTLE)
check(
    "two hooks overlapping: exactly one sync runs",
    calls(LOG_K2) == [f"sync --quiet {main_k2}"],
    str(calls(LOG_K2)),
)
time.sleep(2.5)

print("\npost-merge: skips cleanly when there is nothing to sync with")
BIN_N, LOG_N = make_bin("bin-noindex")
main2, _l2 = make_repo("r2", with_index=False)
rc, _, _ = run_hook(main2, env_with(BIN_N, FAKE_LOG=LOG_N))
time.sleep(SETTLE)
check(
    "no .codegraph/: exits 0 and does not sync",
    rc == 0 and calls(LOG_N) == [],
    str(calls(LOG_N)),
)
check("no .codegraph/: creates no .omc directory", not (main2 / ".omc").exists())

assert shutil.which("codegraph", path=SYSTEM_PATH) is None, (
    "precondition: no codegraph on the bare PATH"
)
main3, _l3 = make_repo("r3")
rc, _, done = run_hook(main3, env_with(None))
time.sleep(SETTLE)
check("codegraph not installed: exits 0", rc == 0)
check(
    "codegraph not installed: skips silently and creates no .omc directory",
    done.stdout == "" and done.stderr == "" and not (main3 / ".omc").exists(),
)

print("\npost-merge never returns nonzero")
BIN_F, LOG_F = make_bin("bin-failing")
main4, _l4 = make_repo("r4")
rc, _, _ = run_hook(main4, env_with(BIN_F, FAKE_LOG=LOG_F, FAKE_RC=7))
check("codegraph sync exiting 7: hook still exits 0", rc == 0)
_olog = main4 / ".omc" / "codegraph-sync.log"
check(
    "codegraph sync exiting 7: the failure is recorded in the log, not lost",
    wait_for(LOG_F)
    and any(
        "exit 7" in line
        for line in (
            _olog.read_text().splitlines() if wait_for(_olog, contains="exit 7") else []
        )
    ),
    _olog.read_text() if _olog.exists() else "no log",
)
time.sleep(0.3)
check(
    "codegraph sync exiting 7: the exit code in the log is the real one",
    "exit 7" in (_olog.read_text() if _olog.exists() else ""),
)

main5, _l5 = make_repo("r5")
(main5 / ".omc").write_text("a file where the log directory should be\n")
BIN_U, LOG_U = make_bin("bin-unwritable")
rc, _, _ = run_hook(main5, env_with(BIN_U, FAKE_LOG=LOG_U))
time.sleep(SETTLE)
check(
    "log directory cannot be created: exits 0 and does not sync",
    rc == 0 and calls(LOG_U) == [],
    str(calls(LOG_U)),
)

notrepo = TMP / "not-a-repo"
notrepo.mkdir()
BIN_R, LOG_R = make_bin("bin-notrepo")
rc, _, _ = run_hook(notrepo, env_with(BIN_R, FAKE_LOG=LOG_R))
time.sleep(SETTLE)
check(
    "outside any repository: exits 0 and does not sync", rc == 0 and calls(LOG_R) == []
)

print("\npost-merge through real git: a fast-forward merge triggers it")
BIN_E, LOG_E = make_bin("bin-e2e")
main6, linked6 = make_repo("r6")
(linked6 / "g.txt").write_text("2\n")
git("add", "g.txt", cwd=linked6)
git("commit", "-q", "-m", "feature", cwd=linked6)
merged = subprocess.run(
    ["git", "merge", "--ff-only", "feat"],
    cwd=main6,
    env=env_with(BIN_E, FAKE_LOG=LOG_E, FAKE_RC=3),
    capture_output=True,
    text=True,
)
check(
    "git merge --ff-only in the main checkout succeeds although codegraph fails",
    merged.returncode == 0,
    merged.stderr,
)
check(
    "git merge --ff-only in the main checkout ran the hook's sync",
    wait_for(LOG_E) and calls(LOG_E) == [f"sync --quiet {main6}"],
    str(calls(LOG_E)),
)

BIN_E2, LOG_E2 = make_bin("bin-e2e-linked")
main7, linked7 = make_repo("r7")
git("checkout", "-q", "-b", "side", cwd=linked7)
(linked7 / "h.txt").write_text("3\n")
git("add", "h.txt", cwd=linked7)
git("commit", "-q", "-m", "side", cwd=linked7)
git("checkout", "-q", "-b", "feat2", "feat", cwd=linked7)
merged = subprocess.run(
    ["git", "merge", "--ff-only", "side"],
    cwd=linked7,
    env=env_with(BIN_E2, FAKE_LOG=LOG_E2),
    capture_output=True,
    text=True,
)
time.sleep(SETTLE)
check(
    "the same fast-forward in a linked worktree succeeds and does not sync",
    merged.returncode == 0 and calls(LOG_E2) == [],
    f"{merged.stderr} {calls(LOG_E2)}",
)


print("\nsync-if-stale: is_main_checkout")
check("the main working tree is the main checkout", cg.is_main_checkout(main1))
check("a linked worktree is not", not cg.is_main_checkout(linked1))
check(
    "a directory outside any repository is not (conservative)",
    not cg.is_main_checkout(notrepo),
)
check("a missing directory is not", not cg.is_main_checkout(TMP / "nope"))

STALE = {
    "initialized": True,
    "lastIndexed": "2026-10-21T00:00:00Z",
    "fileCount": 3,
    "pendingChanges": {"added": 0, "modified": 2, "removed": 0},
    "worktreeMismatch": None,
}
FRESH = dict(STALE, pendingChanges={"added": 0, "modified": 0, "removed": 0})


def with_path(bin_dir):
    class _Ctx:
        def __enter__(self):
            self.old = os.environ.get("PATH")
            os.environ["PATH"] = f"{bin_dir}:{SYSTEM_PATH}" if bin_dir else SYSTEM_PATH

        def __exit__(self, *exc):
            os.environ["PATH"] = self.old

    return _Ctx()


def scenario(name, repo, status, fixes=False, sync_timeout=60, **fake_env):
    """Run cg.sync_if_stale(repo) against a fake codegraph; return (line, sync calls, all calls).

    `fixes` makes the fake's `sync` replace the status with a clean one, as a real sync does.
    """
    bin_dir, log = make_bin(name)
    sfile = TMP / (name + "-status.json")
    sfile.write_text(
        json.dumps(dict(status, projectPath=status.get("projectPath", str(repo))))
    )
    env = dict(FAKE_LOG=log, FAKE_STATUS=sfile, **fake_env)
    if fixes:
        fixed = TMP / (name + "-fixed.json")
        fixed.write_text(json.dumps(dict(FRESH, projectPath=str(repo))))
        env["FAKE_FIXED_STATUS"] = fixed
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        with with_path(bin_dir):
            line = cg.sync_if_stale(repo, sync_timeout=sync_timeout)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return line, [c for c in calls(log) if c.startswith("sync")], calls(log)


print("\nsync-if-stale: syncs only on CG-02, in the main checkout")
line, syncs, _all = scenario("s-stale", main1, STALE, fixes=True)
check(
    "CG-02 in the main checkout: exactly one sync, quiet, on the main path",
    syncs == [f"sync --quiet {main1}"],
    str(_all),
)
check(
    "CG-02 in the main checkout: one-line result says synced",
    line == "codegraph synced: CG-02 cleared",
    line,
)

line, syncs, _all = scenario("s-fresh", main1, FRESH)
check("fresh index: no sync", syncs == [] and "no sync" in line, f"{line} {_all}")

for _name, _status in (
    ("s-cg01", {"initialized": False}),
    ("s-cg03", dict(FRESH, worktreeMismatch="/elsewhere")),
    ("s-cg04", dict(FRESH, lastIndexed=None)),
):
    line, syncs, _all = scenario(_name, main1, _status)
    check(
        f"{_name[2:].upper()} alone (sync cannot clear it): no sync",
        syncs == [],
        f"{line} {_all}",
    )

line, syncs, _all = scenario(
    "s-cg03path", main1, dict(STALE, projectPath="/some/other/project")
)
check(
    "stale AND indexing a different project: still one sync (CG-02 is present; CG-03 is reported separately)",
    len(syncs) == 1,
    f"{line} {_all}",
)

line, syncs, _all = scenario("s-linked", linked1, STALE)
check(
    "CG-02 in a linked worktree: no sync and no status probe at all",
    syncs == [] and _all == [] and "not the main checkout" in line,
    f"{line} {_all}",
)

line, syncs, _all = scenario("s-failrc", main1, STALE, FAKE_RC=5)
check(
    "sync exits nonzero: reported in one line, no exception",
    len(syncs) == 1 and "failed (exit 5)" in line,
    line,
)

t0 = time.monotonic()
line, syncs, _all = scenario("s-timeout", main1, STALE, sync_timeout=1, FAKE_SLEEP=5)
_took = time.monotonic() - t0
check(
    "sync that hangs is cut off at the timeout and reported",
    len(syncs) == 1 and "timed out after 1s" in line and _took < 4,
    f"{line} {_took:.1f}s",
)

line, syncs, _all = scenario("s-persist", main1, STALE)
check(
    "sync that ran but left CG-02 standing is not reported as success",
    len(syncs) == 1 and "CG-02 remains" in line,
    line,
)

with with_path(None):
    line = cg.sync_if_stale(main1)
check(
    "codegraph missing: one line, no exception",
    "unavailable" in line and "\n" not in line,
    line,
)

line, syncs, _all = scenario("s-statusfail", main1, STALE, FAKE_STATUS_RC=1)
check(
    "status probe fails: no sync",
    syncs == [] and "unavailable" in line,
    f"{line} {_all}",
)

print("\nsync-if-stale: one sync at a time")
main_l, _ll = make_repo("rlock")
(main_l / ".omc" / "codegraph-sync.lock").mkdir(parents=True)
line, syncs, _all = scenario("s-locked", main_l, STALE)
check(
    "lock held: no sync, and the line says why",
    syncs == [] and line == "codegraph sync skipped: another sync holds the lock",
    f"{line} {_all}",
)
check(
    "lock held: the holder's lock is not removed",
    (main_l / ".omc" / "codegraph-sync.lock").is_dir(),
)
_old = time.time() - 600
os.utime(main_l / ".omc" / "codegraph-sync.lock", (_old, _old))
line, syncs, _all = scenario("s-stalelock", main_l, STALE, fixes=True)
check(
    "lock older than 5 minutes (dead holder): broken, one sync runs",
    len(syncs) == 1 and "synced" in line,
    f"{line} {_all}",
)
check(
    "the lock is released after a sync",
    not (main_l / ".omc" / "codegraph-sync.lock").exists(),
)
line, syncs, _all = scenario("s-lockfail", main_l, STALE, FAKE_RC=5)
check(
    "the lock is released after a failed sync too",
    not (main_l / ".omc" / "codegraph-sync.lock").exists(),
    line,
)
line, syncs, _all = scenario("s-lockfresh", main_l, FRESH)
check(
    "a fresh index takes no lock",
    not (main_l / ".omc" / "codegraph-sync.lock").exists() and syncs == [],
)

print("\nsync-if-stale: the command contract")
BIN_C, LOG_C = make_bin("bin-cli")
# The script anchors REPO to its own location (paths.py), so run a copy that lives in a scratch
# repository: the real checkout is never probed or synced, whatever tree this test runs in.
main_cli, _lcli = make_repo("rcli")
(main_cli / "scripts").mkdir()
for _f in ("codegraph_freshness.py", "paths.py"):
    shutil.copy2(ROOT / "scripts" / _f, main_cli / "scripts" / _f)
_cli = [
    sys.executable,
    str(main_cli / "scripts" / "codegraph_freshness.py"),
    "--sync-if-stale",
]
_stale_cli = TMP / "cli-status.json"
_stale_cli.write_text(json.dumps(dict(STALE, projectPath=str(main_cli))))
done = subprocess.run(
    _cli,
    cwd=TMP,
    capture_output=True,
    text=True,
    env=env_with(BIN_C, FAKE_LOG=LOG_C, FAKE_RC=9, FAKE_STATUS=_stale_cli),
)
check(
    "--sync-if-stale exits 0 even when codegraph misbehaves",
    done.returncode == 0,
    done.stderr,
)
check(
    "--sync-if-stale prints exactly one line",
    len(done.stdout.splitlines()) == 1,
    repr(done.stdout),
)


ROOT_TOP = git("rev-parse", "--show-toplevel", cwd=ROOT).stdout.strip()
check(
    "--sync-if-stale ran against the scratch repository, not the real checkout",
    calls(LOG_C) == [f"status --json {main_cli}", f"sync --quiet {main_cli}"]
    and all(
        git("rev-parse", "--show-toplevel", cwd=c.rsplit(" ", 1)[1]).stdout.strip()
        != ROOT_TOP
        for c in calls(LOG_C)
    ),
    str(calls(LOG_C)),
)
done = subprocess.run(_cli, cwd=TMP, capture_output=True, text=True, env=env_with(None))
check(
    "--sync-if-stale exits 0 with codegraph absent",
    done.returncode == 0 and len(done.stdout.splitlines()) == 1,
    f"{done.returncode} {done.stdout!r}",
)
check(
    "the default mode is unchanged: UserPromptSubmit still gets the report-only path",
    "--sync-if-stale"
    not in (ROOT / ".claude" / "settings.json").read_text().split('"SessionStart"')[0],
)

print("\nworktree report: no remedy that a worktree must not run (rule 3)")
import contextlib
import io

_CG02 = {
    "initialized": True,
    "lastIndexed": "2026-10-21T00:00:00Z",
    "projectPath": str(ROOT),
    "pendingChanges": {"added": 0, "modified": 1, "removed": 0},
}
_CG03 = dict(_CG02, pendingChanges={}, projectPath="/elsewhere/main")
_RO = "the index describes main and is read-only in a worktree - read the files your branch changed directly"
check(
    "CG-02 in a worktree says read-only, literally",
    cg.evaluate(_CG02, ROOT, main_checkout=False)
    == [
        f"CG-02 codegraph reports 1 uningested change(s) (0 added, 1 modified, 0 removed) - {_RO}"
    ],
    str(cg.evaluate(_CG02, ROOT, main_checkout=False)),
)
check(
    "CG-03 in a worktree says read-only, literally",
    cg.evaluate(_CG03, ROOT, main_checkout=False)
    == [f"CG-03 the index describes '/elsewhere/main', not {str(ROOT)!r} - {_RO}"],
    str(cg.evaluate(_CG03, ROOT, main_checkout=False)),
)
check(
    "in the main checkout the remedies are unchanged",
    cg.evaluate(_CG02, ROOT)[0].endswith("- run `codegraph sync`")
    and cg.evaluate(_CG03, ROOT)[0].endswith(
        "- run `codegraph init .` in this worktree"
    ),
)


def _report_as(main_checkout):
    saved = (cg.is_main_checkout, cg.status_or_reason, sys.argv)
    cg.is_main_checkout = lambda repo=ROOT: main_checkout
    cg.status_or_reason = lambda root=None, timeout=8: (
        dict(_CG02, pendingChanges={"modified": 2}),
        None,
    )
    sys.argv = ["codegraph_freshness.py", "--report"]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cg.main()
    finally:
        cg.is_main_checkout, cg.status_or_reason, sys.argv = saved
    return out.getvalue()


_wt = _report_as(False)
check(
    "the --report output in a worktree never tells an agent to run codegraph sync or init",
    "run `codegraph sync`" not in _wt
    and "codegraph init" not in _wt
    and "read-only in a worktree" in _wt,
    _wt,
)
check(
    "the --report output in the main checkout still says to sync",
    "run `codegraph sync`" in _report_as(True),
)

print("\nsettings.json wiring (literal expectations)")
_settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
_hooks = _settings["hooks"]


def commands(event):
    return [
        (h["command"], h["timeout"])
        for entry in _hooks.get(event, [])
        for h in entry["hooks"]
    ]


_ups = [
    (c, t) for c, t in commands("UserPromptSubmit") if "codegraph_freshness.py" in c
]
check("one UserPromptSubmit freshness hook", len(_ups) == 1, str(_ups))
check(
    "UserPromptSubmit freshness hook timeout is 30",
    _ups and _ups[0][1] == 30,
    str(_ups),
)
check(
    "UserPromptSubmit freshness hook stays report-only: it never syncs",
    _ups and "--sync-if-stale" not in _ups[0][0] and "codegraph sync" not in _ups[0][0],
    str(_ups),
)
_ss = [(c, t) for c, t in commands("SessionStart") if "codegraph_freshness.py" in c]
check("one SessionStart freshness hook", len(_ss) == 1, str(_ss))
check(
    "SessionStart runs --sync-if-stale",
    _ss and "codegraph_freshness.py --sync-if-stale" in _ss[0][0],
    str(_ss),
)
check(
    "SessionStart hook cannot fail the session: command ends `|| true`",
    _ss and _ss[0][0].rstrip().endswith("|| true"),
    str(_ss),
)
# Worst case, uv start-up aside, from literal timeouts (Rule 11: not read back from the scripts).
# sync-if-stale: worktree probe 5 + status 8 + sync 60 + status 8 = 81. The lock is non-blocking.
check(
    "the timeouts the worst case is computed from are the ones in the script",
    (cg.WORKTREE_TIMEOUT, cg.STATUS_TIMEOUT_S, cg.SYNC_TIMEOUT) == (5, 8, 60),
    str((cg.WORKTREE_TIMEOUT, cg.STATUS_TIMEOUT_S, cg.SYNC_TIMEOUT)),
)
check(
    "SessionStart timeout is 120, above the 81s worst case",
    _ss and _ss[0][1] == 120 and 5 + 8 + 60 + 8 == 81 < 120,
    str(_ss),
)
check(
    "SessionStart runs only the freshness sync: nothing else rides on the hook",
    _ss
    and _ss[0][0]
    == 'cd "${CLAUDE_PROJECT_DIR}" && uv run python3 scripts/codegraph_freshness.py --sync-if-stale || true',
    str(_ss),
)
check(
    "SessionStart matcher is `startup`, so resume, clear and compact do not sync",
    [e.get("matcher") for e in _hooks.get("SessionStart", [])] == ["startup"],
)
check(
    "the catch-up step is not wired anywhere",
    "catch_up" not in (ROOT / ".claude" / "settings.json").read_text(),
)

print("\nroot AGENTS.md rule 3 states the automation and the worktree boundary")
_rule6 = next(
    l for l in (ROOT / "AGENTS.md").read_text().splitlines() if l.startswith("3. ")
)
for _needle in (
    "`.githooks/post-merge`",
    "SessionStart",
    "CG-02",
    "linked worktree",
    "never run `codegraph sync` there",
    "read-only",
    "the files your branch changed directly",
):
    check(f"rule 3 mentions {_needle}", _needle in _rule6, _rule6)
check("rule 3 does not mention the catch-up step", "catch_up" not in _rule6)
check(
    "rule 3 no longer tells every agent to sync before, during and after changes",
    "before, during, and after changes" not in _rule6,
)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
