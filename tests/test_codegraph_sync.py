"""Verify checkout-local preparation, main sync, Git lifecycle hooks and bounded cleanup.

The CLI is replaced by a recording fake. Real Git drives checkout and merge
hooks; no authoritative index or repository is changed by these fixtures.
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

cg.LOCK_WAIT_S = 0

HOOK = ROOT / ".githooks" / "post-merge"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="cg-sync-test-")).resolve()
SYSTEM_PATH = "/usr/bin:/bin"
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

FAKE = """#!/bin/bash
# fake codegraph: records argv; `status` prints $FAKE_STATUS; `sync` can fix or fail.
echo "$@" >> "$FAKE_LOG"
case "$1" in
  status) [ -n "$FAKE_FAIL_AFTER_WRITE" ] && [ -f "$FAKE_FAIL_AFTER_WRITE" ] && exit 1
          [ -f "$FAKE_STATUS" ] && cat "$FAKE_STATUS"; exit "${FAKE_STATUS_RC:-0}" ;;
  init)   [ -n "$FAKE_ENV_LOG" ] && printf '%s|%s\\n' "$CODEGRAPH_NO_WATCH" "$CODEGRAPH_FORCE_WATCH" >> "$FAKE_ENV_LOG"
          mkdir -p "${@: -1}/.codegraph"
          [ -n "$FAKE_FIXED_STATUS" ] && cp "$FAKE_FIXED_STATUS" "$FAKE_STATUS"
          exit "${FAKE_RC:-0}" ;;
  sync)   [ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
          [ -n "$FAKE_FIXED_STATUS" ] && cp "$FAKE_FIXED_STATUS" "$FAKE_STATUS"
          [ -n "$FAKE_FAIL_AFTER_WRITE" ] && touch "$FAKE_FAIL_AFTER_WRITE"
          exit "${FAKE_RC:-0}" ;;
esac
exit 0
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)


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
    done = subprocess.run(["bash", str(HOOK)], cwd=cwd, env=env, capture_output=True, text=True)
    return done.returncode, time.monotonic() - t0, done


def wait_for(path, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


SETTLE = 1.5  # a background sync that was going to run has started well inside this (0.6 flaked under load)


print("\nthe hook file itself")
check("post-merge is executable, or git silently ignores it", os.access(HOOK, os.X_OK))
check("post-merge passes bash -n",
      subprocess.run(["bash", "-n", str(HOOK)], capture_output=True).returncode == 0)
_src = HOOK.read_text()
check("post-merge never uses `set -e` (a failing step must not become a failing pull)",
      re.search(r"^\s*set\s+-[a-z]*e", _src, re.M) is None)
check("post-merge ends in an unconditional exit 0", _src.rstrip().splitlines()[-1] == "exit 0")
_tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", ".githooks/post-merge"],
                          capture_output=True, text=True).stdout
check("post-merge is tracked with the executable bit, so a fresh clone is armed",
      _tracked.startswith("100755"), _tracked)
check("the existing hooks are untouched in kind: pre-commit and pre-merge-commit still present",
      (ROOT / ".githooks" / "pre-commit").is_file()
      and (ROOT / ".githooks" / "pre-merge-commit").is_file())

print("\npost-merge: main checkout syncs, in the background")
BIN, LOG = make_bin("bin-ok")
main1, linked1 = make_repo("r1")
rc, _, _ = run_hook(main1, env_with(BIN, FAKE_LOG=LOG))
check("main checkout: hook exits 0", rc == 0)
check("main checkout: codegraph sync ran, quiet, against the main checkout path",
      wait_for(LOG) and calls(LOG) == [f"sync --quiet {main1}"], str(calls(LOG)))
check("main checkout: the run is logged under .omc/, which is gitignored",
      wait_for(main1 / ".omc" / "codegraph-sync.log")
      and subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", ".omc/codegraph-sync.log"]
                         ).returncode == 0)

BIN_SLOW, LOG_SLOW = make_bin("bin-slow")
rc, took, done = run_hook(main1, env_with(BIN_SLOW, FAKE_LOG=LOG_SLOW, FAKE_SLEEP=3))
check("a 3 s sync does not hold the hook: it returns in under 2 s", rc == 0 and took < 2.0, f"{took:.2f}s")
check("the hook leaves nothing on stdout/stderr for git to relay", done.stdout == "" and done.stderr == "")
wait_for(LOG_SLOW)

