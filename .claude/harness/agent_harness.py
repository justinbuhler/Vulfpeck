#!/usr/bin/env python3
"""Isolation and serialized commit landing for parallel agent tasks.

Four agents editing one checkout is the failure this removes: they share an
index, a HEAD and one `.git/index.lock`, so one agent's `git add` stages
another's half-written file and the pre-commit gate then reports on a tree
nobody authored. Each task gets its own worktree instead, and no agent runs
`git commit` at all - it enqueues a request, and a single lander commits the
requests one at a time.

State lives under the repository's *common* git directory. That is the one path
every worktree of this repository resolves identically, so the queue and the
ledger are the same files whichever directory an agent runs from:

    <common .git>/agent-harness/
        tasks/<task>.json   manifest, including the baseline SHA
        queue/*.json        commit requests waiting for the lander
        processing/*.json   the request a lander has claimed, with its .owner.json
        done/*.json         requests the lander has consumed
        ledger.jsonl        one LANDED or FAILED record per attempt
        lander.lock         flock held for the whole life of the single lander
        meta.lock           flock held only around short queue/manifest transitions

Subcommands:
    provision   create a worktree for one task from a named baseline commit
    enqueue     drop a commit request for one task
    land        consume the queue, one commit at a time, through the real gate
    reconcile   report landed / queued / orphaned across every task
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from harness_config import load as load_config
from harness_inputs import copy_inputs

# The pre-commit gate runs lint only (see .githooks/pre-commit), the lint command
# from harness.toml, which is seconds once uvx has the tool cached and about a
# minute when it does not. The full suite now runs at the land, so the hour this ceiling used to need is gone with it.
# It is still generous on purpose: a timeout short enough to cut a slow-but-passing
# gate turns a green run into a FAILED ledger row, which is the same lie as masking
# the exit code. Ten minutes covers a cold uvx fetch under load.
# If this hook is ever given the suite back, raise this with it.
GATE_TIMEOUT = 600

# index.lock is held for the length of one git write. Retrying past ~2 minutes
# means the holder died and left it behind, which is a defect to report, not to
# wait out.
LOCK_TRIES = 30
LOCK_BACKOFF = 0.5
LOCK_BACKOFF_MAX = 8.0


# git's per-invocation variables beat both `cwd` and `-C`. A pre-commit hook runs
# with GIT_DIR, GIT_INDEX_FILE and GIT_WORK_TREE set, so anything that shells out
# to git from inside the gate operates on the repository being committed to
# rather than on its own. That is not hypothetical: it moved a branch ref to a
# test fixture commit while the gate printed only "verify failed", so the visible
# signal was an ordinary red gate. Scrub them on every call.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


def clean_env():
    """Return the environment with git's per-invocation variables removed."""
    env = os.environ.copy()
    for name in GIT_ENV_LEAKS:
        env.pop(name, None)
    return env


def die(message):
    """Exit non-zero with one diagnostic on stderr."""
    print(f"harness: {message}", file=sys.stderr)
    raise SystemExit(1)


def now():
    """Return the current UTC instant as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(*args, cwd=None, timeout=120):
    """Run one git command and return its CompletedProcess with the code intact.

    Nothing here inspects `.returncode` on the caller's behalf. Every caller
    decides what a non-zero code means, because "gate failed", "lock held" and
    "nothing to commit" are three different outcomes and the ledger has to tell
    them apart.

    Args:
        *args (str): arguments after `git`.
        cwd (Path): directory to run in, or None for the current one.
        timeout (int): seconds before the call is abandoned.

    Returns:
        (subprocess.CompletedProcess): captured stdout, stderr and exit code.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=clean_env(),
    )


