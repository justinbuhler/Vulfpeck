#!/usr/bin/env python3
"""Rule 3's missing half: prove the codegraph index still describes this tree.

Rule 3 keeps the index in step with the main checkout. The read path is
automated - a UserPromptSubmit hook runs this module's report on every turn - and
since 2026-10-21 so is the write path, in the main checkout only: `.githooks/
post-merge` syncs in the background when main moves, and a SessionStart hook runs
`--sync-if-stale` below. A stale index answers confidently about code that no
longer exists, and before the write path was automated nothing noticed.

    CG-01  the index does not exist at all
    CG-02  codegraph reports uningested changes against the working tree
    CG-03  the index was built against a different worktree
    CG-04  the index does not say when it was built

CG-02 is the whole check. It was measured before this module was written, not
assumed: editing a tracked file makes `codegraph status --json` report
`modified: 1` immediately, before the background daemon has synced anything, so
`pendingChanges` is a live content comparison between tree and index rather
than a counter the daemon maintains. A stopped daemon therefore cannot mask
staleness, and this needs no daemon-liveness probe of its own.

The first draft compared each tracked file's mtime against `lastIndexed`,
on the assumption that `pendingChanges` was daemon-maintained. That assumption
was false, and the check was worse than redundant: `codegraph sync` does not
advance `lastIndexed` when content is unchanged, so a bare `touch` raised a
violation no sync could clear. Content is the only honest basis, and codegraph
already computes it.

`evaluate` is pure - it takes a parsed status document and returns violation
strings - so every case below is an ordinary fixture rather than a daemon
someone has to stop.

    uv run python3 scripts/codegraph_freshness.py                  # assert, exit 1 on any violation
    uv run python3 scripts/codegraph_freshness.py --report         # print status, always exit 0
    uv run python3 scripts/codegraph_freshness.py --sync-if-stale  # SessionStart: one line, always exit 0

`--sync-if-stale` syncs only on CG-02 (the one marker `codegraph sync` can clear;
CG-01/03/04 need `codegraph init` or a different tree), only in the main checkout
(the index describes main, so a linked worktree never syncs it), and with a bounded
timeout. It prints one line and never fails the session.
"""

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

from paths import REPO

# Every violation names its own marker, the way RECON-0N and MODEL-0N already do.
MARKERS = ("CG-01", "CG-02", "CG-03", "CG-04")

PENDING_KINDS = ("added", "modified", "removed")

# The SessionStart hook in .claude/settings.json (120s) must outlast this script's worst
# case, uv start-up aside: worktree probe 5 + status 8 + sync 60 + status 8 = 81.
# The sync lock is non-blocking, so it adds nothing to that.
SYNC_TIMEOUT = 60
# One sync of the index at a time: `.githooks/post-merge` (background, no timeout) and the
# SessionStart hook both take this directory under `<repo>/.omc/`. mkdir is atomic in bash
# and Python alike. A holder that died leaves the directory behind, so one older than
# LOCK_STALE_S (> SYNC_TIMEOUT, and the hook mirrors it with -mmin +5) is broken.
LOCK_NAME = "codegraph-sync.lock"
LOCK_STALE_S = 300
WORKTREE_TIMEOUT = 5


# In a linked worktree the index describes main, so neither `codegraph sync` nor
# `codegraph init .` is a remedy there (rule 3); the report must not tell an agent to run them.
READ_ONLY_REMEDY = (
    "the index describes main and is read-only in a worktree - "
    "read the files your branch changed directly"
)