print("\npost-merge: main-checkout-only guard")
BIN_L, LOG_L = make_bin("bin-linked")
rc, _, done = run_hook(linked1, env_with(BIN_L, FAKE_LOG=LOG_L))
time.sleep(SETTLE)
check("linked worktree (with its own .codegraph/): exits 0", rc == 0)
check("linked worktree (with its own .codegraph/): no sync, so the guard is the reason",
      calls(LOG_L) == [] and (linked1 / ".codegraph").is_dir(), str(calls(LOG_L)))
check("linked worktree: skips silently", done.stdout == "" and done.stderr == "")
check("linked worktree: writes no .omc log", not (linked1 / ".omc").exists())

print("\npost-merge: one sync at a time (.omc/codegraph-sync.lock)")
BIN_K, LOG_K = make_bin("bin-lock")
main_k, _lk = make_repo("rk")
(main_k / ".omc" / "codegraph-sync.lock").mkdir(parents=True)
rc, _, done = run_hook(main_k, env_with(BIN_K, FAKE_LOG=LOG_K))
time.sleep(SETTLE)
check("lock held by another sync: exits 0, silent, and does not sync",
      rc == 0 and calls(LOG_K) == [] and done.stdout == "" and done.stderr == "", str(calls(LOG_K)))
check("lock held by another sync: the holder's lock is left in place",
      (main_k / ".omc" / "codegraph-sync.lock").is_dir())
_old = time.time() - 600
os.utime(main_k / ".omc" / "codegraph-sync.lock", (_old, _old))
rc, _, _ = run_hook(main_k, env_with(BIN_K, FAKE_LOG=LOG_K))
check("lock older than 5 minutes (dead holder): broken, and the sync runs",
      rc == 0 and wait_for(LOG_K) and calls(LOG_K) == [f"sync --quiet {main_k}"], str(calls(LOG_K)))
_deadline = time.monotonic() + 5
while (main_k / ".omc" / "codegraph-sync.lock").exists() and time.monotonic() < _deadline:
    time.sleep(0.05)
check("the lock is released when the background sync ends", not (main_k / ".omc" / "codegraph-sync.lock").exists())

BIN_K2, LOG_K2 = make_bin("bin-lock-overlap")
main_k2, _lk2 = make_repo("rk2")
run_hook(main_k2, env_with(BIN_K2, FAKE_LOG=LOG_K2, FAKE_SLEEP=3))
wait_for(LOG_K2)
run_hook(main_k2, env_with(BIN_K2, FAKE_LOG=LOG_K2, FAKE_SLEEP=3))
time.sleep(SETTLE)
check("two hooks overlapping: exactly one sync runs", calls(LOG_K2) == [f"sync --quiet {main_k2}"], str(calls(LOG_K2)))
time.sleep(2.5)

print("\npost-merge: skips cleanly when there is nothing to sync with")
BIN_N, LOG_N = make_bin("bin-noindex")
main2, _l2 = make_repo("r2", with_index=False)
rc, _, _ = run_hook(main2, env_with(BIN_N, FAKE_LOG=LOG_N))
time.sleep(SETTLE)
check("no .codegraph/: exits 0 and does not sync", rc == 0 and calls(LOG_N) == [], str(calls(LOG_N)))
check("no .codegraph/: creates no .omc directory", not (main2 / ".omc").exists())

assert shutil.which("codegraph", path=SYSTEM_PATH) is None, "precondition: no codegraph on the bare PATH"
main3, _l3 = make_repo("r3")
rc, _, done = run_hook(main3, env_with(None))
time.sleep(SETTLE)
check("codegraph not installed: exits 0", rc == 0)
check("codegraph not installed: skips silently and creates no .omc directory",
      done.stdout == "" and done.stderr == "" and not (main3 / ".omc").exists())