def git_ok(*args, **kwargs):
    """Run one git command that has no meaningful failure mode and return stdout."""
    proc = git(*args, **kwargs)
    if proc.returncode:
        die(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def common_git_dir():
    """Return the git directory shared by every worktree of this repository."""
    return Path(git_ok("rev-parse", "--git-common-dir")).resolve()


def main_checkout():
    """Return the root of the main checkout, the one holding the ignored inputs."""
    return common_git_dir().parent


def state_root():
    """Return the harness state directory, creating its subdirectories."""
    root = common_git_dir() / "agent-harness"
    for name in ("tasks", "queue", "processing", "done"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def write_json(path, payload):
    """Write JSON to a temporary name and rename it into place.

    A reader of `queue/` must never see a half-written request: rename within a
    directory is atomic, a partial write is not.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def meta_lock(root):
    """Hold `meta.lock` for one short state transition.

    Taken after the lander lock, never before it, and never held across a gate,
    a git commit, a sleep or a network call. Closing the descriptor releases it.
    """
    fd = os.open(root / "meta.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# A task name becomes a file name under tasks/ and queue/. Anything that could
# leave those directories (a separator, `..`, a leading dot) is refused.
TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def task_name(value):
    """argparse type: return value if it is a safe task name."""
    if not isinstance(value, str) or not TASK_NAME.fullmatch(value) or ".." in value:
        raise argparse.ArgumentTypeError(
            f"invalid task name {value!r}: letters, digits, '.', '_' and '-' only, "
            "starting with a letter or digit, no '..'"
        )
    return value


def load_manifest(root, task):
    """Return one task manifest, or exit if the task was never provisioned."""
    try:
        task_name(task)
    except argparse.ArgumentTypeError as exc:
        die(str(exc))
    path = root / "tasks" / f"{task}.json"
    if not path.exists():
        die(f"no manifest for task {task!r}; run provision first ({path})")
    return json.loads(path.read_text())


def head_sha(worktree):
    """Return the full HEAD SHA of one worktree, or None if it has no HEAD."""
    proc = git("rev-parse", "HEAD", cwd=worktree)
    return proc.stdout.strip() if proc.returncode == 0 else None


def porcelain(worktree):
    """Return the non-empty lines of `git status --porcelain` for one worktree."""
    proc = git("status", "--porcelain", cwd=worktree)
    if proc.returncode:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def append_ledger(root, record):
    """Append one outcome record to the ledger and flush it to disk.

    The ledger is the only durable claim this harness makes. An unflushed write
    that dies with the process would report nothing at all, which reads as
    "no attempt" rather than "attempt lost".

    `ts` is stamped when the attempt starts and `finished` when its outcome is
    known. Both are needed: with only one of them a row that took two minutes
    and a row that burned the whole gate timeout look identical.
    """
    record.setdefault("finished", now())
    with open(root / "ledger.jsonl", "a") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger(root):
    """Return every ledger record, oldest first."""
    path = root / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- provision ---------------------------------------------------------------


def refuse_if_dirty(path):
    """Exit unless `path` is free for a new worktree.

    A worktree laid over existing content inherits it silently, and the agent's
    first commit then carries work it never made. An existing worktree is
    refused whether or not it is dirty - `git worktree add` cannot reuse it
    either - but the message distinguishes the two, because a dirty one holds
    somebody's uncommitted work and deleting it would lose that work.
    """
    if not path.exists():
        return
    if not path.is_dir():
        die(f"target exists and is not a directory: {path}")
    entries = list(path.iterdir())
    if not entries:
        return
    top = git("rev-parse", "--show-toplevel", cwd=path)
    # A directory inside an enclosing repository is not a worktree of its own.
    if top.returncode or not Path(top.stdout.strip()).samefile(path):
        die(f"target directory is not empty ({len(entries)} entries): {path}")
    dirty = porcelain(path)
    if dirty is None:
        die(
            "target is an existing worktree whose status git cannot read; "
            f"inspect it before removing: {path}"
        )
    if dirty:
        die(
            f"target is an existing worktree with {len(dirty)} uncommitted "
            f"path(s); land or discard them first: {path}"
        )
    die(f"target is an existing clean worktree; provision a new path: {path}")


def clone_ignored_inputs(worktree):
    """Copy the task baseline's declared inputs and return their paths."""
    return [
        item["path"]
        for item in copy_inputs(
            main_checkout(), worktree, load_config(worktree)["land"]["copy_untracked"]
        )
    ]


def cmd_provision(args):
    """Create one task's worktree from an explicitly named baseline commit."""
    root = state_root()
    manifest_path = root / "tasks" / f"{args.task}.json"
    if manifest_path.exists():
        die(f"task {args.task!r} is already provisioned: {manifest_path}")

    # --baseline is required by the parser. Resolving it here, and recording
    # what it resolved to, is what stops a task from silently starting on
    # whatever HEAD happened to be when the agent got round to running.
    baseline_sha = git_ok("rev-parse", "--verify", f"{args.baseline}^{{commit}}")

    path = Path(args.path).expanduser().resolve()
    refuse_if_dirty(path)

    branch = args.branch or f"agent/{args.task}"
    proc = git("worktree", "add", "-b", branch, str(path), baseline_sha)
    if proc.returncode:
        die(f"git worktree add failed ({proc.returncode}): {proc.stderr.strip()}")

    try:
        cloned = [] if args.no_inputs else clone_ignored_inputs(path)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        die(str(exc))

    # An input can depend on an untracked ignore rule in main. Refuse a task
    # worktree made dirty by that missing rule before handing it to a writer.
    dirt = porcelain(path) or []
    if dirt:
        die(
            f"provisioning left {len(dirt)} untracked path(s) in {path}; the "
            f"clone carried content whose ignore rule it did not: "
            f"{', '.join(line[3:] for line in dirt[:5])}. "
            f"Remove that worktree before retrying."
        )

    # The worktree's code is pinned to a commit; its inputs are a snapshot of a
    # checkout that is still being edited. Nothing links the two, because the
    # generated authority under `output/` is gitignored and so cannot be pinned
    # by commit. A gate failure caused by that skew is not the agent's, so the
    # manifest records where the inputs came from and how far off it was.
    source = main_checkout()
    source_dirt = porcelain(source) or []
    source_head = head_sha(source)
    # Something in this environment writes an absolute core.hooksPath into each
    # new worktree's config, pointing at the main checkout. Git does not do that
    # by itself - a clean repository writes no per-worktree config at all - so it
    # is worth saying when it happens: the worktree then gates on main's current
    # hook rather than the one at its own commit, and that hook can change
    # underneath it. Reported rather than corrected, because `git config
    # --worktree` triggers git's init_worktree_config migration, which moves
    # core.bare into the shared config and breaks every linked worktree in a
    # repository that has not migrated yet. That is a bad thing to do as a side
    # effect of provisioning.
    _, gate_hook, _ = resolve_gate(path)
    foreign_gate = gate_hook is not None and not gate_hook.is_relative_to(path)

    manifest = {
        "task": args.task,
        "baseline_ref": args.baseline,
        "baseline_sha": baseline_sha,
        "source_checkout": str(source),
        "source_head": source_head,
        "source_uncommitted_paths": len(source_dirt),
        "gate": str(gate_hook) if gate_hook else None,
        "branch": branch,
        "worktree": str(path),
        # A request carries the generation it was queued under; nothing bumps it yet.
        "generation": 1,
        "cloned_inputs": cloned,
        "created": now(),
    }
    write_json(manifest_path, manifest)
    print(f"provisioned {args.task}")
    print(f"  worktree  {path}")
    print(f"  branch    {branch}")
    print(f"  baseline  {args.baseline} -> {baseline_sha}")
    print(f"  inputs    {len(cloned)} declared files copied from {source}")
    if source_head != baseline_sha:
        print(
            f"  WARNING   inputs come from {source_head[:9]}, "
            f"not the baseline {baseline_sha[:9]}"
        )
    if source_dirt:
        print(
            f"  WARNING   that checkout has {len(source_dirt)} uncommitted "
            f"path(s); a gate failure may be input skew, not this task"
        )
    if foreign_gate:
        print(
            f"  WARNING   this worktree's gate is {gate_hook}, which is outside "
            f"it, so it commits under a hook from another checkout. Clear it "
            f"with: git -C {path} config --worktree --unset core.hooksPath"
        )
    return 0


# --- enqueue -----------------------------------------------------------------


def cmd_enqueue(args):
    """Record one commit request for the lander to consume."""
    root = state_root()
    manifest = load_manifest(root, args.task)
    worktree = Path(manifest["worktree"])

    files = []
    for name in args.file:
        # A request names files, never patterns: `*.txt` or `:(glob)**` would
        # let one request commit paths the ledger never lists.
        if set("*?[") & set(name) or name.startswith(":"):
            die(f"--file {name!r} is a pattern; name each file literally")
        resolved = (Path.cwd() / name).resolve()
        # The containment check is the cross-contamination boundary. An agent
        # that names a path in a sibling worktree is refused here, before the
        # lander ever has a chance to stage it.
        if not resolved.is_relative_to(worktree):
            die(f"{resolved} is outside task {args.task!r}'s worktree {worktree}")
        # A directory (or `.`) is a pattern in disguise: git stages everything under it.
        if resolved == worktree or resolved.is_dir():
            die(f"--file {name!r} is a directory; name each file literally")
        files.append(str(resolved.relative_to(worktree)))
    if not files:
        die("a commit request must name at least one --file")

    name = f"{time.time_ns()}-{args.task}-{secrets.token_hex(4)}.json"
    with meta_lock(root):
        # Re-read under the lock: the task may have been re-provisioned or removed
        # while the paths above were being checked.
        current = load_manifest(root, args.task)
        for field in ("worktree", "branch", "baseline_sha"):
            if current.get(field) != manifest.get(field):
                die(f"task {args.task!r} manifest {field} changed while queueing")
        if "generation" not in current:
            die(f"task {args.task!r} manifest has no generation; provision it again")
        request = {
            "task": args.task,
            "message": args.message,
            "files": files,
            "worktree": str(worktree),
            "baseline_sha": current["baseline_sha"],
            "branch": current["branch"],
            "generation": current["generation"],
            "queued": now(),
            "queued_by_pid": os.getpid(),
        }
        write_json(root / "queue" / name, request)
    print(f"queued {name}")
    return 0


# --- land --------------------------------------------------------------------


def resolve_gate(worktree):
    """Return the pre-commit hook that will really run in one worktree.

    Ask git from inside the worktree rather than resolving the config by hand.
    `--git-path hooks` honours core.hooksPath, and `--path-format=absolute`
    makes a relative value resolve against *this* worktree instead of against
    whatever directory the lander happens to be running in.

    Resolving it once per run against the main checkout - which is what this
    did - handed every worktree main's hook. That is wrong twice over: a
    worktree then gates on a commit other than its own, and when main's hook
    changes, the gate every worktree ran yesterday is not the gate it runs
    today. `.githooks/pre-commit` on main has already changed once to refuse
    commits made while on main, so this was live, not theoretical.

    The gate is a property of the worktree, so a missing one fails that request
    rather than stopping the queue: another task's worktree may be wired fine,
    and refusing to land it too would help nobody.

    Args:
        worktree (Path): the worktree a commit is about to be made in.

    Returns:
        (tuple): (hooks directory, pre-commit hook, problem or None).
    """
    proc = git(
        "rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=worktree
    )
    if proc.returncode:
        return None, None, f"cannot resolve the hooks path: {proc.stderr.strip()}"
    directory = Path(proc.stdout.strip())
    hook = directory / "pre-commit"
    if not (hook.is_file() and os.access(hook, os.X_OK)):
        return (
            directory,
            hook,
            (
                f"the pre-commit gate is not wired for this worktree ({hook} is "
                f"missing or not executable); run: git config core.hooksPath .githooks"
            ),
        )
    return directory, hook, None


# The gate is a process tree, not a process: git spawns the hook, the hook spawns
# the verify spine, the spine spawns a test runner. `subprocess` signals only the
# process it started, so killing a lander leaves that tree running - observed
# still burning CPU twenty minutes after its lander was stopped, and still able
# to write to the repository. Each gated command therefore gets its own process
# group, so the whole tree can be signalled at once.
_child = None


def _group_gone(pgid):
    """Return whether a process group can no longer be signalled from here.

    EPERM counts as gone. It means the group still exists but is not ours to
    signal, so there is nothing further this process can do about it, and the
    caller is a cleanup path that must not turn that into an exception.
    A pgid of 1 or less is never signalled, for the reason `kill_group` gives.
    """
    if pgid <= 1:
        return True
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def kill_group(pgid, proc=None):
    """Signal a whole process group, escalating to SIGKILL if it lingers.

    This never raises. It is called from `finally` blocks and from a signal
    handler, and an exception there costs more than the processes it was trying
    to clean up: the first version let `PermissionError` escape from its own
    liveness probe, which killed the lander mid-landing and lost the ledger row
    for the attempt. A cleanup path that can fail loudly is worse than none.

    glibc implements `killpg(1, sig)` as `kill(-1, sig)`, which signals every
    process this user owns: on a Linux CI runner the suite's probe of group 1
    killed the runner's own shell. No group this module starts has a pgid of 1
    or less, so those are refused rather than signalled.

    Linux keeps a group signalable while its leader is an unreaped zombie, so a
    leader this process started must be reaped for the group to read as gone.

    Args:
        pgid (int): the group to kill, or None for nothing to do.
        proc (subprocess.Popen): the group's leader, reaped while waiting.

    Returns:
        (bool): whether this process has stopped trying - the group is gone, or
            is not ours to signal.
    """
    if pgid is None or pgid <= 1:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        except OSError:
            return False
        for _ in range(20):
            time.sleep(0.1)
            if proc is not None:
                proc.poll()
            if _group_gone(pgid):
                return True
    return False


def kill_running_gate():
    """Kill the gate `run_in_group` is running, if any, reaping its leader."""
    return kill_group(_child.pid if _child else None, _child)


def run_in_group(argv, timeout):
    """Run one command in its own process group, killing the tree on timeout.

    Args:
        argv (list): the command to run.
        timeout (int): seconds before the whole tree is killed.

    Returns:
        (subprocess.CompletedProcess): the same shape `subprocess.run` gives.

    Raises:
        subprocess.TimeoutExpired: after the tree has been killed, not before,
            so a timed-out gate leaves nothing behind.
    """
    global _child
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clean_env(),
        start_new_session=True,
    )
    # start_new_session makes the child its own group leader. getpgid() would say
    # the same, except that on macOS it raises ESRCH for a child that has already
    # exited but is not yet reaped - a `git add` that fails at once on index.lock.
    _child = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid, proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    finally:
        _child = None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def alive(pid_text):
    """Return whether a pid recorded in a lock file is still running.

    An unreadable holder counts as dead. A lock nobody can attribute is not
    protecting anything, and treating it as alive would wedge the queue forever
    on a truncated write.
    """
    try:
        pid = int(pid_text)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def acquire_lander_lock(root):
    """Take the single-lander lock and keep it until the process ends.

    The lock is an advisory flock on an open descriptor, so a lander that dies
    releases it with no cleanup and the file is never unlinked. The recorded pid
    is diagnostic only; it is written after the lock is held, never before.

    Returns:
        (int): the descriptor holding the lock; closing it releases the lock.
    """
    lock = root / "lander.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        os.close(fd)
        die(
            f"another lander already holds {lock}"
            + (f" (pid {holder})" if holder else "")
        )
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def lander_holder(root):
    """Return the pid text recorded by the live lander, or None if none holds the lock."""
    try:
        fd = os.open(root / "lander.lock", os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return os.pread(fd, 32, 0).decode(errors="replace").strip() or "unknown"
    finally:
        os.close(fd)
    return None


def owner_path(claimed):
    """Return the owner record that sits beside one claimed request."""
    return claimed.with_name(f"{claimed.name}.owner.json")


def requests_in(directory):
    """Return the request files in one state directory, oldest first."""
    return sorted(
        p for p in directory.glob("*.json") if not p.name.endswith(".owner.json")
    )


def claim_next(root, token):
    """Move the oldest queued request to `processing/` and record who owns it.

    Returns:
        (Path): the claimed request, or None if the queue is empty.
    """
    with meta_lock(root):
        pending = requests_in(root / "queue")
        if not pending:
            return None
        claimed = root / "processing" / pending[0].name
        os.replace(pending[0], claimed)
        write_json(
            owner_path(claimed),
            {"pid": os.getpid(), "token": token, "claimed": now()},
        )
    return claimed


def acknowledge(root, claimed, token):
    """Move a finished request to `done/`, but only if this lander still owns it."""
    with meta_lock(root):
        owner = json.loads(owner_path(claimed).read_text())
        if owner["token"] != token:
            die(f"{claimed.name} is owned by another lander (pid {owner['pid']})")
        os.replace(claimed, root / "done" / claimed.name)
        owner_path(claimed).unlink()


def with_lock_backoff(argv, timeout, tries=LOCK_TRIES):
    """Run one git command, retrying while another process holds index.lock.

    The exit code of the deciding attempt is returned unchanged. A gate failure
    and a lock that never cleared must stay distinguishable in the ledger, so
    neither is rewritten into the other.

    Args:
        argv (list): the full command to run.
        timeout (int): seconds allowed for each attempt.
        tries (int): retries before the lock is declared stuck and reported.

    Returns:
        (tuple): (CompletedProcess, retries).
    """
    delay, retries = LOCK_BACKOFF, 0
    while True:
        proc = run_in_group(argv, timeout)
        if proc.returncode == 0 or "index.lock" not in proc.stderr + proc.stdout:
            return proc, retries
        retries += 1
        if retries >= tries:
            return proc, retries
        time.sleep(delay)
        delay = min(delay * 2, LOCK_BACKOFF_MAX)


def verify_landed(worktree, message, before_sha):
    """Re-read the log and decide whether the commit is actually there.

    A zero exit code from `git commit` is a claim, not evidence. This reads
    `git log --oneline -1` back out of the worktree and requires three things
    to agree: the tip moved, the abbreviated SHA on that line prefixes the
    current HEAD, and the subject is the one that was requested. An
    `--amend`, an empty commit or a hook that rewrote the message all fail here.

    Args:
        worktree (Path): the worktree that was committed in.
        message (str): the requested message; its first line is the subject.
        before_sha (str): HEAD before the commit attempt.

    Returns:
        (tuple): (landed, sha, oneline, detail).
    """
    proc = git("log", "--oneline", "-1", cwd=worktree)
    if proc.returncode:
        return (
            False,
            None,
            "",
            f"git log failed ({proc.returncode}): {proc.stderr.strip()}",
        )
    oneline = proc.stdout.strip()
    short_sha, _, subject = oneline.partition(" ")
    sha = head_sha(worktree)
    if not short_sha or not sha:
        return False, sha, oneline, "no commit is readable at HEAD"
    if sha == before_sha:
        return False, sha, oneline, f"HEAD did not move from {before_sha}"
    if not sha.startswith(short_sha):
        return False, sha, oneline, f"log line {short_sha} does not prefix HEAD {sha}"
    wanted = message.splitlines()[0]
    if subject != wanted:
        return False, sha, oneline, f"subject is {subject!r}, requested {wanted!r}"
    return True, sha, oneline, ""


def land_one(root, request_path, timeout, lock_tries=LOCK_TRIES, no_hooks=False):
    """Stage, commit and verify one queued request; append its ledger row.

    `no_hooks` commits with no hooks at all: nothing from the worktree runs on the lander's
    host, and whoever passes it runs the checks the hook provided somewhere else.

    Returns:
        (dict): the ledger record written.
    """
    request = json.loads(request_path.read_text())
    worktree = Path(request["worktree"])
    record = {
        "ts": now(),  # attempt started; `finished` is added on write
        "task": request["task"],
        "request": request_path.name,
        "worktree": str(worktree),
        "branch": request["branch"],
        "baseline_sha": request["baseline_sha"],
        "message": request["message"],
        "gate": None,
        "files": request["files"],
        "lock_retries": 0,
        "sha": None,
        "oneline": "",
        "exit_code": None,
        "stderr": "",
    }

    def fail(detail, stderr="", code=None):
        record.update(status="FAILED", detail=detail, stderr=stderr, exit_code=code)
        append_ledger(root, record)
        return record

    if not worktree.is_dir():
        return fail(f"worktree is gone: {worktree}")

    # Confirm the worktree is still the one the request was made against. A task
    # whose branch was switched underneath it would otherwise land its commit on
    # somebody else's branch.
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).stdout.strip()
    if branch != request["branch"]:
        return fail(
            f"worktree is on branch {branch!r}, request expects {request['branch']!r}"
        )

    # Resolved here, from this worktree, so the hook that runs is this
    # worktree's own and the ledger names the exact file that gated the commit.
    if no_hooks:
        hooks_dir, hook, problem = "/dev/null", None, None
        record["gate"] = "none (--no-hooks)"
    else:
        hooks_dir, hook, problem = resolve_gate(worktree)
        record["gate"] = str(hook) if hook else None
    if problem:
        return fail(problem)

    before_sha = head_sha(worktree)
    record["head_before"] = before_sha

    # Only the lander commits in a task worktree, so HEAD must be exactly where
    # the lander last left it: the task's last landed commit, else its baseline.
    # Anything else means a commit was made outside the queue (an agent ran
    # `git commit`, `commit-tree` or `update-ref` itself) and landing on top of
    # it would carry work nobody queued or gated.
    # The baseline comes from the task manifest written at provision, not from
    # the request: a request is written by the agent and could name any commit.
    try:
        task_name(request["task"])
    except argparse.ArgumentTypeError as exc:
        return fail(str(exc))
    try:
        manifest = load_manifest(root, request["task"])
    except (SystemExit, ValueError) as exc:
        return fail(f"no readable manifest for task {request['task']!r}: {exc}")
    for field in ("worktree", "branch", "baseline_sha"):
        if str(request.get(field)) != str(manifest.get(field)):
            return fail(
                f"request {field} {request.get(field)!r} does not match the task "
                f"manifest's {manifest.get(field)!r}"
            )
    if request.get("generation") is None or request["generation"] != manifest.get(
        "generation"
    ):
        return fail(
            f"request generation {request.get('generation')!r} does not match the "
            f"task manifest's {manifest.get('generation')!r}"
        )
    landed = [
        r
        for r in read_ledger(root)
        if r.get("task") == request["task"] and r.get("status") == "LANDED"
    ]
    expected = landed[-1]["sha"] if landed else manifest["baseline_sha"]
    if before_sha != expected:
        return fail(
            f"HEAD is {str(before_sha)[:9]}, expected {expected[:9]} (the task's "
            f"last landed commit or baseline): a commit was made outside the "
            f"queue; agents enqueue, they never commit"
        )

    # The request file is agent-written, so the enqueue checks are repeated here.
    for name in request["files"]:
        if not isinstance(name, str) or set("*?[") & set(name) or name.startswith(":"):
            return fail(
                f"queued path {name!r} is not one literal file inside the worktree"
            )
        target = (worktree / name).resolve()
        if (
            not target.is_relative_to(worktree.resolve())
            or target == worktree.resolve()
            or target.is_dir()
        ):
            return fail(
                f"queued path {name!r} is not one literal file inside the worktree"
            )

    # The agent's own index is not the queue. Anything staged there that the
    # request does not name would ride along in a pathspec-less commit.
    # --no-renames: a staged rename must show both its source and its target.
    staged = git(
        "diff", "--cached", "--name-only", "--no-renames", "-z", cwd=worktree
    ).stdout.split("\0")
    queued = {Path(f).as_posix() for f in request["files"]}
    extra = sorted(p for p in staged if p and p not in queued)
    if extra:
        return fail(
            f"the worktree index holds staged changes nobody queued: "
            f"{', '.join(extra[:5])}; agents enqueue, they never stage"
        )

    add, add_retries = with_lock_backoff(
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(worktree),
            "add",
            "--",
            *request["files"],
        ],
        timeout,
        lock_tries,
    )
    record["lock_retries"] += add_retries
    if add.returncode:
        return fail(f"git add exited {add.returncode}", add.stderr, add.returncode)

    try:
        commit, commit_retries = with_lock_backoff(
            # Literal: the request file is agent-written, so a pattern that got
            # past enqueue still names only a file of that exact name.
            [
                "git",
                "--literal-pathspecs",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-C",
                str(worktree),
                "commit",
                "-m",
                request["message"],
                "--only",
                "--",
                *request["files"],
            ],
            timeout,
            lock_tries,
        )
    except subprocess.TimeoutExpired as expired:
        return fail(
            f"the pre-commit gate exceeded {timeout}s",
            (expired.stderr or b"").decode(errors="replace")
            if isinstance(expired.stderr, bytes)
            else (expired.stderr or ""),
        )
    record["lock_retries"] += commit_retries
    record["exit_code"] = commit.returncode

    if commit.returncode:
        # A failed commit must leave the ref where it was. It does not always:
        # anything inside the gate that shells out to git inherits GIT_DIR and
        # can write to this very branch, and the only visible signal is the red
        # gate - which is exactly the signal an operator learns to discount.
        # Say it out loud instead.
        after = head_sha(worktree)
        moved = ""
        if after and before_sha and after != before_sha:
            moved = (
                f"; HEAD moved from {before_sha[:9]} to {after[:9]} even though "
                f"the commit failed, so something inside the gate wrote to "
                f"{request['branch']}"
            )
        return fail(
            f"git commit exited {commit.returncode} after "
            f"{commit_retries} lock retries{moved}",
            commit.stderr,
            commit.returncode,
        )

    landed, sha, oneline, detail = verify_landed(
        worktree, request["message"], before_sha
    )
    record.update(sha=sha, oneline=oneline)
    if not landed:
        # The exit code said yes and the log says no. That gap is the whole
        # reason this check exists, so it is recorded as a failure with the
        # commit's own stderr attached.
        return fail(
            f"commit reported success but verification failed: {detail}",
            commit.stderr,
            commit.returncode,
        )

    record.update(status="LANDED", detail="")
    record["stderr"] = commit.stderr
    append_ledger(root, record)
    return record


