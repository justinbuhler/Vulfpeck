#!/usr/bin/env python3
"""A read-only agent must leave the main checkout exactly as it found it.

ORCH-03: reviewers never mutate repository or git state. Predicting mutation
from a command line cannot be complete (`sed -i`, `tee`, `python -c`,
`git update-ref` all get past a blocklist), so this detects it instead:

    SubagentStart  a read-only agent starts: snapshot the main checkout
    SubagentStop   it stops: snapshot again; any difference blocks the stop and
                   tells the agent what changed, once

Read-only agent types are fnmatch patterns in harness.toml [roles] read_only.
The snapshot covers what a reviewer must not touch: the checkout's HEAD and the
branch it is on, local `main`, every other ref, the local git config (a
`core.hooksPath` change switches gates off), and every tracked or untracked
path's status. Branches checked out in other worktrees are left out: a writer
committing in its own worktree is expected, so running one in parallel does not
trip this. A parent session editing the checkout while the
reviewer runs does, which is why the message says the checkout changed while
the agent ran, not that the agent changed it.

A read-only agent that stops with no snapshot (SubagentStart failed or never
fired) cannot be verified. That is said out loud and recorded, never passed
silently.

Snapshots live in <git common dir>/harness/snapshots/<agent_id>.json.
"""
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATUS_LINES = 200
# Stop decisions that end one run of an agent (see logged_snapshot).
FINISHED = ("pass", "block-overridden", "unverified")
# The floor when harness.toml cannot be read: an unreadable config must not
# switch the guard off.
DEFAULT_READ_ONLY = ["dual-derive", "live-proof-auditor", "reviewer-*", "Explore", "Plan"]