print("\npost-merge never returns nonzero")
BIN_F, LOG_F = make_bin("bin-failing")
main4, _l4 = make_repo("r4")
rc, _, _ = run_hook(main4, env_with(BIN_F, FAKE_LOG=LOG_F, FAKE_RC=7))
check("codegraph sync exiting 7: hook still exits 0", rc == 0)
_olog = main4 / ".omc" / "codegraph-sync.log"
check("codegraph sync exiting 7: the failure is recorded in the log, not lost",
      wait_for(LOG_F) and any("exit 7" in line for line in
                              (_olog.read_text().splitlines() if wait_for(_olog) else [])),
      _olog.read_text() if _olog.exists() else "no log")
time.sleep(0.3)
check("codegraph sync exiting 7: the exit code in the log is the real one",
      "exit 7" in (_olog.read_text() if _olog.exists() else ""))

main5, _l5 = make_repo("r5")
(main5 / ".omc").write_text("a file where the log directory should be\n")
BIN_U, LOG_U = make_bin("bin-unwritable")
rc, _, _ = run_hook(main5, env_with(BIN_U, FAKE_LOG=LOG_U))
time.sleep(SETTLE)
check("log directory cannot be created: exits 0 and does not sync", rc == 0 and calls(LOG_U) == [], str(calls(LOG_U)))

notrepo = TMP / "not-a-repo"
notrepo.mkdir()
BIN_R, LOG_R = make_bin("bin-notrepo")
rc, _, _ = run_hook(notrepo, env_with(BIN_R, FAKE_LOG=LOG_R))
time.sleep(SETTLE)
check("outside any repository: exits 0 and does not sync", rc == 0 and calls(LOG_R) == [])

print("\npost-merge through real git: a fast-forward merge triggers it")
BIN_E, LOG_E = make_bin("bin-e2e")
main6, linked6 = make_repo("r6")
(linked6 / "g.txt").write_text("2\n")
git("add", "g.txt", cwd=linked6)
git("commit", "-q", "-m", "feature", cwd=linked6)
merged = subprocess.run(["git", "merge", "--ff-only", "feat"], cwd=main6,
                        env=env_with(BIN_E, FAKE_LOG=LOG_E, FAKE_RC=3), capture_output=True, text=True)
check("git merge --ff-only in the main checkout succeeds although codegraph fails",
      merged.returncode == 0, merged.stderr)
check("git merge --ff-only in the main checkout ran the hook's sync",
      wait_for(LOG_E) and calls(LOG_E) == [f"sync --quiet {main6}"], str(calls(LOG_E)))

BIN_E2, LOG_E2 = make_bin("bin-e2e-linked")
main7, linked7 = make_repo("r7")
git("checkout", "-q", "-b", "side", cwd=linked7)
(linked7 / "h.txt").write_text("3\n")
git("add", "h.txt", cwd=linked7)
git("commit", "-q", "-m", "side", cwd=linked7)
git("checkout", "-q", "-b", "feat2", "feat", cwd=linked7)
merged = subprocess.run(["git", "merge", "--ff-only", "side"], cwd=linked7,
                        env=env_with(BIN_E2, FAKE_LOG=LOG_E2), capture_output=True, text=True)
time.sleep(SETTLE)
check("the same fast-forward in a linked worktree succeeds and does not sync",
      merged.returncode == 0 and calls(LOG_E2) == [], f"{merged.stderr} {calls(LOG_E2)}")


print("\nsync-if-stale: is_main_checkout")
check("the main working tree is the main checkout", cg.is_main_checkout(main1))
check("a linked worktree is not", not cg.is_main_checkout(linked1))
check("a directory outside any repository is not (conservative)", not cg.is_main_checkout(notrepo))
check("a missing directory is not", not cg.is_main_checkout(TMP / "nope"))

STALE = {"initialized": True, "lastIndexed": "2026-10-21T00:00:00Z", "fileCount": 3,
         "pendingChanges": {"added": 0, "modified": 2, "removed": 0}, "worktreeMismatch": None}
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
    sfile.write_text(json.dumps(dict(status, projectPath=status.get("projectPath", str(repo)))))
    env = dict(FAKE_LOG=log, FAKE_STATUS=sfile, **fake_env)
    if fixes:
        fixed = TMP / (name + "-fixed.json")
        fixed.write_text(json.dumps(dict(FRESH, projectPath=str(repo))))
        env["FAKE_FIXED_STATUS"] = fixed
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        with with_path(bin_dir):
            ready, line = cg.ensure_index(repo, sync_timeout=sync_timeout)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ready, line, [c for c in calls(log) if c.startswith(("sync", "init"))], calls(log)