def cmd_land(args):
    """Consume the queue as the single lander, one commit at a time."""
    root = state_root()
    lock_fd = acquire_lander_lock(root)
    token = secrets.token_hex(8)

    def shutdown(signum, _frame):
        """Kill the running gate's tree, then let `finally` release the lock.

        Without this a stopped lander orphans its gate; the lock itself is
        released by the kernel whenever the process ends.
        """
        print(f"signal {signum}: killing the running gate and releasing the lock")
        kill_running_gate()
        raise SystemExit(128 + signum)

    previous_handlers = {
        sig: signal.signal(sig, shutdown) for sig in (signal.SIGTERM, signal.SIGINT)
    }

    landed = failed = 0
    try:
        print(
            f"lander pid {os.getpid()}, timeout {args.timeout}s; "
            + (
                "commits run with no hooks"
                if args.no_hooks
                else "each commit is gated by its own worktree's hook"
            )
        )
        while True:
            request_path = claim_next(root, token)
            if request_path is None:
                if args.once:
                    break
                time.sleep(args.poll)
                continue
            record = land_one(
                root, request_path, args.timeout, args.lock_tries, args.no_hooks
            )
            acknowledge(root, request_path, token)
            if record["status"] == "LANDED":
                landed += 1
                print(
                    f"LANDED {record['task']:<22} {record['oneline']}"
                    f"  (lock retries {record['lock_retries']})"
                )
            else:
                failed += 1
                print(
                    f"FAILED {record['task']:<22} {record['detail']}"
                    f"  (lock retries {record['lock_retries']})"
                )
    finally:
        kill_running_gate()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        os.close(lock_fd)

    print(f"{landed} landed, {failed} failed")
    # The process exit code carries the outcome. Nothing is echoed after this.
    return 1 if failed else 0


