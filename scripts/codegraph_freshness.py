#!/usr/bin/env python3
"""Keep CodeGraph indexes local to the Git checkout whose files they describe.

CG-01 detects an absent index, CG-02 uningested content, CG-03 a different
checkout, and CG-04 a missing index timestamp. Identity is checked independently
of CodeGraph's own worktree-mismatch flag because a nested worktree without an
index can otherwise resolve upward to main.

`--ensure` initializes a missing local index when this repository already opted
in, syncs changed content, and verifies every marker before work begins.
`--ensure-main` prepares the primary checkout before creating a worktree.
`--cleanup` removes only a verified linked checkout's index after integration.
`--sync-if-stale` reports preparation failures without failing SessionStart.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from paths import REPO

# Every violation names its own marker, the way RECON-0N and MODEL-0N already do.
MARKERS = ("CG-01", "CG-02", "CG-03", "CG-04")

PENDING_KINDS = ("added", "modified", "removed")

# The SessionStart hook in .claude/settings.json (120s) must outlast this script's worst
# case, uv start-up aside: Git probe 5 + lock wait 20 + two status calls 16 + sync 60 = 101.
SYNC_TIMEOUT = 60
# One sync of the index at a time: `.githooks/post-merge` (background, no timeout) and the
# SessionStart hook both take this directory under `<repo>/.omc/`. mkdir is atomic in bash
# and Python alike. A holder that died leaves the directory behind, so one older than
# LOCK_STALE_S (> SYNC_TIMEOUT, and the hook mirrors it with -mmin +5) is broken.
LOCK_NAME = "codegraph-sync.lock"
LOCK_STALE_S = 300
LOCK_WAIT_S = 20
WORKTREE_TIMEOUT = 5


def evaluate(status, repo=REPO):
    """Return every freshness violation for one `codegraph status --json` document."""
    if not status or not status.get("initialized"):
        return ["CG-01 codegraph is not initialised for this repository"]

    violations = []
    pending = status.get("pendingChanges") or {}
    total = sum(int(pending.get(k) or 0) for k in PENDING_KINDS)
    if total:
        detail = ", ".join(f"{pending.get(k) or 0} {k}" for k in PENDING_KINDS)
        violations.append(
            f"CG-02 codegraph reports {total} uningested change(s) ({detail}) - "
            + "run `codegraph sync` in this checkout")

    if status.get("worktreeMismatch"):
        violations.append(
            f"CG-03 index was built against a different worktree: "
            f"{status['worktreeMismatch']!r}")

    # Derived here rather than read out of the status document. Asking codegraph
    # whether codegraph indexed the wrong tree makes this control depend on the
    # thing it checks, and the delegated signal does not fire for the case that
    # actually happens. Measured 2026-09-02: from a worktree with no local index,
    # codegraph resolved UPWARD to a parent directory's project and reported
    # worktreeMismatch=absent, so this gate passed green against an unrelated
    # 724-file Go/Java/JavaScript codebase. projectPath vs REPO is computable
    # locally and catches it without trusting the tool's own verdict.
    indexed = status.get("projectPath")
    if not indexed:
        violations.append("CG-03 the index does not record which project it describes")
    elif Path(indexed).resolve() != Path(repo).resolve():
        violations.append(
            f"CG-03 the index describes {str(indexed)!r}, not {str(repo)!r} - "
            + "verify index ownership before explicit recovery")

    if not status.get("lastIndexed"):
        violations.append("CG-04 the index does not record when it was built")
    return violations


# The UserPromptSubmit hook in .claude/settings.json kills this script at 30s
# and discards its output, so the status call must give up well inside that
# budget (uv start-up takes a few seconds of it) and say so, instead of being
# killed silently. 60s here once let a slow `codegraph status` under load erase
# the freshness report with no trace.
STATUS_TIMEOUT_S = 8


def status_or_reason(root=None, timeout=STATUS_TIMEOUT_S):
    """Return (status document, None) or (None, why it is unavailable)."""
    root = root or REPO
    try:
        out = subprocess.run(["codegraph", "status", "--json", str(root)],
                             cwd=root, env=git_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"codegraph status timed out after {timeout}s"
    except OSError:
        return None, "codegraph unavailable or not initialised"
    if out.returncode != 0:
        return None, "codegraph unavailable or not initialised"
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError:
        return None, "codegraph status returned unreadable output"


def read_status(root=None, timeout=STATUS_TIMEOUT_S):
    """Return the parsed `codegraph status --json`, or None when unavailable."""
    return status_or_reason(root, timeout)[0]


def git_env():
    """Return the current environment without inherited Git repository overrides."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def main_checkout(repo=REPO):
    """Return the canonical primary checkout, or None if Git cannot identify it."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                             env=git_env(), capture_output=True, text=True, timeout=WORKTREE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    roots = [Path(line[len("worktree "):]).resolve() for line in out.stdout.splitlines()
             if line.startswith("worktree ")] if out.returncode == 0 else []
    return roots[0] if Path(repo).resolve() in roots else None


def is_main_checkout(repo=REPO):
    """Return whether repo is the primary working tree."""
    return main_checkout(repo) == Path(repo).resolve()


def acquire_sync_lock(repo=REPO):
    """Take the non-blocking sync lock; return its path, or None when another sync holds it."""
    lock = Path(repo) / ".omc" / LOCK_NAME
    try:
        lock.parent.mkdir(exist_ok=True)
        for _ in range(2):
            try:
                lock.mkdir()
                return lock
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
                        return None
                    lock.rmdir()  # stale: its holder died
                except OSError:
                    return None
    except OSError:
        return None
    return None


def ensure_index(repo=REPO, sync_timeout=SYNC_TIMEOUT):
    """Prepare and verify this checkout's own index without using a parent's index.

    Args:
        repo (Path): Git checkout root.
        sync_timeout (int): Maximum duration of one index operation.

    Returns:
        (bool): Whether the checkout is ready or indexing is not enabled.
        (str): Operation result or the failure that prevented verification.
    """
    repo = Path(repo).resolve()
    index = repo / ".codegraph"
    main = main_checkout(repo)
    if index.is_symlink():
        return False, f"CG-03 refusing a linked index at {index}"
    if index.exists() and (main is None or not index.is_dir()):
        return False, f"CG-03 index is not at a Git checkout root: {repo}"
    if not index.is_dir() and not (main and (main / ".codegraph").is_dir()):
        return True, "codegraph not enabled - no index"
    until = time.monotonic() + LOCK_WAIT_S
    lock = acquire_sync_lock(repo)
    while lock is None and time.monotonic() < until:
        time.sleep(0.1)
        lock = acquire_sync_lock(repo)
    if lock is None:
        return False, "codegraph preparation skipped: another index operation holds the lock"
    try:
        action = "init"
        if index.is_dir():
            status, reason = status_or_reason(repo)
            if status is None:
                return False, f"{reason} - index not verified"
            violations = evaluate(status, repo)
            if not violations:
                return True, "codegraph fresh: local index verified"
            if any(m in markers_of(violations) for m in ("CG-01", "CG-03", "CG-04")):
                return False, "codegraph index not verified; local index retained for manual recovery: " + "; ".join(violations)
            action = "sync"
        args = ["codegraph", action, "--yes" if action == "init" else "--quiet", str(repo)]
        env = git_env()
        if action == "init":
            env.pop("CODEGRAPH_NO_WATCH", None)
            env["CODEGRAPH_FORCE_WATCH"] = "1"  # init must not install fallback shared Git hooks
        done = subprocess.run(args, cwd=repo, env=env, capture_output=True,
                              text=True, timeout=sync_timeout)
        if done.returncode:
            return False, f"codegraph {action} failed (exit {done.returncode}) - index not verified"
        after, reason = status_or_reason(repo)
        if after is None:
            return False, f"{reason} after {action} - index not verified"
        violations = evaluate(after, repo)
        if violations:
            return False, "codegraph index not verified: " + "; ".join(violations)
        return True, f"codegraph {action} completed: local index verified"
    except subprocess.TimeoutExpired:
        return False, f"codegraph preparation timed out after {sync_timeout}s - index not verified"
    except OSError as exc:
        return False, f"codegraph preparation failed ({exc}) - index not verified"
    finally:
        with contextlib.suppress(OSError):
            lock.rmdir()


def sync_if_stale(repo=REPO, sync_timeout=SYNC_TIMEOUT):
    """Return the SessionStart result of preparing this checkout's own index."""
    return ensure_index(repo, sync_timeout)[1]