print("\ncheckout-local index preparation")
ready, line, writes, all_calls = scenario("s-stale", main1, STALE, fixes=True)
check("a stale main index syncs once and is verified", ready and writes == [f"sync --quiet {main1}"], str(all_calls))
ready, line, writes, all_calls = scenario("s-fresh", main1, FRESH)
check("a fresh index is reused without init or sync", ready and writes == [], str(all_calls))
ready, line, writes, all_calls = scenario("s-linked", linked1, STALE, fixes=True)
check("a stale linked index syncs only its own checkout", ready and writes == [f"sync --quiet {linked1}"], str(all_calls))

for name, status in (
    ("missing", {"initialized": False}),
    ("mismatch", dict(STALE, projectPath=str(main1), worktreeMismatch=str(main1))),
    ("timestamp", dict(FRESH, lastIndexed=None)),
):
    ready, line, writes, all_calls = scenario("repair-" + name, linked1, status, fixes=True)
    check(f"{name}: initialize locally instead of syncing a foreign or incomplete index",
          ready and writes == [f"init --yes {linked1}"], str(all_calls))

init_env = TMP / "init-env.log"
ready, line, writes, all_calls = scenario("s-no-watch", linked1, {"initialized": False},
                                         fixes=True, CODEGRAPH_NO_WATCH="1", FAKE_ENV_LOG=init_env)
check("init cannot install shared fallback hooks through inherited watch settings",
      ready and init_env.read_text().strip() == "|1", init_env.read_text())

missing = TMP / "r1-nested" / "new checkout"
git("worktree", "add", "-q", "--detach", str(missing), cwd=main1)
ready, line, writes, all_calls = scenario("s-new", missing, dict(FRESH, projectPath=str(main1)), fixes=True)
check("new checkout initializes its own index before any parent status lookup",
      ready and all_calls[0] == f"init --yes {missing}" and (missing / ".codegraph").is_dir(), str(all_calls))
ready, line, writes, all_calls = scenario("s-unindexed", main2, STALE)
check("an unindexed repository is not opted in by a hook", ready and all_calls == [], str(all_calls))

for name, status, env in (
    ("persistent", STALE, {}),
    ("foreign-after-init", dict(STALE, projectPath="/elsewhere"), {}),
    ("status-unavailable", STALE, {"FAKE_STATUS_RC": 1}),
    ("write-fails", STALE, {"FAKE_RC": 5}),
):
    ready, line, writes, all_calls = scenario("s-" + name, linked1, status, **env)
    check(f"{name}: never claims the index is verified", not ready and "verified" in line, line)

ready, line, writes, all_calls = scenario("s-statusfails-after-sync", linked1, STALE,
                                         FAKE_FAIL_AFTER_WRITE=TMP / "after-sync-failure")
check("a successful sync with an unavailable post-sync status is not success",
      not ready and writes == [f"sync --quiet {linked1}"] and "after sync" in line, line)

foreign = TMP / "foreign-index"
foreign.mkdir()
foreign_index = foreign / ".codegraph"
foreign_index.symlink_to(main1 / ".codegraph", target_is_directory=True)
ready, line = cg.ensure_index(foreign)
check("a symlink to another index is rejected before any index mutation", not ready and "CG-03" in line, line)

(main1 / ".omc" / "codegraph-sync.lock").mkdir(exist_ok=True)
ready, line, writes, all_calls = scenario("s-locked", main1, STALE)
check("a live lock prevents overlapping index writes", not ready and writes == [] and "holds the lock" in line, line)
(main1 / ".omc" / "codegraph-sync.lock").rmdir()
old = time.time() - 600
(main1 / ".omc" / "codegraph-sync.lock").mkdir()
os.utime(main1 / ".omc" / "codegraph-sync.lock", (old, old))
ready, line, writes, all_calls = scenario("s-deadlock", main1, STALE, fixes=True)
check("a dead lock is replaced and the completed operation releases it", ready and not (main1 / ".omc" / "codegraph-sync.lock").exists(), line)
ready, line, writes, all_calls = scenario("s-timeout", main1, STALE, sync_timeout=1, FAKE_SLEEP=5)
check("a bounded operation timeout retains an unverified result and releases its lock",
      not ready and "timed out" in line and not (main1 / ".omc" / "codegraph-sync.lock").exists(), line)