# --- reconcile ---------------------------------------------------------------


def classify(manifest, ledger, queued, processing, lander_live):
    """Return one task's state and the evidence behind it.

    The states are decided in this order, because work in flight outranks work
    already done and unlanded work outranks a clean history:

        PROCESSING   a lander has claimed a request and has not acknowledged it
        QUEUED       a request is waiting for the lander
        ORPHANED     the worktree holds work no request will land
        LANDED       HEAD is exactly the last landed commit, tree clean
        FAILED       every attempt failed and nothing is left to retry
        PROVISIONED  the worktree is still at its baseline, untouched

    Returns:
        (tuple): (state, detail).
    """
    task = manifest["task"]
    worktree = Path(manifest["worktree"])
    mine = [r for r in ledger if r["task"] == task]
    landed = [r for r in mine if r["status"] == "LANDED"]
    failed = [r for r in mine if r["status"] == "FAILED"]
    waiting = [q for q in queued if q["task"] == task]
    claimed = [owner for request, owner in processing if request["task"] == task]

    if claimed:
        pids = ", ".join(
            sorted({str((o or {}).get("pid", "unknown")) for o in claimed})
        )
        stale = (
            ""
            if lander_live
            else "; stale: no live lander holds the lock, awaits recovery"
        )
        return "PROCESSING", f"{len(claimed)} request(s), owner pid {pids}{stale}"
    if waiting:
        return "QUEUED", f"{len(waiting)} request(s) waiting"
    if not worktree.is_dir():
        if landed:
            return "LANDED", f"{landed[-1]['sha'][:9]} (worktree removed)"
        return "ORPHANED", "worktree is gone and nothing landed"

    dirty = porcelain(worktree) or []
    head = head_sha(worktree)
    expected = landed[-1]["sha"] if landed else manifest["baseline_sha"]

    if dirty or head != expected:
        reason = []
        if dirty:
            reason.append(f"{len(dirty)} uncommitted path(s)")
        if head != expected:
            reason.append(f"HEAD {head[:9]} is past {expected[:9]}")
        return "ORPHANED", "; ".join(reason)
    if landed:
        return "LANDED", f"{len(landed)} commit(s), tip {landed[-1]['sha'][:9]}"
    if failed:
        return "FAILED", f"{len(failed)} attempt(s), last: {failed[-1]['detail']}"
    return "PROVISIONED", f"at baseline {manifest['baseline_sha'][:9]}"