def evaluate(status, repo=REPO, main_checkout=True):
    """Return every freshness violation for one `codegraph status --json` document.

    `main_checkout=False` swaps the remediation text for the worktree one: the facts
    reported are identical, only the instruction differs.
    """
    if not status or not status.get("initialized"):
        return ["CG-01 codegraph is not initialised for this repository"]

    violations = []
    pending = status.get("pendingChanges") or {}
    total = sum(int(pending.get(k) or 0) for k in PENDING_KINDS)
    if total:
        detail = ", ".join(f"{pending.get(k) or 0} {k}" for k in PENDING_KINDS)
        violations.append(
            f"CG-02 codegraph reports {total} uningested change(s) ({detail}) - "
            + ("run `codegraph sync`" if main_checkout else READ_ONLY_REMEDY)
        )

    if status.get("worktreeMismatch"):
        violations.append(
            f"CG-03 index was built against a different worktree: "
            f"{status['worktreeMismatch']!r}"
        )

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
            + (
                "run `codegraph init .` in this worktree"
                if main_checkout
                else READ_ONLY_REMEDY
            )
        )

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
        out = subprocess.run(
            ["codegraph", "status", "--json", str(root)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
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


def is_main_checkout(repo=REPO):
    """True only when `repo` is the main working tree (first `git worktree list` entry).

    Conservative: any doubt (git missing, not a repository, unparsable output) is
    False, because a wrong True syncs an index from a tree it does not describe.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=WORKTREE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    first = out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else ""
    if not first.startswith("worktree "):
        return False
    return Path(first[len("worktree ") :]).resolve() == Path(repo).resolve()


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


def sync_if_stale(repo=REPO, sync_timeout=SYNC_TIMEOUT):
    """Run one `codegraph sync` iff CG-02 is raised in the main checkout; return one line."""
    if not is_main_checkout(repo):
        return (
            "codegraph sync skipped: not the main checkout (the index describes main)"
        )
    status = read_status(repo, timeout=STATUS_TIMEOUT_S)
    if status is None:
        return "codegraph unavailable or not initialised - no sync"
    if "CG-02" not in markers_of(evaluate(status, repo)):
        return "codegraph not stale (CG-02 clear) - no sync"
    lock = acquire_sync_lock(repo)
    if lock is None:
        return "codegraph sync skipped: another sync holds the lock"
    try:
        done = subprocess.run(
            ["codegraph", "sync", "--quiet", str(repo)],
            capture_output=True,
            text=True,
            timeout=sync_timeout,
        )
    except subprocess.TimeoutExpired:
        return f"codegraph sync timed out after {sync_timeout}s - index still stale"
    except OSError as exc:
        return f"codegraph sync could not start ({exc}) - index still stale"
    finally:
        with contextlib.suppress(OSError):
            lock.rmdir()
    if done.returncode != 0:
        return f"codegraph sync failed (exit {done.returncode}) - index still stale"
    after = read_status(repo, timeout=STATUS_TIMEOUT_S)
    if after is not None and "CG-02" in markers_of(evaluate(after, repo)):
        return "codegraph sync ran but CG-02 remains - index still stale"
    return "codegraph synced: CG-02 cleared"


def markers_of(violations):
    """The CG-0N markers a violation list carries, in order."""
    return [v.split()[0] for v in violations]


def main():
    if "--sync-if-stale" in sys.argv[1:]:
        # SessionStart: the contract is one line and exit 0, whatever happens.
        try:
            print(sync_if_stale())
        except Exception as exc:  # noqa: BLE001 - a session must never be blocked here
            print(f"codegraph sync-if-stale error ({type(exc).__name__}) - no sync")
        return 0
    report_only = "--report" in sys.argv[1:]
    status, reason = status_or_reason()
    if status is None:
        # codegraph absent is not a freshness failure - it is rule 3 not applying,
        # the same way the global rule says to skip a repository with no index.
        # A timeout is reported by name so it is not mistaken for absence.
        print(f"{reason} - freshness not asserted")
        return 0

    violations = evaluate(status)
    if violations and not is_main_checkout():
        violations = evaluate(status, main_checkout=False)
    if not violations:
        print(
            f"codegraph fresh: {status.get('fileCount')} files indexed at "
            f"{status.get('lastIndexed')}"
        )
        return 0

    print("codegraph freshness failed:")
    for v in violations:
        print(f"  {v}")
    return 0 if report_only else 1


if __name__ == "__main__":
    sys.exit(main())