def remove_local_index(repo=REPO):
    """Remove a verified linked checkout's index while retaining shared Git hooks."""
    repo = Path(repo).resolve()
    main = main_checkout(repo)
    index = repo / ".codegraph"
    if main is None or repo == main or index.is_symlink():
        return False, f"CG-03 refusing index cleanup at {repo}"
    if not index.exists():
        return True, "codegraph local index already absent"
    lock = acquire_sync_lock(repo)
    if lock is None:
        return False, "codegraph cleanup skipped: another index operation holds the lock"
    try:
        status, reason = status_or_reason(repo)
        if status is None:
            return False, f"{reason} - local index retained"
        if "CG-03" in markers_of(evaluate(status, repo)) or not status.get("initialized"):
            return False, "CG-03 local index ownership not verified - index retained"
        executable = shutil.which("codegraph")
        if not executable:
            return False, "codegraph unavailable - local index retained"
        bundle = Path(executable).resolve().parents[1]
        # shortcut: installed internal layout; replace when CodeGraph has a public stop CLI.
        stop = """
const base=require('path').dirname(process.argv[1]), root=process.argv[2], fs=require('fs');
const {stopDaemonAt}=require(process.argv[1]);
const {readWriterLock,getWriterPidPath,tryAcquireWriterLock,releaseWriterLock}=require(base+'/writer-lock.js');
const {isProcessAlive}=require(base+'/daemon.js');
const unknown=name=>fs.existsSync(getWriterPidPath(root,name))&&!readWriterLock(root,name);
(async()=>{
  if(unknown('rebuild.pid')){process.exitCode=1;return;}
  const rebuild=tryAcquireWriterLock(root,'rebuild','rebuild.pid');
  if(rebuild.kind==='taken'){process.exitCode=2;return;}
  try {
    const stopped=await stopDaemonAt(root,{preserveUnverified:true});
    if(!['no-daemon','not-running','term','kill'].includes(stopped.outcome)){process.exitCode=1;return;}
    const active=readWriterLock(root);
    if(active&&active.pid>0&&isProcessAlive(active.pid)){process.exitCode=2;return;}
    if(unknown('writer.pid')){process.exitCode=1;return;}
    const writer=tryAcquireWriterLock(root,'rebuild');
    if(writer.kind==='taken'){process.exitCode=2;return;}
    try { fs.rmSync(require('path').join(root,'.codegraph'),{recursive:true}); }
    finally { releaseWriterLock(root); }
  } finally { releaseWriterLock(root,'rebuild.pid'); }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""

        done = subprocess.run([str(bundle / "node"), "-e", stop,
                               str(bundle / "lib/dist/mcp/daemon-registry.js"), str(repo)],
                              cwd=repo, env=git_env(), capture_output=True, text=True,
                              timeout=SYNC_TIMEOUT)
        if done.returncode == 2:
            return True, f"codegraph cleanup deferred: active MCP writer or rebuild; local index retained at {repo}"
        if done.returncode:
            return False, f"codegraph writer shutdown not verified at {repo} - local index retained"
        return True, "codegraph local index removed; shared Git hooks retained"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"codegraph cleanup failed ({exc}) - local index retained"
    finally:
        with contextlib.suppress(OSError):
            lock.rmdir()


def markers_of(violations):
    """The CG-0N markers a violation list carries, in order."""
    return [v.split()[0] for v in violations]


def main():
    repo = Path(sys.argv[sys.argv.index("--root") + 1]).resolve() if "--root" in sys.argv else REPO
    if "--hook" in sys.argv:
        event = json.load(sys.stdin)
        checkout = subprocess.run(["git", "-C", str(event.get("cwd") or repo),
                                   "rev-parse", "--show-toplevel"], env=git_env(),
                                  capture_output=True, text=True, timeout=WORKTREE_TIMEOUT)
        if checkout.returncode:
            print("codegraph hook checkout unavailable")
            return 1
        repo = Path(checkout.stdout.strip()).resolve()
    if "--ensure-main" in sys.argv:
        repo = main_checkout(repo)
        if repo is None:
            print("codegraph primary checkout unavailable")
            return 1
    if "--ensure" in sys.argv or "--ensure-main" in sys.argv or "--cleanup" in sys.argv:
        ready, message = remove_local_index(repo) if "--cleanup" in sys.argv else ensure_index(repo)
        print(message)
        return 0 if ready else 1
    if "--sync-if-stale" in sys.argv[1:]:
        print(sync_if_stale(repo))
        return 0
    index = repo / ".codegraph"
    if index.is_symlink() or (index.exists() and not index.is_dir()):
        print(f"CG-03 index is not a checkout-local directory: {index}")
        return 1
    if not index.is_dir():
        print("codegraph not enabled - no local index")
        return 0
    report_only = "--report" in sys.argv[1:]
    status, reason = status_or_reason(repo)
    if status is None:
        # codegraph absent is not a freshness failure - it is rule 6 not applying,
        # the same way the global rule says to skip a repository with no index.
        # A timeout is reported by name so it is not mistaken for absence.
        print(f"{reason} - freshness not asserted")
        return 0

    violations = evaluate(status, repo)
    if not violations:
        print(f"codegraph fresh: {status.get('fileCount')} files indexed at "
              f"{status.get('lastIndexed')}")
        return 0

    print("codegraph freshness failed:")
    for v in violations:
        print(f"  {v}")
    return 0 if report_only else 1


if __name__ == "__main__":
    sys.exit(main())