print("\npost-checkout runs through real Git")
checkout = ROOT / ".githooks" / "post-checkout"
check("post-checkout is executable", os.access(checkout, os.X_OK))
check("post-checkout is valid shell", subprocess.run(["bash", "-n", str(checkout)], capture_output=True).returncode == 0)
main_c, linked_c = make_repo("checkout", with_hook=False)
(main_c / ".githooks").mkdir()
shutil.copy2(checkout, main_c / ".githooks/post-checkout")
(main_c / "scripts").mkdir()
for name in ("codegraph_freshness.py", "paths.py"):
    shutil.copy2(ROOT / "scripts" / name, main_c / "scripts" / name)
git("add", ".githooks", "scripts", cwd=main_c)
git("commit", "-qm", "hook", cwd=main_c)
git("config", "core.hooksPath", ".githooks", cwd=main_c)
BIN_C, LOG_C = make_bin("bin-checkout")
uv = BIN_C / "uv"
uv.write_text(f'#!/bin/bash\nshift; shift; shift; exec "{sys.executable}" "$@"\n')
uv.chmod(0o755)
status_c, fixed_c = TMP / "checkout-status.json", TMP / "checkout-fixed.json"
new_c = main_c / "tmp" / "worktrees" / "created"
status_c.write_text(json.dumps(dict(FRESH, projectPath=str(main_c))))
fixed_c.write_text(json.dumps(dict(FRESH, projectPath=str(new_c))))
env_c = env_with(BIN_C, FAKE_LOG=LOG_C, FAKE_STATUS=status_c, FAKE_FIXED_STATUS=fixed_c)
created = subprocess.run(["git", "worktree", "add", "-q", "-b", "new-feature", str(new_c)],
                         cwd=main_c, env=env_c, capture_output=True, text=True)
check("Git worktree creation initializes before returning and verifies the new root",
      created.returncode == 0 and calls(LOG_C) == [f"init --yes {new_c}", f"status --json {new_c}"], created.stdout + created.stderr + str(calls(LOG_C)))
LOG_C.write_text("")
file_checkout = subprocess.run(["git", "checkout", "--", "f.txt"], cwd=new_c, env=env_c, capture_output=True, text=True)
check("checking out a file does not prepare an index", file_checkout.returncode == 0 and calls(LOG_C) == [], str(calls(LOG_C)))
status_c.write_text(json.dumps(dict(STALE, projectPath=str(new_c))))
LOG_C.write_text("")
branch_checkout = subprocess.run(["git", "checkout", "-q", "-b", "another-feature"], cwd=new_c, env=env_c, capture_output=True, text=True)
check("a branch switch syncs the existing local index without initializing it again",
      branch_checkout.returncode == 0 and calls(LOG_C) == [f"status --json {new_c}", f"sync --quiet {new_c}", f"status --json {new_c}"], branch_checkout.stdout + branch_checkout.stderr + str(calls(LOG_C)))