def record(*args, **kwargs):
    """Log this decision (scripts/harness_events.py). Logging never changes it:
    any failure here, including a failed import, is swallowed."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_events
        harness_events.record(*args, **kwargs)
    except Exception:
        pass


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def read_only(agent_type, cwd, floor=False):
    """Return whether this agent type is declared read-only for the repository.

    floor=True also counts DEFAULT_READ_ONLY, for decisions taken after the
    agent ran, when harness.toml may be the agent's own edit.
    """
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_config
        top = (git(cwd, "rev-parse", "--show-toplevel") or "").strip() or cwd
        patterns = list(harness_config.load(top)["roles"]["read_only"])
    except Exception:
        patterns = list(DEFAULT_READ_ONLY)
    if floor:
        patterns += DEFAULT_READ_ONLY
    return bool(agent_type) and any(fnmatch.fnmatchcase(agent_type, p) for p in patterns)


def writer_branches(top):
    """Return the branch refs checked out in linked worktrees other than top.

    A writer agent commits on its own worktree's branch while a reviewer runs;
    that is expected, so those refs are left out of the comparison.
    """
    out, path = set(), None
    for line in (git(top, "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line.startswith("branch ") and path and Path(path).resolve() != Path(top).resolve():
            out.add(line[7:])
    return out


def refs(top):
    """Return {ref: sha} for every ref."""
    found = {}
    for line in (git(top, "for-each-ref", "--format=%(refname) %(objectname)") or "").splitlines():
        name, _, sha = line.partition(" ")
        found[name] = sha
    return found


def gitdir_state(top):
    """Return a hash of git-dir state outside refs and config that a reviewer could
    use to change later behaviour: info/exclude (hides untracked files from status),
    the hooks directory, and which worktrees exist."""
    common = Path((git(top, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip())
    digest = hashlib.sha256()
    exclude = common / "info" / "exclude"
    digest.update(exclude.read_bytes() if exclude.is_file() else b"-")
    hooks = common / "hooks"
    for f in sorted(hooks.iterdir()) if hooks.is_dir() else []:
        digest.update(f.name.encode() + b"\0" + (f.read_bytes() if f.is_file() else b"<dir>") + b"\0")
    worktrees = [line for line in (git(top, "worktree", "list", "--porcelain") or "").splitlines()
                 if line.startswith("worktree ")]
    digest.update("\n".join(sorted(worktrees)).encode())
    return digest.hexdigest()


def is_ancestor(top, old, new):
    return git(top, "merge-base", "--is-ancestor", old, new) is not None


def content_state(top):
    """Fingerprint bytes, link targets, modes and staged identities."""
    listed = git(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    staged = git(top, "ls-files", "--stage", "-z")
    if listed is None or staged is None:
        raise ValueError("cannot read checkout content/index identities")
    contents = {}
    for name in sorted(set(listed.split("\0")) - {""}):
        path = Path(top) / name
        if not path.exists() and not path.is_symlink():
            contents[name] = "absent"
            continue
        mode = path.lstat().st_mode
        raw = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes() if path.is_file() else b"<directory>"
        contents[name] = hashlib.sha256(str(mode).encode() + b"\0" + raw).hexdigest()
    return contents, hashlib.sha256(staged.encode()).hexdigest()


def snapshot(cwd):
    """Return the state of the checkout at cwd that a read-only agent must keep."""
    top = (git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return None
    status = git(top, "status", "--porcelain=v1", "--untracked-files=all") or ""
    lines = sorted(status.splitlines())
    config = git(top, "config", "--local", "--list") or ""
    contents, index_sha = content_state(top)
    return {
        "refs": refs(top),
        "contents": contents,
        "index_sha": index_sha,
        "writer_branches": sorted(writer_branches(top)),
        "gitdir_sha": gitdir_state(top),
        "config_sha": hashlib.sha256(config.encode()).hexdigest(),
        "top": top,
        "head": (git(top, "rev-parse", "HEAD") or "").strip(),
        "branch": (git(top, "symbolic-ref", "--quiet", "--short", "HEAD") or "").strip(),
        "main": (git(top, "rev-parse", "--verify", "-q", "refs/heads/main") or "").strip(),
        "status_sha": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
        "status": lines[:STATUS_LINES],
    }


def differences(before, after):
    """Return (changes that block, changes only reported) between two snapshots.

    A branch checked out in another worktree at the start may advance: a writer
    committing there is expected. Its fast-forward is reported, never silent;
    any other move of it (rewound, reset elsewhere, deleted) blocks like any ref.
    """
    out, noted = [], []
    top = before.get("top")
    writers = set(before.get("writer_branches", []))
    for key, label in (("head", "HEAD"), ("branch", "checked-out branch"), ("main", "local main")):
        if before.get(key) != after.get(key):
            out.append(f"{label}: {(before.get(key) or '-')[:12]} -> {(after.get(key) or '-')[:12]}")
    was, now = before.get("refs", {}), after.get("refs", {})
    moved = sorted(r for r in set(was) | set(now)
                   if was.get(r) != now.get(r) and r != "refs/heads/main" and not (
                       before.get("branch") and r == "refs/heads/" + before["branch"]))
    advanced = [r for r in moved if r in writers and was.get(r) and now.get(r)
                and is_ancestor(top, was[r], now[r])]
    moved = [r for r in moved if r not in advanced]
    if advanced:
        noted.append(f"branches checked out in other worktrees advanced: {', '.join(advanced[:6])}")
    if moved:
        more = f" (+{len(moved) - 6} more)" if len(moved) > 6 else ""
        out.append(f"refs: {', '.join(moved[:6])}{more}")
    if before.get("gitdir_sha") != after.get("gitdir_sha"):
        out.append("git dir changed (info/exclude, hooks, or the worktree list)")
    if before.get("config_sha") != after.get("config_sha"):
        out.append("local git config changed (git config --local --list)")
    if before.get("index_sha") != after.get("index_sha"):
        out.append("index contents changed")
    if before.get("contents") != after.get("contents"):
        was, current = before.get("contents", {}), after.get("contents", {})
        names = sorted(name for name in set(was) | set(current) if was.get(name) != current.get(name))
        out.append("working-tree contents: " + ", ".join(names[:8]))
    if before.get("status_sha") != after.get("status_sha"):
        was, now = set(before.get("status", [])), set(after.get("status", []))
        changed = sorted((now - was) | (was - now))
        shown = ", ".join(line[3:] for line in changed[:8])
        more = f" (+{len(changed) - 8} more)" if len(changed) > 8 else ""
        out.append(f"working tree: {shown or 'status changed'}{more}")
    return out, noted


def logged_snapshot(cwd, agent_id):
    """Return (sha recorded when this agent started, problem) from the decision log.

    The snapshot file sits where the agent can write. Its hash goes into the
    hash-chained log at the start, so rewriting the file to match a changed
    checkout is caught unless the agent also rebuilds the whole chain.
    """
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import harness_events
        path = harness_events.log_path(cwd)
        if path is None or not path.is_file():
            return None, "the decision log is missing"
        _, broken, why = harness_events.verify(path)
        if broken:
            return None, f"the decision log chain is broken at line {broken} ({why})"
        mine = [r for r in (json.loads(line) for line in path.read_text().splitlines())
                if r.get("hook") == "readonly_guard" and r.get("agent_id") == agent_id]
        # A resumed agent starts again, so count only this run: the rows after
        # the agent's last finished stop. The hook writes exactly one snapshot
        # row per run; a second is an append by someone else.
        finished = [i for i, r in enumerate(mine) if r.get("decision") in FINISHED]
        run = mine[finished[-1] + 1:] if finished else mine
        found = [r.get("reason", "")[len("sha256:"):] for r in run if r.get("decision") == "snapshot"]
        if len(found) > 1:
            return None, f"{len(found)} snapshot rows are logged for this run; the hook writes one"
        return (found[0], None) if found else (None, "no snapshot was recorded for this agent")
    except Exception as exc:
        return None, f"the decision log is unreadable ({exc})"


def snapshot_path(cwd, agent_id):
    common = (git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip()
    if not common or not agent_id:
        return None
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in agent_id)
    return Path(common) / "harness" / "snapshots" / f"{safe}.json"


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError):
        print("readonly_guard: unreadable hook input, nothing checked", file=sys.stderr)
        return 0
    event = payload.get("hook_event_name")
    agent_type = str(payload.get("agent_type") or "")
    agent_id = str(payload.get("agent_id") or "")
    cwd = payload.get("cwd") or os.getcwd()
    if event not in ("SubagentStart", "SubagentStop"):
        return 0
    path = snapshot_path(cwd, agent_id)
    if path is None:
        return 0
    subject = f"{agent_type}/{agent_id[:12]}"
    # The read-only decision is taken once, at the start. At the stop, a snapshot
    # for this agent is compared whatever harness.toml says now, so an agent that
    # edits the roles while it runs cannot opt itself out (and that edit is
    # itself a change to the checkout). With no snapshot, the default list counts
    # as well, so deleting the snapshot and the role together still reports.
    if event == "SubagentStart" and not read_only(agent_type, cwd):
        return 0
    if event == "SubagentStop" and not path.is_file() and not read_only(agent_type, cwd, floor=True):
        return 0

    if event == "SubagentStart":
        state = snapshot(cwd)
        if state:
            text = json.dumps(state)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            record("readonly_guard", "snapshot", "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                   subject, payload)
        return 0

    if not path.is_file():
        record("readonly_guard", "unverified", "no-snapshot", subject, payload)
        print(json.dumps({"systemMessage":
                          f"readonly_guard: {agent_type} stopped with no start snapshot; "
                          "whether it changed the checkout was NOT verified"}))
        return 0
    text = path.read_text()
    before = json.loads(text)
    after = snapshot(before["top"]) or {}
    changed, noted = differences(before, after)
    logged, problem = logged_snapshot(cwd, agent_id)
    if problem:
        # A snapshot exists, so its row was written at the start. A log that is
        # now missing, broken or doubled cannot vouch for it: block, do not note.
        changed.insert(0, f"the start snapshot cannot be verified: {problem}")
    elif logged != hashlib.sha256(text.encode()).hexdigest():
        changed.insert(0, "the start snapshot was rewritten after it was recorded")
    if not changed:
        path.unlink()
        record("readonly_guard", "pass", "unchanged", subject, payload)
        if noted:
            print(json.dumps({"systemMessage": f"readonly_guard: {agent_type}: " + "; ".join(noted)}))
        return 0
    if payload.get("stop_hook_active"):
        # Already told once. Let it stop, keep the snapshot as evidence, and make
        # sure the parent session sees what happened.
        record("readonly_guard", "block-overridden", "changed", subject, payload)
        print(json.dumps({"systemMessage":
                          f"readonly_guard: the main checkout still differs after read-only "
                          f"agent {agent_type} stopped: " + "; ".join(changed + noted)}))
        return 0
    record("readonly_guard", "block", "changed", subject, payload)
    print(json.dumps({"decision": "block", "reason":
                      f"You are a read-only agent ({agent_type}), and the main checkout "
                      f"{before['top']} changed while you ran: " + "; ".join(changed)
                      + ". If you made these changes, revert exactly them, then stop. "
                      "If you did not, say so in your report."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