def cmd_reconcile(args):
    """Report which tasks landed, which are queued and which are orphaned."""
    root = state_root()
    ledger = read_ledger(root)
    # One snapshot: a claim moves a request between the two directories.
    with meta_lock(root):
        queued = [json.loads(p.read_text()) for p in requests_in(root / "queue")]
        processing = [
            (
                json.loads(p.read_text()),
                json.loads(owner_path(p).read_text())
                if owner_path(p).exists()
                else None,
            )
            for p in requests_in(root / "processing")
        ]
    holder = lander_holder(root)
    manifests = [
        json.loads(p.read_text()) for p in sorted((root / "tasks").glob("*.json"))
    ]
    rows = []
    for manifest in manifests:
        state, detail = classify(
            manifest, ledger, queued, processing, holder is not None
        )
        rows.append(
            {
                "task": manifest["task"],
                "state": state,
                "detail": detail,
                "branch": manifest["branch"],
                "baseline_sha": manifest["baseline_sha"],
                "worktree": manifest["worktree"],
            }
        )

    if args.json:
        print(json.dumps({"tasks": rows, "ledger": ledger}, indent=2))
    else:
        width = max([len(r["task"]) for r in rows], default=4)
        for row in rows:
            print(f"  {row['state']:<12} {row['task']:<{width}}  {row['detail']}")
        counts = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        print(
            f"  {len(rows)} task(s): "
            + ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items()))
        )
        if holder is not None:
            print(f"  lander.lock held by pid {holder}")
    return 0