print("\nindex cleanup retains main and shared Git hooks")
ready, line = cg.remove_local_index(main_c)
check("main's index cannot be removed", not ready and (main_c / ".codegraph").is_dir(), line)
ready, line = cg.remove_local_index(foreign)
check("a foreign symlink cannot be removed", not ready and foreign_index.is_symlink(), line)
node = TMP / "node"
node.write_text('#!/bin/bash\nif [ "${FAKE_STOP_RC:-0}" = 0 ]; then rm -rf "$4/.codegraph"; fi\nexit "${FAKE_STOP_RC:-0}"\n')
node.chmod(0o755)
saved = {key: os.environ.get(key) for key in ("PATH", "FAKE_LOG", "FAKE_STATUS", "FAKE_STOP_RC")}
os.environ.update(PATH=f"{BIN_C}:{SYSTEM_PATH}", FAKE_LOG=str(LOG_C), FAKE_STATUS=str(status_c))
try:
    os.environ["FAKE_STOP_RC"] = "1"
    ready, line = cg.remove_local_index(new_c)
    check("unverified daemon shutdown retains the local index", not ready and (new_c / ".codegraph").is_dir(), line)
    os.environ["FAKE_STOP_RC"] = "2"
    ready, line = cg.remove_local_index(new_c)
    check("a live shared MCP writer defers cleanup without deleting its index",
          ready and "deferred" in line and (new_c / ".codegraph").is_dir(), line)
    os.environ["FAKE_STOP_RC"] = "0"
    ready, line = cg.remove_local_index(new_c)
    check("successful cleanup removes only the local index",
          ready and not (new_c / ".codegraph").exists() and (main_c / ".codegraph").is_dir(), line)
    check("cleanup retains every shared Git hook", (main_c / ".githooks/post-checkout").read_bytes() == checkout.read_bytes())
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print("\nnative writer protocol prevents a cleanup race")
# The installed interpreter runs fake native modules; no real daemon or index is touched.
real_node = Path(shutil.which("codegraph")).resolve().parents[1] / "node"
assert real_node.is_file(), "CodeGraph's installed Node runtime is required for its cleanup adapter test"
protocol = TMP / "native-protocol"
protocol.mkdir()
protocol_root = protocol / "checkout"
protocol_root.mkdir()
trace = protocol / "calls.jsonl"
registry = protocol / "daemon-registry.js"
registry.write_text("exports.stopDaemonAt=async()=>{require('fs').appendFileSync(process.env.PROTOCOL_TRACE,'stop\\n');return {outcome:'no-daemon'}};")
(protocol / "daemon.js").write_text("exports.isProcessAlive=()=>false;")
(protocol / "writer-lock.js").write_text("""
const fs=require('fs'),path=require('path');
exports.getWriterPidPath=(root,name='writer.pid')=>path.join(root,'.codegraph',name);
exports.readWriterLock=()=>null;
exports.tryAcquireWriterLock=(root,mode,name='writer.pid')=>{
  fs.appendFileSync(process.env.PROTOCOL_TRACE,'claim '+name+'\\n');
  return {kind:process.env.PROTOCOL_INTERPOSE==='1'&&name==='writer.pid'?'taken':'acquired'};
};
exports.releaseWriterLock=(root,name='writer.pid')=>fs.appendFileSync(process.env.PROTOCOL_TRACE,'release '+name+'\\n');
""")
# Capture exactly the adapter production sends to its installed Node runtime.
run_saved = cg.subprocess.run
captured = []
status_c.write_text(json.dumps(dict(FRESH, projectPath=str(linked1))))
metadata = {**FRESH, "projectPath": str(linked1)}
status_saved = cg.status_or_reason
which_saved = cg.shutil.which
try:
    cg.status_or_reason = lambda root: (metadata, None)
    cg.shutil.which = lambda name: str(BIN_C / "codegraph")
    def capture_adapter(args, **kwargs):
        if args[0].endswith('/node'):
            captured.append(args[2])
            return subprocess.CompletedProcess(args, 2, '', '')
        return run_saved(args, **kwargs)
    cg.subprocess.run = capture_adapter
    cg.remove_local_index(linked1)
finally:
    cg.subprocess.run, cg.status_or_reason, cg.shutil.which = run_saved, status_saved, which_saved
assert captured, "production cleanup must call the native adapter"
for interpose in ("1", "0"):
    idx = protocol_root / ".codegraph"
    idx.mkdir(exist_ok=True)
    (idx / "sentinel").write_text("index")
    trace.write_text("")
    result = subprocess.run([str(real_node), "-e", captured[0], str(registry), str(protocol_root)],
                            env={**GIT_ENV, "PROTOCOL_TRACE": str(trace), "PROTOCOL_INTERPOSE": interpose},
                            capture_output=True, text=True)
    events = trace.read_text().splitlines()
    if interpose == "1":
        check("a new writer between the stopped check and writer claim retains the index",
              result.returncode == 2 and (idx / "sentinel").is_file()
              and events == ["claim rebuild.pid", "stop", "claim writer.pid", "release rebuild.pid"], result.stderr + str(events))
    else:
        check("deletion holds both native slots and releases them only afterward",
              result.returncode == 0 and not idx.exists()
              and events == ["claim rebuild.pid", "stop", "claim writer.pid", "release writer.pid", "release rebuild.pid"], result.stderr + str(events))

print("\nagent context is bound to the actual checkout")
import contextlib
import importlib.util
import io
spec = importlib.util.spec_from_file_location("blast_radius_advisory", ROOT / ".claude/hooks/blast_radius_advisory.py")
advisory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(advisory)
read_saved, run_saved = cg.read_status, advisory.subprocess.run
explorations = []
def explore(*args, **kwargs):
    explorations.append((args[0], kwargs))
    return subprocess.CompletedProcess(args[0], 0, "- `signal` 1 caller; tests: test_signal.py", "")
try:
    advisory.subprocess.run = explore
    cg.read_status = lambda root: dict(FRESH, projectPath=str(main_c))
    check("the advisory refuses another checkout's index", advisory.blast_radius(["signal"], main1) == [] and explorations == [])
    cg.read_status = lambda root: dict(STALE, projectPath=str(main1))
    check("the advisory refuses stale context", advisory.blast_radius(["signal"], main1) == [] and explorations == [])
    cg.read_status = lambda root: dict(FRESH, projectPath=str(main1))
    check("the advisory reads source and call paths only for its verified checkout",
          advisory.blast_radius(["signal"], main1) and explorations[0][0][0:4] == ["codegraph", "explore", "--path", str(main1)])
finally:
    cg.read_status, advisory.subprocess.run = read_saved, run_saved

stdin_saved, argv_saved, ensure_saved = sys.stdin, sys.argv, cg.ensure_index
git_dir_saved = os.environ.get("GIT_DIR")
seen = []
try:
    os.environ["GIT_DIR"] = str(main_c / ".git")
    sys.stdin = io.StringIO(json.dumps({"cwd": str(new_c / "scripts")}))
    sys.argv = ["codegraph_freshness.py", "--ensure", "--hook"]
    cg.ensure_index = lambda repo: (seen.append(repo) is None, "prepared")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cg.main()
    check("hook event cwd wins over the launch project and inherited GIT_DIR",
          rc == 0 and seen == [new_c.resolve()], str(seen))
finally:
    sys.stdin, sys.argv, cg.ensure_index = stdin_saved, argv_saved, ensure_saved
    if git_dir_saved is None:
        os.environ.pop("GIT_DIR", None)
    else:
        os.environ["GIT_DIR"] = git_dir_saved

print("\nconfiguration and lander caller wiring")
settings = json.loads((ROOT / ".claude/settings.json").read_text())
for event in ("UserPromptSubmit", "SessionStart"):
    commands = [hook["command"] for entry in settings["hooks"][event] for hook in entry["hooks"] if "codegraph_freshness.py" in hook["command"]]
    check(f"{event} selects the actual event checkout", len(commands) == 1 and "--hook" in commands[0], str(commands))
lander = (ROOT / "scripts/land.sh").read_text()
check("the lander prepares main before creating its persistent worktree",
      lander.index('prepare_codegraph "$MAIN"', lander.index('else\n  rm -rf "$LWT"')) < lander.index('git worktree add --detach "$LWT"'))
check("the lander prepares the merged index before running its gate",
      lander.index('prepare_codegraph "$LWT"', lander.index('while :; do')) < lander.index('( cd "$LWT" && env -u VERIFY_LINT_ONLY'))
check("candidate cleanup follows verified reachability and precedes the receipt",
      lander.index('git merge-base --is-ancestor "$CANDIDATE" main', lander.index('if [ "$swapped" = 0 ]')) < lander.index('cleanup_codegraph "$CALLER"') < lander.index('say "✓ LANDED'))
check("only the caller and own lander are cleaned", lander.count('cleanup_codegraph "') == 2)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
shutil.rmtree(TMP)
sys.exit(1 if FAIL else 0)