def build_parser():
    """Return the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="agent_harness", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("provision", help="create one task's worktree")
    p.add_argument("--task", required=True, type=task_name)
    # Required on purpose: a baseline defaulting to HEAD is a baseline nobody
    # chose, and four tasks started minutes apart would each have a different one.
    p.add_argument(
        "--baseline",
        required=True,
        help="commit-ish to branch from; never defaulted to HEAD",
    )
    p.add_argument("--path", required=True)
    p.add_argument("--branch")
    p.add_argument(
        "--no-inputs",
        action="store_true",
        help="clone no gitignored inputs, so no host-only file reaches the worktree",
    )
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("enqueue", help="queue one commit request")
    p.add_argument("--task", required=True, type=task_name)
    p.add_argument("-m", "--message", required=True)
    p.add_argument(
        "--file",
        action="append",
        default=[],
        help="path to stage, relative to the current directory",
    )
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("land", help="consume the queue as the single lander")
    p.add_argument("--once", action="store_true", help="stop when the queue empties")
    p.add_argument(
        "--no-hooks",
        action="store_true",
        help="commit with no hooks, so nothing from the worktree runs on this host",
    )
    p.add_argument("--timeout", type=int, default=GATE_TIMEOUT)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument(
        "--lock-tries",
        type=int,
        default=LOCK_TRIES,
        help="index.lock retries before the lock is reported stuck",
    )
    p.set_defaults(func=cmd_land)

    p = sub.add_parser("reconcile", help="report landed / queued / orphaned")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reconcile)
    return parser


def main(argv=None):
    """Dispatch one subcommand and return its exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
