#!/usr/bin/env python3
"""Checks that the parallel-agent harness cannot report a commit it did not make.

Every check here protects one way a parallel-agent workflow lies about itself:

  - a task started from whatever HEAD happened to be, rather than a named baseline;
  - a worktree laid over somebody else's uncommitted work;
  - an agent staging a path that belongs to a sibling worktree;
  - `git commit` exiting 0 while the log shows a different commit, or none;
  - a stale `index.lock` read as a gate failure, or a gate failure read as a lock;
  - a `land` run that failed and exited 0 anyway.

The repository under test is a temporary one with a pre-commit hook this file
controls, so the accept and the reject path both run in about a second. What is
supplied is the harness's *input* - a repository - never its own state: every
manifest, queue entry and ledger row below is written by the harness itself.
"""

import atexit
import fcntl
import json
import os
import shlex
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "harness"))

import agent_harness as H

HARNESS = ROOT / ".claude" / "harness" / "agent_harness.py"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'ok  ' if cond else 'FAIL'} {name}"
        + (f" - {detail}" if detail and not cond else "")
    )


# See agent_harness.GIT_ENV_LEAKS. This file runs inside the pre-commit gate, so
# without scrubbing, every `git init`/`add`/`commit` below would operate on the
# repository being committed to instead of on its own temporary one.
def _clean_env():
    env = os.environ.copy()
    for name in H.GIT_ENV_LEAKS:
        env.pop(name, None)
    return env


def run(*args, cwd, gate=True):
    """Run the harness CLI as a subprocess, the way an agent would."""
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env=_clean_env(),
    )


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=_clean_env()
    )


def make_repo(tmp, gate_exit=0):
    """Return a repository with two commits and a pre-commit hook we control."""
    repo = tmp / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "harness@test", cwd=repo)
    git("config", "user.name", "Harness Test", cwd=repo)
    hooks = repo / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho 'gate ran' >&2\nexit {gate_exit}\n")
    hook.chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=repo)
    # A partly tracked inputs directory: one file in git, one only on disk.
    # `git worktree add` recreates the tracked file and nothing else, so this is
    # the shape that made the provisioner skip the directory as "already there".
    (repo / "func" / "sources").mkdir(parents=True)
    (repo / "func" / "sources" / "tracked.txt").write_text("tracked\n")
    # Mirrors the real repository: the inputs directory is ignored apart from
    # the one file that is genuinely authored here. Without this the cloned
    # inputs would show up as untracked and every worktree would read dirty.
    (repo / ".gitignore").write_text(
        "func/sources/*\n!func/sources/tracked.txt\n.env\n.env.*\nnested_cache/\ntmp/\n"
    )
    (repo / "harness.toml").write_text('[land]\ncopy_untracked = ["func/sources"]\n')
    (repo / "a.txt").write_text("one\n")
    git("add", "-A", cwd=repo)
    git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "first", cwd=repo)
    first = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "a.txt").write_text("two\n")
    git("add", "-A", cwd=repo)
    git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "second", cwd=repo)
    second = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "func" / "sources" / "supplied.bin").write_text("supplied by hand\n")
    (repo / "func" / "sources" / "nested").mkdir()
    (repo / "func" / "sources" / "nested" / "deep.json").write_text("{}\n")
    (repo / ".env").write_text("API_TOKEN=super-secret\n")
    (repo / ".env.local").write_text("API_TOKEN=local-fixture\n")
    (repo / "tmp" / "worktrees").mkdir(parents=True)
    (repo / "tmp" / "worktrees" / "peer.txt").write_text("another task\n")
    (repo / "nested_cache").mkdir()
    (repo / "nested_cache" / "junk.bin").write_text("rebuildable\n")
    return repo, first, second


def set_gate(repo, exit_code):
    hook = repo / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho 'gate ran' >&2\nexit {exit_code}\n")
    hook.chmod(0o755)


tmpdir = tempfile.TemporaryDirectory()
tmp = Path(tmpdir.name)
repo, FIRST, SECOND = make_repo(tmp)
state = repo / ".git" / "agent-harness"

print("a task starts from the baseline it names, not from HEAD")
# HEAD is SECOND. The task names FIRST. If the harness ever defaults to HEAD,
# the recorded baseline and the worktree tip both become SECOND and this fails.
out = run(
    "provision",
    "--task",
    "alpha",
    "--baseline",
    FIRST,
    "--path",
    str(tmp / "wt-alpha"),
    cwd=repo,
)
check(
    "provision succeeds against an explicit baseline",
    out.returncode == 0,
    out.stdout + out.stderr,
)
manifest = (
    json.loads((state / "tasks" / "alpha.json").read_text())
    if (state / "tasks" / "alpha.json").exists()
    else {}
)
check(
    "the manifest records the resolved baseline SHA",
    manifest.get("baseline_sha") == FIRST,
    f"{manifest.get('baseline_sha')} != {FIRST}",
)
check("the baseline recorded is not HEAD", manifest.get("baseline_sha") != SECOND)
# The inputs come from a checkout that is still being edited, so a gate failure
# may be skew rather than the agent's change. The manifest has to say so.
check(
    "the manifest records which checkout the inputs came from",
    manifest.get("source_checkout") == str(repo.resolve()),
    str(manifest.get("source_checkout")),
)
check(
    "and that checkout's HEAD, which need not be the baseline",
    manifest.get("source_head") == SECOND
    and manifest["source_head"] != manifest["baseline_sha"],
    f"source_head={manifest.get('source_head')} baseline={manifest.get('baseline_sha')}",
)
check(
    "and how many uncommitted paths it had at the time",
    isinstance(manifest.get("source_uncommitted_paths"), int),
    str(manifest.get("source_uncommitted_paths")),
)
check(
    "provision warns out loud when the inputs are not from the baseline",
    "WARNING" in out.stdout and "not the baseline" in out.stdout,
    out.stdout,
)
check(
    "the manifest records the gate the worktree will actually commit under",
    manifest.get("gate", "").endswith("pre-commit"),
    str(manifest.get("gate")),
)
# This repository points every worktree at one shared hooks directory, which is
# the shape the real one has, so the warning is the correct outcome here.
check(
    "provision says so when the gate lies outside the worktree",
    "outside it" in out.stdout,
    out.stdout,
)
check(
    "and gives the exact command that clears it",
    "config --worktree --unset core.hooksPath" in out.stdout,
    out.stdout,
)
check(
    "the worktree is checked out at that baseline",
    git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip() == FIRST,
)
check(
    "--baseline is mandatory, so HEAD can never be the accidental default",
    run(
        "provision", "--task", "nob", "--path", str(tmp / "wt-nob"), cwd=repo
    ).returncode
    != 0,
)

print("\nthe ignored inputs a checkout cannot carry are cloned in and verified")
inputs = tmp / "wt-alpha" / "func" / "sources"
check(
    "the tracked file the checkout carried is still there",
    (inputs / "tracked.txt").read_text() == "tracked\n",
)
check(
    "the untracked input beside it was cloned in, not skipped",
    (inputs / "supplied.bin").exists(),
    f"contents: {sorted(p.name for p in inputs.iterdir())}",
)
check(
    "and so was one nested a directory deep", (inputs / "nested" / "deep.json").exists()
)
check(
    "the supplied inputs are reported as cloned",
    any(c.startswith("func/sources/") for c in manifest.get("cloned_inputs", [])),
    str(manifest.get("cloned_inputs")),
)
# Inputs are explicit; ignored credentials and another task's scratch stay out.
check(
    "the ignored .env exists in the checkout being cloned from",
    (repo / ".env").read_text() == "API_TOKEN=super-secret\n",
)
check(
    "but credentials are never cloned into a worktree",
    not (tmp / "wt-alpha" / ".env").exists(),
)
check(
    "and .env is not even reported as a cloned input",
    ".env" not in manifest.get("cloned_inputs", []),
    str(manifest.get("cloned_inputs")),
)
check(
    "environment variants never arrive in the task worktree",
    not (tmp / "wt-alpha" / ".env.local").exists(),
)
check(
    "scratch worktrees are not copied as task inputs",
    not (tmp / "wt-alpha" / "tmp").exists(),
)
check(
    "ignored data not declared in copy_untracked stays outside the task",
    not (tmp / "wt-alpha" / "nested_cache").exists(),
)
check(
    "the clone gave the worktree its own inode, not a shared one",
    (inputs / "supplied.bin").stat().st_ino
    != (repo / "func" / "sources" / "supplied.bin").stat().st_ino,
)
(inputs / "supplied.bin").write_text("worktree-local change\n")
check(
    "writing through the clone does not reach the source",
    (repo / "func" / "sources" / "supplied.bin").read_text() == "supplied by hand\n",
)
print("\na live socket in declared inputs is refused before copying")
sock_path = repo / "func" / "sources" / "live.sock"
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
# Bind by relative name: a repo-local TMPDIR exceeds the 104-byte sun_path limit.
_cwd = os.getcwd()
os.chdir(sock_path.parent)
try:
    server.bind(sock_path.name)
finally:
    os.chdir(_cwd)
out = run(
    "provision",
    "--task",
    "socket",
    "--baseline",
    FIRST,
    "--path",
    str(tmp / "wt-socket"),
    cwd=repo,
)
check(
    "nonregular declared input stops provisioning",
    out.returncode != 0 and "nonregular" in out.stderr,
    out.stdout + out.stderr,
)
check(
    "refused input leaves no successful task manifest",
    not (state / "tasks/socket.json").exists(),
)
server.close()
sock_path.unlink()

print("\nprovisioning refuses a clone that would dirty the new worktree")
# `.claude-flow/` is the case this protects: its nested .gitignore is itself
# untracked, so the content reads as ignored in the checkout it is copied FROM
# and untracked in the worktree it is copied TO. Every worktree then reports
# ORPHANED forever, for a reason the agent had no part in.
dirt_dir = tempfile.TemporaryDirectory()
dirt_tmp = Path(dirt_dir.name)
dirt_repo, dirt_first, _ = make_repo(dirt_tmp)
(dirt_repo / "flow" / "logs").mkdir(parents=True)
(dirt_repo / "flow" / ".gitignore").write_text("logs/\n")
(dirt_repo / "flow" / "logs" / "daemon.log").write_text("noise\n")
(dirt_repo / "harness.toml").write_text(
    '[land]\ncopy_untracked = ["func/sources", "flow/logs"]\n'
)
git("add", "harness.toml", cwd=dirt_repo)
git("commit", "-q", "-m", "declare inputs", cwd=dirt_repo)
dirt_first = git("rev-parse", "HEAD", cwd=dirt_repo).stdout.strip()
listed = subprocess.run(
    ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
    cwd=dirt_repo,
    capture_output=True,
    text=True,
    env=_clean_env(),
).stdout
check(
    "git reports the content as ignored in the source checkout",
    "flow/logs/" in listed,
    listed,
)
out = run(
    "provision",
    "--task",
    "dirty",
    "--baseline",
    dirt_first,
    "--path",
    str(dirt_tmp / "wt-dirty"),
    cwd=dirt_repo,
)
check(
    "provision refuses rather than handing over a dirty worktree",
    out.returncode != 0,
    out.stdout,
)
check(
    "and names what it carried in", "untracked path" in out.stderr, out.stderr.strip()
)
check(
    "no manifest is written for the refused task",
    not (dirt_repo / ".git" / "agent-harness" / "tasks" / "dirty.json").exists(),
)
dirt_dir.cleanup()

print("\na dirty target directory stops provisioning before anything is created")


# The target must sit outside every Git worktree, or it is the "inside an enclosing
# repository" case, which the next block covers on purpose.
def outside_any_worktree():
    for base in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        if probe.returncode:
            return Path(tempfile.mkdtemp(dir=base))
    raise SystemExit("no scratch location outside a Git worktree")


outside = outside_any_worktree()
dirty = outside / "dirty"
dirty.mkdir()
(dirty / "someones-work.txt").write_text("uncommitted\n")
out = run(
    "provision", "--task", "beta", "--baseline", FIRST, "--path", str(dirty), cwd=repo
)
check("provision refuses a non-empty target", out.returncode != 0, out.stdout)
check("and says why", "not empty" in out.stderr, out.stderr.strip())
check(
    "the target is left exactly as it was",
    (dirty / "someones-work.txt").read_text() == "uncommitted\n",
)
check(
    "and no manifest was written for the refused task",
    not (state / "tasks" / "beta.json").exists(),
)
shutil.rmtree(outside)

print("\na non-empty target inside an enclosing repository's ignored area is refused")
# The target is not its own worktree's top level, so it is a plain non-empty directory.
(repo / ".git" / "info" / "exclude").write_text("enclosed/\n")
enclosed = repo / "enclosed"
enclosed.mkdir()
(enclosed / "someones-work.txt").write_text("uncommitted\n")
out = run(
    "provision",
    "--task",
    "enclosed",
    "--baseline",
    FIRST,
    "--path",
    str(enclosed),
    cwd=repo,
)
check(
    "provision refuses a non-empty target inside an enclosing repo",
    out.returncode != 0,
    out.stdout,
)
check(
    "and the refusal is 'not empty'",
    "not empty" in out.stderr,
    out.stderr.strip(),
)
check(
    "the enclosed target is left exactly as it was",
    (enclosed / "someones-work.txt").read_text() == "uncommitted\n",
)
check(
    "and no manifest was written for the enclosed refusal",
    not (state / "tasks" / "enclosed.json").exists(),
)
out = run(
    "provision",
    "--task",
    "gamma",
    "--baseline",
    FIRST,
    "--path",
    str(tmp / "wt-alpha"),
    cwd=repo,
)
check("provision refuses an existing worktree as a target", out.returncode != 0)
check(
    "and calls the clean one an existing clean worktree",
    "existing clean worktree" in out.stderr,
    out.stderr.strip(),
)

print("\nan existing worktree with uncommitted work is never called clean")
fixture = tmp / "wt-fixture"
git("worktree", "add", "-q", "--detach", str(fixture), FIRST, cwd=repo)
(fixture / "a.txt").write_text("someone's unsaved edit\n")
out = run(
    "provision", "--task", "fx1", "--baseline", FIRST, "--path", str(fixture), cwd=repo
)
check("provision refuses a worktree with an edit", out.returncode != 0, out.stdout)
check(
    "and names the uncommitted work",
    "uncommitted" in out.stderr,
    out.stderr.strip(),
)
index = Path(
    git(
        "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=fixture
    ).stdout.strip()
)
index.write_bytes(b"garbage, not a git index\n" * 8)
out = run(
    "provision", "--task", "fx2", "--baseline", FIRST, "--path", str(fixture), cwd=repo
)
check(
    "provision refuses a worktree whose index git cannot read",
    out.returncode != 0,
    out.stdout,
)
check(
    "and says git cannot read it",
    "cannot read" in out.stderr,
    out.stderr.strip(),
)
check(
    "and does not call that worktree clean",
    "clean" not in out.stderr,
    out.stderr.strip(),
)
check(
    "and no manifest is written for either refused fixture task",
    not (state / "tasks" / "fx1.json").exists()
    and not (state / "tasks" / "fx2.json").exists(),
)
git("worktree", "remove", "--force", str(fixture), cwd=repo)
check(
    "a task name cannot be provisioned twice",
    run(
        "provision",
        "--task",
        "alpha",
        "--baseline",
        FIRST,
        "--path",
        str(tmp / "wt-alpha2"),
        cwd=repo,
    ).returncode
    != 0,
)

print("\nan agent cannot queue a path belonging to another worktree")
run(
    "provision",
    "--task",
    "delta",
    "--baseline",
    FIRST,
    "--path",
    str(tmp / "wt-delta"),
    cwd=repo,
)
(tmp / "wt-alpha" / "a.txt").write_text("alpha edit\n")
(tmp / "wt-delta" / "a.txt").write_text("delta edit\n")
out = run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "reach across",
    "--file",
    str(tmp / "wt-alpha" / "a.txt"),
    cwd=tmp / "wt-delta",
)
check("enqueue refuses a file outside the task's worktree", out.returncode != 0)
check(
    "and names the boundary it refused",
    "outside task" in out.stderr,
    out.stderr.strip(),
)
check("no request reached the queue", not list((state / "queue").glob("*delta*")))
check(
    "enqueue requires at least one file",
    run("enqueue", "--task", "delta", "-m", "empty", cwd=tmp / "wt-delta").returncode
    != 0,
)

print("\nthe two worktrees hold different content at the same relative path")
check(
    "wt-alpha keeps its own a.txt",
    (tmp / "wt-alpha" / "a.txt").read_text() == "alpha edit\n",
)
check(
    "wt-delta keeps its own a.txt",
    (tmp / "wt-delta" / "a.txt").read_text() == "delta edit\n",
)

print("\nverification re-reads the log and disagrees with a lying exit code")
before = git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip()
git("add", "-A", cwd=tmp / "wt-alpha")
git(
    "-c",
    "core.hooksPath=/dev/null",
    "commit",
    "-q",
    "-m",
    "alpha edit",
    cwd=tmp / "wt-alpha",
)
landed, sha, oneline, detail = H.verify_landed(tmp / "wt-alpha", "alpha edit", before)
check("a real commit with the requested subject verifies", landed, detail)
check(
    "the verified SHA is the one git log reports",
    sha == git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip(),
)
check(
    "the abbreviated SHA on the log line prefixes it",
    sha.startswith(oneline.split(" ")[0]),
)
bad, _, _, why = H.verify_landed(tmp / "wt-alpha", "a subject nobody committed", before)
check("a different subject is not accepted as landed", not bad, why)
check("and the mismatch is named", "subject is" in why, why)
still, _, _, why = H.verify_landed(tmp / "wt-alpha", "alpha edit", sha)
check("an unmoved HEAD is not accepted as landed", not still, why)
check("and the reason is that HEAD did not move", "did not move" in why, why)

print("\nindex.lock is retried with backoff, not reported as a gate failure")
check(
    "the backoff seam exists to be controlled",
    all(hasattr(H, n) for n in ("LOCK_TRIES", "LOCK_BACKOFF", "LOCK_BACKOFF_MAX")),
)
check(
    "the retry budget is a parameter an operator can set, not a constant",
    "tries" in H.with_lock_backoff.__code__.co_varnames,
)
lock = (
    Path(git("rev-parse", "--absolute-git-dir", cwd=tmp / "wt-delta").stdout.strip())
    / "index.lock"
)
lock.write_text("held\n")
threading.Timer(1.2, lambda: lock.unlink(missing_ok=True)).start()
started = time.monotonic()
proc, retries = H.with_lock_backoff(
    ["git", "-C", str(tmp / "wt-delta"), "add", "--", "a.txt"], 60
)
check(
    "the command succeeds once the lock clears",
    proc.returncode == 0,
    proc.stderr.strip(),
)
check("and it took at least one retry to get there", retries >= 1, f"retries={retries}")
check(
    "which means it waited rather than failing fast", time.monotonic() - started >= 1.0
)

lock.write_text("held forever\n")
H.LOCK_BACKOFF, H.LOCK_BACKOFF_MAX = 0.01, 0.01
proc, retries = H.with_lock_backoff(
    ["git", "-C", str(tmp / "wt-delta"), "add", "--", "a.txt"], 60, tries=3
)
check(
    "a lock that never clears exhausts the retries", retries == 3, f"retries={retries}"
)
check("and the real git exit code survives, unmasked", proc.returncode != 0)
check("with git's own words kept", "index.lock" in proc.stderr, proc.stderr.strip())
lock.unlink(missing_ok=True)
H.LOCK_BACKOFF, H.LOCK_BACKOFF_MAX = 0.5, 8.0

print("\na rejected gate lands nothing and says so with git's own stderr")
set_gate(repo, 1)
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "delta change",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
head_before = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
rejected = [r for r in ledger if r["task"] == "delta"]
check("the attempt is recorded", len(rejected) == 1, str(len(rejected)))
check("its status is FAILED", rejected and rejected[0]["status"] == "FAILED")
check(
    "the ledger carries the gate's raw stderr",
    rejected and "gate ran" in rejected[0]["stderr"],
    str(rejected),
)
check(
    "the row records when the attempt finished, not only when it began",
    rejected and rejected[0].get("finished", "") >= rejected[0]["ts"],
    str(rejected[0].get("finished")) if rejected else "",
)
check(
    "and git's real exit code, not a rewritten one",
    rejected and rejected[0]["exit_code"] not in (0, None),
    str(rejected[0]["exit_code"]),
)
check(
    "no commit was made",
    git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == head_before,
)
check("land exits non-zero when anything failed", out.returncode != 0, out.stdout)
check("the failure is visible on stdout too", "FAILED" in out.stdout, out.stdout)

print("\na passing gate lands, and the ledger SHA is the one git log reports")
set_gate(repo, 0)
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "delta change",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
good = [r for r in ledger if r["task"] == "delta" and r["status"] == "LANDED"]
check("the commit landed", len(good) == 1, out.stdout + out.stderr)
real = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
check(
    "the ledger SHA matches the worktree HEAD",
    good and good[0]["sha"] == real,
    f"{good[0]['sha'] if good else None} != {real}",
)
check(
    "the ledger message matches the commit subject",
    git("log", "--oneline", "-1", cwd=tmp / "wt-delta").stdout.strip().split(" ", 1)[1]
    == "delta change",
)
check("land exits zero when everything landed", out.returncode == 0, out.stdout)
# The repository under test sets a RELATIVE core.hooksPath, and the lander
# commits with `git -C <worktree>`. Git resolves a relative hooksPath against the
# process working directory, so before the lander pinned the verified directory
# onto the commit this hook was silently skipped and the commit landed ungated.
check(
    "the gate actually executed on the landed commit",
    good and "gate ran" in good[0]["stderr"],
    str(good[0]["stderr"] if good else None),
)
check(
    "and the ledger names the gate that ran",
    good and good[0]["gate"].endswith("hooks/pre-commit"),
    str(good[0].get("gate") if good else None),
)
check("the consumed request left the queue", not list((state / "queue").glob("*.json")))
check("and is retained for forensics", len(list((state / "done").glob("*.json"))) >= 2)
check(
    "wt-alpha was untouched by landing wt-delta",
    (tmp / "wt-alpha" / "a.txt").read_text() == "alpha edit\n",
)

print("\nreconcile separates landed, queued and orphaned")
out = run("reconcile", "--json", cwd=repo)
report = json.loads(out.stdout)
states = {row["task"]: row["state"] for row in report["tasks"]}
check(
    "the task whose commit landed reads LANDED",
    states.get("delta") == "LANDED",
    str(states),
)
check(
    "the task holding an uncommitted edit reads ORPHANED",
    states.get("alpha") == "ORPHANED",
    str(states),
)
run(
    "enqueue",
    "--task",
    "alpha",
    "-m",
    "alpha pending",
    "--file",
    "a.txt",
    cwd=tmp / "wt-alpha",
)
states = {
    row["task"]: row["state"]
    for row in json.loads(run("reconcile", "--json", cwd=repo).stdout)["tasks"]
}
check(
    "a waiting request outranks the orphan reading",
    states.get("alpha") == "QUEUED",
    str(states),
)
check(
    "reconcile reports every provisioned task and no others",
    set(states) == {"alpha", "delta"},
    str(set(states)),
)

print("\nan unwired gate lands nothing and says so")
# The gate is a property of the worktree, so a missing one fails that request
# rather than stopping the queue - another task's worktree may be wired fine.
git("config", "--unset", "core.hooksPath", cwd=repo)
head_before = git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "land exits non-zero when no pre-commit hook will run",
    out.returncode != 0,
    out.stdout,
)
check(
    "the ledger records it rather than the lander dying silently",
    ledger and ledger[-1]["status"] == "FAILED",
    str(ledger[-1] if ledger else None),
)
check(
    "and names the fix", "core.hooksPath" in ledger[-1]["detail"], ledger[-1]["detail"]
)
check(
    "no commit was made",
    git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip() == head_before,
)
git("config", "core.hooksPath", str(repo / "hooks"), cwd=repo)

print("\na worktree is gated by its own hook, not by whatever main's hook is now")
# The defect this protects against: the gate was resolved once per run against
# the main checkout, so every worktree was gated by main's current hook rather
# than the one at its own commit. Here main's HEAD carries a hook that rejects
# everything and the worktree's baseline carries one that accepts, so the two
# cannot be confused: a landing proves the worktree's own hook ran.
own = tempfile.TemporaryDirectory()
own_tmp = Path(own.name)
own_repo = own_tmp / "repo"
own_repo.mkdir()
git("init", "-q", "-b", "main", cwd=own_repo)
git("config", "user.email", "harness@test", cwd=own_repo)
git("config", "user.name", "Harness Test", cwd=own_repo)
# Relative, so git resolves it against whichever worktree is asking.
git("config", "core.hooksPath", ".githooks", cwd=own_repo)
(own_repo / ".githooks").mkdir()
accepting = own_repo / ".githooks" / "pre-commit"
accepting.write_text("#!/bin/sh\necho 'baseline hook accepted' >&2\nexit 0\n")
accepting.chmod(0o755)
(own_repo / "a.txt").write_text("one\n")
git("add", "-A", cwd=own_repo)
git(
    "-c",
    "core.hooksPath=/dev/null",
    "commit",
    "-q",
    "-m",
    "accepting hook",
    cwd=own_repo,
)
baseline = git("rev-parse", "HEAD", cwd=own_repo).stdout.strip()
# main then moves on to a hook that refuses everything.
accepting.write_text("#!/bin/sh\necho 'main hook refused' >&2\nexit 1\n")
accepting.chmod(0o755)
git("add", "-A", cwd=own_repo)
git(
    "-c",
    "core.hooksPath=/dev/null",
    "commit",
    "-q",
    "-m",
    "refusing hook",
    cwd=own_repo,
)

own_wt = own_tmp / "wt-own"
out = run(
    "provision",
    "--task",
    "own",
    "--baseline",
    baseline,
    "--path",
    str(own_wt),
    cwd=own_repo,
)
check(
    "a worktree can be provisioned at the older, accepting baseline",
    out.returncode == 0,
    out.stdout + out.stderr,
)
check(
    "main's checked-out hook refuses",
    "exit 1" in (own_repo / ".githooks" / "pre-commit").read_text(),
)
check(
    "the worktree's own checked-out hook accepts",
    "exit 0" in (own_wt / ".githooks" / "pre-commit").read_text(),
)
resolved_dir, resolved_hook, problem = H.resolve_gate(own_wt)
check(
    "the gate resolves inside the worktree, not the main checkout",
    problem is None and str(resolved_hook).startswith(str(own_wt.resolve())),
    f"{resolved_hook} (problem: {problem})",
)
check(
    "a worktree gating on its own hook draws no foreign-gate warning",
    "outside it" not in out.stdout,
    out.stdout,
)

(own_wt / "a.txt").write_text("edited in the worktree\n")
run(
    "enqueue",
    "--task",
    "own",
    "-m",
    "test: gated by my own hook",
    "--file",
    "a.txt",
    cwd=own_wt,
)
out = run("land", "--once", "--timeout", "120", cwd=own_repo)
own_state = own_repo / ".git" / "agent-harness"
own_ledger = [
    json.loads(x)
    for x in (own_state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "the commit lands, so the accepting worktree hook ran",
    own_ledger and own_ledger[-1]["status"] == "LANDED",
    str(own_ledger[-1] if own_ledger else out.stdout + out.stderr),
)
check(
    "and it was the worktree's hook, not main's, that spoke",
    own_ledger and "baseline hook accepted" in own_ledger[-1]["stderr"],
    str(own_ledger[-1]["stderr"] if own_ledger else None),
)
check(
    "main's refusing hook never ran",
    own_ledger and "main hook refused" not in own_ledger[-1]["stderr"],
)
check(
    "the ledger names the exact hook file that gated the commit",
    own_ledger and own_ledger[-1]["gate"].startswith(str(own_wt.resolve())),
    str(own_ledger[-1]["gate"] if own_ledger else None),
)
own.cleanup()

print("\ngit's environment cannot redirect this suite onto the repository under test")
# This file runs inside the pre-commit gate, where GIT_DIR, GIT_INDEX_FILE and
# GIT_WORK_TREE are set and beat both `cwd` and `-C`. Unscrubbed, every git call
# below would operate on the repository being committed to.
decoy = tmp / "decoy"
decoy.mkdir()
git("init", "-q", "-b", "main", str(decoy), cwd=tmp)
probe_repo = tmp / "probe-repo"
probe_repo.mkdir()
os.environ["GIT_DIR"] = str((decoy / ".git").resolve())
try:
    git("init", "-q", "-b", "main", cwd=probe_repo)
    scrubbed = git("rev-parse", "--absolute-git-dir", cwd=probe_repo).stdout.strip()
    # unscrubbed: this call is the control that proves the scrub does real work.
    leaked = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=probe_repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
finally:
    os.environ.pop("GIT_DIR", None)
check(
    "a scrubbed call lands in its own repository",
    Path(scrubbed).resolve() == (probe_repo / ".git").resolve(),
    scrubbed,
)
check(
    "while an unscrubbed one is captured by GIT_DIR, so the scrub does real work",
    Path(leaked).resolve() == (decoy / ".git").resolve(),
    leaked,
)

print("\na ref moved by something inside the gate is reported, not hidden")
# The peer defect this reproduces: a test inside the verify spine inherits
# GIT_DIR and commits through the branch being landed on. The commit fails, so
# the operator sees only a red gate, while the ref has moved underneath them.
hijack = repo / "hooks" / "pre-commit"
hijack.write_text(
    "#!/bin/sh\n"
    "git commit --no-verify --allow-empty -m 'hijacked by the gate' "
    ">/dev/null 2>&1\n"
    "echo 'gate ran' >&2\n"
    "exit 1\n"
)
hijack.chmod(0o755)
(tmp / "wt-delta" / "a.txt").write_text("delta once more\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: delta hijack probe",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
before_hijack = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
hijacked = ledger[-1]
after_hijack = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
check(
    "the gate really did move the ref",
    after_hijack != before_hijack,
    f"{before_hijack} -> {after_hijack}",
)
check(
    "the landing is recorded as FAILED, not LANDED",
    hijacked["status"] == "FAILED",
    str(hijacked),
)
check(
    "and the ledger says the ref moved rather than only that the gate failed",
    "HEAD moved from" in hijacked["detail"],
    hijacked["detail"],
)
check(
    "it names the branch that was written to",
    "agent/delta" in hijacked["detail"],
    hijacked["detail"],
)
set_gate(repo, 0)

print("\na commit made outside the queue is refused, not landed on top of")
# The hijack above left wt-delta one commit past its last landed SHA, which is
# exactly the state a task agent running `git commit` itself produces.
(tmp / "wt-delta" / "a.txt").write_text("delta after a direct commit\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: delta after direct commit",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
outside = ledger[-1]
check(
    "a request whose worktree HEAD moved outside the queue is FAILED",
    outside["status"] == "FAILED"
    and outside["message"] == "test: delta after direct commit",
    str(outside),
)
check(
    "and the ledger says a commit was made outside the queue",
    "outside the queue" in outside["detail"],
    outside["detail"],
)
check(
    "nothing was committed on top of it",
    git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == after_hijack,
)

# The request file is written by the agent, so a forged baseline must not move
# the lander's expectation: the manifest written at provision decides.
(tmp / "wt-delta" / "a.txt").write_text("delta with a forged baseline\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: forged baseline",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["baseline_sha"] = after_hijack
forged.write_text(json.dumps(request))
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a request whose baseline differs from the task manifest is FAILED",
    ledger[-1]["status"] == "FAILED"
    and "does not match the task manifest" in ledger[-1]["detail"],
    ledger[-1].get("detail", ""),
)

# Repair the fixture: back to the last landed commit, so later cases start clean.
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")

print("\na task name cannot leave the harness directories")
for bad in ("../escape", "a/b", ".hidden", "x..y", ""):
    out = run(
        "provision",
        "--task",
        bad,
        "--baseline",
        FIRST,
        "--path",
        str(tmp / "wt-bad"),
        cwd=repo,
    )
    check(
        f"provision refuses task name {bad!r}",
        out.returncode != 0 and "invalid task name" in out.stderr,
        out.stderr,
    )
out = run(
    "enqueue",
    "--task",
    "../../tasks/delta",
    "-m",
    "x",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
check(
    "enqueue refuses a traversing task name",
    out.returncode != 0 and "invalid task name" in out.stderr,
    out.stderr,
)
# A request file is agent-written, so a traversing task inside it must fail at land.
(tmp / "wt-delta" / "a.txt").write_text("delta with a traversing task\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: traversing task",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["task"] = "../tasks/delta"
forged.write_text(json.dumps(request))
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a queued request with a traversing task name is FAILED at land",
    ledger[-1]["status"] == "FAILED" and "invalid task name" in ledger[-1]["detail"],
    ledger[-1].get("detail", ""),
)
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
git("checkout", "-q", "--", ".", cwd=tmp / "wt-delta")

print("\nthe lander commits only what was queued")
(tmp / "wt-delta" / "deep").mkdir(exist_ok=True)
for name in (".", "deep"):
    out = run(
        "enqueue", "--task", "delta", "-m", "x", "--file", name, cwd=tmp / "wt-delta"
    )
    check(
        f"enqueue refuses the directory {name!r}",
        out.returncode != 0 and "is a directory" in out.stderr,
        out.stderr,
    )
(tmp / "wt-delta" / "deep").rmdir()
for pattern in ("*.txt", ":(glob)**", "a?.txt"):
    out = run(
        "enqueue", "--task", "delta", "-m", "x", "--file", pattern, cwd=tmp / "wt-delta"
    )
    check(
        f"enqueue refuses the pattern {pattern!r}",
        out.returncode != 0 and "is a pattern" in out.stderr,
        out.stderr,
    )
(tmp / "wt-delta" / "sneaky.txt").write_text("never queued\n")
(tmp / "wt-delta" / "a.txt").write_text("delta via a forged pattern\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: forged pattern",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = ["*.txt"]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a pattern forged into a request lands nothing",
    ledger[-1]["status"] == "FAILED" and "not one literal file" in ledger[-1]["detail"],
    str(ledger[-1]),
)
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: forged dot",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = ["."]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a '.' forged into a request lands nothing",
    ledger[-1]["status"] == "FAILED" and "not one literal file" in ledger[-1]["detail"],
    str(ledger[-1]),
)
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: forged type",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = [5]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a non-string path forged into a request is FAILED, not a lander crash",
    ledger[-1]["status"] == "FAILED" and "not one literal file" in ledger[-1]["detail"],
    str(ledger[-1]),
)
check(
    "and commits no file it matches",
    git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == before_hijack,
)
check(
    "and stages no file it matches, so the task's index stays clean",
    git("diff", "--cached", "--name-only", cwd=tmp / "wt-delta").stdout.strip() == "",
    git("diff", "--cached", "--name-only", cwd=tmp / "wt-delta").stdout,
)
(tmp / "wt-delta" / "sneaky.txt").unlink()
git("checkout", "-q", "--", ".", cwd=tmp / "wt-delta")
git("mv", "a.txt", "renamed.txt", cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "renamed.txt").write_text("renamed and edited\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: rename half-queued",
    "--file",
    "renamed.txt",
    cwd=tmp / "wt-delta",
)
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a staged rename whose source was not queued is refused, naming the source",
    ledger[-1]["status"] == "FAILED" and "a.txt" in ledger[-1]["detail"],
    ledger[-1].get("detail", ""),
)
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "renamed.txt").unlink(missing_ok=True)

(tmp / "wt-delta" / "smuggled.txt").write_text("staged by the agent, never queued\n")
git("add", "smuggled.txt", cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "a.txt").write_text("delta, queued\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: queued only",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a request whose worktree index holds unqueued staged files is FAILED",
    ledger[-1]["status"] == "FAILED" and "smuggled.txt" in ledger[-1]["detail"],
    ledger[-1].get("detail", ""),
)
check(
    "and nothing was committed",
    git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == before_hijack,
)
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "smuggled.txt").unlink(missing_ok=True)
(tmp / "wt-delta" / "a.txt").write_text("delta, queued and staged by the agent\n")
git("add", "a.txt", cwd=tmp / "wt-delta")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: queued and staged",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
check(
    "a queued file the agent also staged still lands",
    ledger[-1]["status"] == "LANDED",
    str(ledger[-1]),
)
files = git(
    "show", "--name-only", "--format=", "HEAD", cwd=tmp / "wt-delta"
).stdout.split()
check("the landed commit holds exactly the queued file", files == ["a.txt"], str(files))
before_hijack = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()

print("\na timed-out gate leaves nothing running behind it")
# The gate is a tree: git spawns the hook, the hook spawns the spine, the spine
# spawns a test runner. `subprocess` signals only the process it started, so
# before this the rest survived - a real lander's gate was found still running
# twenty minutes after the lander had been stopped. The hook here spawns a
# grandchild that would outlive a naive kill, and records its pid.
marker = tmp / "gate-grandchild.pid"
slow = repo / "hooks" / "pre-commit"
slow.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {marker}\nsleep 300\n")
slow.chmod(0o755)
(tmp / "wt-delta" / "a.txt").write_text("delta slow gate\n")
run(
    "enqueue",
    "--task",
    "delta",
    "-m",
    "test: delta slow gate",
    "--file",
    "a.txt",
    cwd=tmp / "wt-delta",
)
out = run("land", "--once", "--timeout", "3", cwd=repo)
ledger = [
    json.loads(x)
    for x in (state / "ledger.jsonl").read_text().splitlines()
    if x.strip()
]
# Select by this request's own message rather than by position - the row order
# is not this check's business.
mine = [r for r in ledger if r["message"] == "test: delta slow gate"]
check(
    "the attempt was recorded at all",
    len(mine) == 1,
    f"{len(mine)} rows. lander rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}",
)
timed_out = mine[-1] if mine else {"status": "?", "detail": "no row"}
check(
    "the landing is FAILED on the gate timeout",
    timed_out["status"] == "FAILED" and "exceeded" in timed_out["detail"],
    timed_out["detail"],
)
check(
    "the gate really did spawn a grandchild that outlives its parent",
    marker.exists() and marker.read_text().strip().isdigit(),
    f"marker: {marker.read_text() if marker.exists() else 'absent'}",
)
grandchild = marker.read_text().strip()
for _ in range(30):  # the kill escalates, so allow for it
    if not H.alive(grandchild):
        break
    time.sleep(0.1)
check(
    "and it was killed with the rest of the tree, not orphaned",
    not H.alive(grandchild),
    f"pid {grandchild} still running",
)
check(
    "kill_group reports success on a group that is already gone",
    H.kill_group(int(grandchild)) is True,
)
_killpg = H.os.killpg
check("kill_group on None is a no-op rather than an error", H.kill_group(None) is True)
# Linux keeps a group signalable while its leader is an unreaped zombie, and
# run_in_group only reaps after kill_group returns. Simulate that: the probe
# succeeds until the Popen has been reaped. Expectation by hand: the timeout (1s)
# plus a prompt kill (~0.1s), under the 2s one unreaped polling round costs.
_Popen, _procs = H.subprocess.Popen, []


class _Tracked(_Popen):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _procs.append(self)


def _linux_probe(pgid, sig):
    if sig == 0:
        if _procs[-1].returncode is None:
            return
        raise ProcessLookupError(3, "no such process")
    return _killpg(pgid, sig)


try:
    H.subprocess.Popen, H.os.killpg = _Tracked, _linux_probe
    _t0 = time.monotonic()
    try:
        H.run_in_group(["sleep", "30"], 1)
    except subprocess.TimeoutExpired:
        pass
    _elapsed = time.monotonic() - _t0
finally:
    H.subprocess.Popen, H.os.killpg = _Popen, _killpg
check(
    "a timed-out gate whose leader is a zombie is reaped, not polled out",
    _elapsed < 2,
    f"{_elapsed:.1f}s",
)
# The lander's signal handler and finally block kill a gate that is still running.
# Same zombie-leader probe; the leader must be reaped there too.
_procs.clear()
try:
    H.subprocess.Popen, H.os.killpg = _Tracked, _linux_probe
    _runner = threading.Thread(target=H.run_in_group, args=(["sleep", "30"], 30))
    _runner.start()
    for _ in range(50):
        if H._child is not None:
            break
        time.sleep(0.1)
    _t0 = time.monotonic()
    _stopped = H.kill_running_gate()
    _elapsed = time.monotonic() - _t0
    _runner.join(10)
finally:
    H.subprocess.Popen, H.os.killpg = _Popen, _killpg
check(
    "stopping the lander mid-gate reaps the zombie leader and reports the group gone",
    _stopped is True and _elapsed < 2,
    f"{_stopped} after {_elapsed:.1f}s",
)
# On Linux killpg(1) is kill(-1): every process this user owns, the test runner
# included. Group 1 must be refused before any signal is sent, on every platform,
# so no check here may pass it to the real killpg.
_killpg, _sent = H.os.killpg, []
try:
    H.os.killpg = lambda pgid, sig: _sent.append((pgid, sig))
    _answers = (H.kill_group(1), H.kill_group(0), H._group_gone(1))
finally:
    H.os.killpg = _killpg
check("group 1 and below are never signalled at all", _sent == [], str(_sent))
check("and both report them as done", _answers == (True, True, True), str(_answers))


# It runs from `finally` and from a signal handler. The first version let
# PermissionError escape its own liveness probe, which killed the lander
# mid-landing and lost the ledger row - the exact evidence it exists to write.
def _refuse(pgid, sig):
    raise PermissionError(1, "not ours")


try:
    H.os.killpg = _refuse
    check(
        "a group that exists but is not ours to signal is handled, not raised",
        H._group_gone(424242) is True and H.kill_group(424242) is True,
    )
finally:
    H.os.killpg = _killpg
check(
    "the lander installs a shutdown handler, so a stop is not a leak",
    "signal.signal" in (ROOT / ".claude" / "harness" / "agent_harness.py").read_text(),
)
set_gate(repo, 0)

print("\nthe lander lock is held by a live process, never by a file that names one")
check("a running process reads as alive", H.alive(str(os.getpid())))
# A pid_max of 4194304 makes any fixed pid a possible live process on Linux, so
# the dead holder is a child that has really exited and been reaped.
_reaped = subprocess.Popen(["true"])
_reaped.wait()
DEAD_PID = str(_reaped.pid)
check("a process that has exited does not", not H.alive(DEAD_PID))
check("nor does a pid too large for kill()", not H.alive("9" * 20))
check(
    "and neither does an unreadable holder", not H.alive("") and not H.alive("nonsense")
)

# Holders are real processes. The helper calls the harness's own acquisition, so
# what it holds is exactly what a lander holds; nothing here writes a pid into a
# lock file and calls it a holder.
HELPER = tmp / "lock_helper.py"
HELPER.write_text(
    f"""
import fcntl, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, {str(HARNESS.parent)!r})
import agent_harness as H

signal.alarm(120)  # a wedged helper must not hang the suite
mode, root = sys.argv[1], Path(sys.argv[2])
if mode == "hold":
    held = H.acquire_lander_lock(root)
    print("HELD", flush=True)
    sys.stdin.read()
elif mode == "holdraw":
    # Holds the flock but has not yet written its pid: the old O_EXCL window.
    fd = os.open(root / "lander.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("HELD", flush=True)
    sys.stdin.read()
elif mode == "race":
    os.read(int(sys.argv[3]), 1)  # released together when the parent closes the pipe
    try:
        held = H.acquire_lander_lock(root)
    except SystemExit:
        print("REFUSED", time.monotonic_ns(), flush=True)
        sys.exit(0)
    print("ACQ", time.monotonic_ns(), flush=True)
    sys.stdin.readline()
    print("REL", time.monotonic_ns(), flush=True)
    os._exit(0)
elif mode == "enqueue":
    # Announces it just before it blocks on meta.lock, which enqueue only takes
    # after its first read of the manifest: a deterministic sync point.
    marker, real = Path(sys.argv[3]), H.meta_lock

    def arriving(root):
        marker.write_text("arrived")
        return real(root)

    H.meta_lock = arriving
    sys.exit(H.main(sys.argv[4:]))
elif mode == "probe":
    fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
"""
)
GATE_PROBE = tmp / "gate_probe.py"
GATE_PROBE.write_text(
    """
import fcntl, os, sys

lock = sys.argv[1]
want = os.stat(lock)
leaked = []
for fd in range(3, 256):
    try:
        got = os.fstat(fd)
    except OSError:
        continue
    if (got.st_dev, got.st_ino) == (want.st_dev, want.st_ino):
        leaked.append(fd)
fd = os.open(lock, os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    held = "FREE"
except BlockingIOError:
    held = "HELD"
print(f"leaked={leaked} lock={held}")
"""
)


LIVE_HELPERS = []


def helper(mode, *args, **kwargs):
    proc = subprocess.Popen(
        [sys.executable, str(HELPER), mode, *map(str, args)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clean_env(),
        **kwargs,
    )
    LIVE_HELPERS.append(proc)
    return proc


def reap(*procs):
    """Kill and collect helpers, so a failed check never strands a lock holder."""
    for proc in procs:
        proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
        if proc in LIVE_HELPERS:
            LIVE_HELPERS.remove(proc)


atexit.register(lambda: reap(*LIVE_HELPERS))


def say(proc, *tokens, seconds=30):
    """Return the first helper line whose first word is in `tokens`, or None on timeout.

    Other lines are ignored. Reads the descriptor directly so a helper that
    never speaks fails a check instead of hanging the suite.
    """
    end = time.monotonic() + seconds
    buf = getattr(proc, "pending", b"")
    while True:
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            words = line.decode(errors="replace").split()
            if words[:1] and words[0] in tokens:
                proc.pending = buf
                return words
        left = end - time.monotonic()
        chunk = (
            os.read(proc.stdout.fileno(), 4096)
            if left > 0 and select.select([proc.stdout], [], [], left)[0]
            else b""
        )
        if not chunk:
            proc.pending = buf
            return None
        buf += chunk


def lock_free(name):
    """Return whether no process holds `state/<name>`, asked from a separate process."""
    return (
        subprocess.run(
            [sys.executable, str(HELPER), "probe", str(state / name)],
            env=_clean_env(),
        ).returncode
        == 0
    )


def new_task(name):
    """Provision one task at SECOND with an edited a.txt, and return its worktree."""
    wt = tmp / f"wt-{name}"
    out = run(
        "provision", "--task", name, "--baseline", SECOND, "--path", str(wt), cwd=repo
    )
    check(f"provisioned {name}", out.returncode == 0, out.stderr)
    (wt / "a.txt").write_text(f"{name} edit\n")
    return wt


def enq(name, message="test: land"):
    """Enqueue a.txt for one task; return the process and the request it queued."""
    out = run(
        "enqueue",
        "--task",
        name,
        "-m",
        message,
        "--file",
        "a.txt",
        cwd=tmp / f"wt-{name}",
    )
    queued = sorted((state / "queue").glob(f"*-{name}-*.json"))
    return out, (queued[-1] if queued else None)


def wait_for(path, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def blocking_gate(tag):
    """Install a gate that reports its fds, announces itself, then waits for GO."""
    go, started, report = (tmp / f"{tag}.{x}" for x in ("go", "started", "report"))
    hook = repo / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(GATE_PROBE))} "
        f"{shlex.quote(str(state / 'lander.lock'))} > {shlex.quote(str(report))}\n"
        f"echo $$ > {shlex.quote(str(started))}\n"
        "i=0\n"
        f"while [ ! -f {shlex.quote(str(go))} ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)
    return go, started, report


def start_lander(*extra):
    return subprocess.Popen(
        [sys.executable, str(HARNESS), "land", "--once", "--timeout", "120", *extra],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clean_env(),
    )


lander_lock = state / "lander.lock"

new_task("lk")
_out, _req = enq("lk")
holder = helper("hold", state)
check(
    "a helper takes the lock the way a lander does",
    say(holder, "HELD") is not None,
)
_ino = lander_lock.stat().st_ino
out = run("land", "--once", cwd=repo)
check(
    "a second lander refuses while a live process holds the lock",
    out.returncode != 0,
    out.stdout,
)
check("and says who holds it", "already holds" in out.stderr, out.stderr.strip())
check(
    "and names the holder's pid from the lock file",
    f"pid {holder.pid}" in out.stderr,
    out.stderr.strip(),
)
check(
    "the refused lander left the queued request where it was",
    _req is not None and _req.exists(),
)
check(
    "reconcile names the holder while it is live",
    f"lander.lock held by pid {holder.pid}" in run("reconcile", cwd=repo).stdout,
)
_again, _other = enq("lk", "test: enqueued while another process holds the lock")
check(
    "enqueue succeeds while another process holds the lander lock",
    _again.returncode == 0 and _other is not None,
    _again.stderr,
)
_req.unlink()  # one request is enough for the landers below

# A lander killed at any point, including between taking the lock and anything it
# would have cleaned up, must not wedge the queue. The kernel drops the flock; no
# takeover step exists to run, and none may be reported.
reap(holder)
check("a SIGKILLed holder leaves the lock file in place", lander_lock.exists())
check(
    "reconcile reports no holder once it is dead, though the file remains",
    "lander.lock held" not in run("reconcile", cwd=repo).stdout,
)
out = run("land", "--once", "--timeout", "120", cwd=repo)
check(
    "the next lander acquires with no takeover message",
    out.returncode == 0 and "taking over" not in out.stdout + out.stderr,
    out.stdout + out.stderr,
)
check(
    "and the queue drains rather than wedging",
    not list((state / "queue").glob("*.json")),
    out.stdout,
)
check(
    "the lock file survives the lander that used it",
    lander_lock.exists() and lander_lock.stat().st_ino == _ino,
    "the lock file was unlinked or replaced",
)
out = run("land", "--once", cwd=repo)
check(
    "a later lander still acquires, on the same inode",
    out.returncode == 0 and lander_lock.exists() and lander_lock.stat().st_ino == _ino,
    out.stderr,
)

# A stale file proves nothing: only a live holder blocks a lander.
for _stale in (f"{DEAD_PID}\n", "not a pid\n", ""):
    lander_lock.write_text(_stale)
    out = run("land", "--once", cwd=repo)
    check(
        f"a stale lock file ({_stale.strip() or 'empty'}) with no live holder does not block",
        out.returncode == 0 and "already holds" not in out.stderr,
        out.stderr,
    )

print("\ntwo landers reaching the lock together never both own it")
# The old window, held open: the lock exists and its owner has not written a pid
# yet. A lander that read an empty file as "dead" took the lock over here.
_raw_root = tmp / "rawhold"
_raw_root.mkdir()
_raw = helper("holdraw", _raw_root)
check("the raw holder took the lock", say(_raw, "HELD") is not None)
try:
    H.acquire_lander_lock(_raw_root)
    _taken = True
except SystemExit:
    _taken = False
check("a lock whose holder has not written its pid yet is still held", not _taken)
reap(_raw)
_spans = []
for _round in range(25):
    _root = tmp / f"race-{_round}"
    _root.mkdir()
    _r, _w = os.pipe()
    _procs = [helper("race", _root, _r, pass_fds=(_r,)) for _ in range(2)]
    os.close(_r)
    os.close(_w)  # EOF on the shared pipe releases both helpers at once
    try:
        _lines = [say(p, "ACQ", "REFUSED") or [] for p in _procs]
        _winners = [p for p, line in zip(_procs, _lines) if line[:1] == ["ACQ"]]
        check(
            f"round {_round}: exactly one lander acquires and the other refuses",
            len(_winners) == 1
            and sum(line[:1] == ["REFUSED"] for line in _lines) == 1,
            str(_lines),
        )
        # Release every acquirer so a double owner cannot hide behind a held lock.
        for p, line in zip(_procs, _lines):
            if line[:1] == ["ACQ"]:
                p.stdin.write("\n")
                p.stdin.flush()
                _rel = say(p, "REL")
                check(f"round {_round}: the acquirer reports its release", _rel is not None)
                if _rel:
                    _spans.append((int(line[1]), int(_rel[1])))
    finally:
        reap(*_procs)
_spans.sort()
check(
    "no two held intervals overlap across the rounds",
    all(a[1] <= b[0] for a, b in zip(_spans, _spans[1:])),
    str(_spans),
)

print("\na gate runs under the lander without the lock fd and without meta.lock")
new_task("eps")
new_task("zeta")
go, started, report = blocking_gate("g1")
_out, _req = enq("eps")
lander = start_lander()
check("the gate started", wait_for(started))
_proc = list((state / "processing").glob("*.json"))
_owners = [p for p in _proc if p.name.endswith(".owner.json")]
_claimed = [p for p in _proc if not p.name.endswith(".owner.json")]
check(
    "the claimed request is in processing/ and no longer in queue/",
    [p.name for p in _claimed] == [_req.name] and not _req.exists(),
    str(_proc),
)
_owner = json.loads(_owners[0].read_text()) if _owners else {}
check(
    "its owner record names the live lander and carries a token and claim time",
    _owner.get("pid") == lander.pid
    and bool(_owner.get("token"))
    and "claimed" in _owner,
    str(_owner),
)
check(
    "the lock file records the same lander pid",
    lander_lock.read_text().strip() == str(lander.pid),
)
check("meta.lock is free while the gate runs", lock_free("meta.lock"))
_zeta, _zreq = enq("zeta")
check(
    "enqueue completes while a gate runs",
    _zeta.returncode == 0 and _zreq is not None,
    _zeta.stderr,
)
_rec = run("reconcile", cwd=repo).stdout
check(
    "reconcile reads the claimed task as PROCESSING with its owner pid, not stale",
    re.search(rf"PROCESSING\s+eps\s.*owner pid {lander.pid}$", _rec, re.M) is not None
    and "stale" not in _rec,
    _rec,
)
check(
    "reconcile names the live lander as the lock holder",
    f"lander.lock held by pid {lander.pid}" in _rec,
    _rec,
)
check(
    "the gate process does not hold the lander lock's descriptor",
    report.read_text().strip() == "leaked=[] lock=HELD",
    report.read_text(),
)
go.write_text("go\n")
_stdout, _stderr = lander.communicate(timeout=120)
check(
    "the lander lands both requests",
    lander.returncode == 0 and "2 landed, 0 failed" in _stdout,
    _stdout + _stderr,
)
check(
    "afterwards processing/ is empty: request acknowledged, owner record removed",
    not list((state / "processing").glob("*")),
)
check("and the request is in done/", (state / "done" / _req.name).exists())
check(
    "the lander released the lock when it exited",
    lock_free("lander.lock") and lock_free("meta.lock"),
)

print(
    "\na lander killed mid-gate leaves its claim for recovery, and nobody re-lands it"
)
new_task("eta")
go, started, report = blocking_gate("g2")
_out, _req = enq("eta")
lander = start_lander()
check("the gate started", wait_for(started))
_hook_pid = int(started.read_text())
lander.kill()
lander.communicate()
go.write_text("go\n")
for _ in range(300):  # let the orphaned gate finish so cleanup does not race it
    try:
        os.kill(_hook_pid, 0)
    except ProcessLookupError:
        break
    time.sleep(0.1)
_proc = sorted(p.name for p in (state / "processing").glob("*"))
check(
    "the request and its owner record are still in processing/",
    _proc == sorted([_req.name, f"{_req.name}.owner.json"]),
    str(_proc),
)
_rec = run("reconcile", cwd=repo).stdout
check(
    "reconcile reports the task PROCESSING and stale",
    re.search(r"PROCESSING\s+eta\s.*stale", _rec) is not None,
    _rec,
)
_before = len(H.read_ledger(state))
out = run("land", "--once", "--timeout", "120", cwd=repo)
check(
    "a new lander does not re-land the claimed request (recovery is not R1)",
    out.returncode == 0
    and "0 landed, 0 failed" in out.stdout
    and (state / "processing" / _req.name).exists()
    and len(H.read_ledger(state)) == _before,
    out.stdout + out.stderr,
)

# Staleness is about the owner, not about whether some lander runs: lander A's
# claim is still orphaned while lander B is alive and busy with other work.
_dead_lander_pid = lander.pid
new_task("iota")
go, started, report = blocking_gate("g3")
_out, _req = enq("iota")
lander_b = start_lander()
check("a second lander's gate started", wait_for(started))
_rec = run("reconcile", cwd=repo).stdout
check(
    "reconcile keeps the dead lander's claim PROCESSING and stale while another lander runs",
    re.search(rf"PROCESSING\s+eta\s.*owner pid {_dead_lander_pid}; stale", _rec)
    is not None,
    _rec,
)
check(
    "and the live lander's own claim is PROCESSING and not stale",
    re.search(rf"PROCESSING\s+iota\s.*owner pid {lander_b.pid}$", _rec, re.M)
    is not None,
    _rec,
)
go.write_text("go\n")
lander_b.communicate(timeout=120)

print("\na lander that lost its claim does not acknowledge it")
new_task("kappa")
go, started, report = blocking_gate("g4")
_out, _req = enq("kappa")
lander = start_lander()
check("the gate started", wait_for(started))
_claimed = state / "processing" / _req.name
H.write_json(
    H.owner_path(_claimed),
    {**json.loads(H.owner_path(_claimed).read_text()), "token": "forged"},
)
go.write_text("go\n")
_stdout, _stderr = lander.communicate(timeout=120)
check(
    "the lander exits non-zero and says another lander owns the request",
    lander.returncode != 0 and "owned by another lander" in _stderr,
    _stdout + _stderr,
)
check(
    "the request stays in processing/ and nothing for it reaches done/",
    _claimed.exists() and not list((state / "done").glob("*-kappa-*")),
)
set_gate(repo, 0)

print("\nlock order: the lander holds its lock before it waits on meta.lock")
new_task("mu")
_out, _req = enq("mu")
_meta = os.open(state / "meta.lock", os.O_RDWR | os.O_CREAT)
fcntl.flock(_meta, fcntl.LOCK_EX)
lander = start_lander()
_end = time.monotonic() + 30
while lander.poll() is None and lock_free("lander.lock") and time.monotonic() < _end:
    time.sleep(0.05)
check(
    "a lander started while meta.lock is held takes the lander lock first",
    lander.poll() is None and not lock_free("lander.lock"),
)
check(
    "and waits on meta.lock with its request unclaimed, instead of finishing",
    lander.poll() is None and _req.exists(),
)
os.close(_meta)
_stdout, _stderr = lander.communicate(timeout=120)
check(
    "releasing meta.lock lets it finish: no deadlock",
    lander.returncode == 0 and "1 landed, 0 failed" in _stdout,
    _stdout + _stderr,
)

print("\nenqueue re-reads the manifest under meta.lock")
for _name, _change in (("ka", "gone"), ("kb", "baseline")):
    _wt = new_task(_name)
    _meta = os.open(state / "meta.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(_meta, fcntl.LOCK_EX)
    _marker = tmp / f"arrived-{_name}"
    _enq = helper(
        "enqueue",
        state,
        _marker,
        "enqueue",
        "--task",
        _name,
        "-m",
        "test: raced",
        "--file",
        "a.txt",
        cwd=_wt,
    )
    # The marker is written after enqueue's first read and before its wait on
    # meta.lock, so the change below lands strictly between the two reads.
    check(f"enqueue reached meta.lock ({_change})", wait_for(_marker))
    _manifest = state / "tasks" / f"{_name}.json"
    if _change == "gone":
        _manifest.unlink()
    else:
        _data = json.loads(_manifest.read_text())
        _data["baseline_sha"] = FIRST
        H.write_json(_manifest, _data)
    os.close(_meta)
    try:
        _stdout, _stderr = _enq.communicate(timeout=60)
    finally:
        reap(_enq)
    check(
        f"enqueue is refused when the manifest changed under it ({_change})",
        _enq.returncode != 0 and not list((state / "queue").glob(f"*-{_name}-*")),
        _stdout + _stderr,
    )

print("\na request is bound to the generation of the manifest it was queued under")
new_task("th")
_wt = tmp / "wt-th"
for _label, _gen in (("missing", None), ("different", 2)):
    _out, _req = enq("th")
    _forged = json.loads(_req.read_text())
    if _gen is None:
        _forged.pop("generation", None)
    else:
        _forged["generation"] = _gen
    _req.write_text(json.dumps(_forged))
    out = run("land", "--once", "--timeout", "120", cwd=repo)
    _last = H.read_ledger(state)[-1]
    check(
        f"a forged request with a {_label} generation is FAILED and named in the ledger",
        out.returncode != 0
        and _last["status"] == "FAILED"
        and _last["request"] == _req.name
        and "generation" in _last["detail"],
        _last.get("detail", ""),
    )
    check(
        f"and nothing was committed for the {_label} generation",
        git("rev-parse", "HEAD", cwd=_wt).stdout.strip() == SECOND,
    )
new_task("ph")
_pre = state / "tasks" / "ph.json"
_data = json.loads(_pre.read_text())
_data.pop("generation")
H.write_json(_pre, _data)
out, _ = enq("ph")
check(
    "enqueue on a manifest with no generation fails and says how to recover",
    out.returncode != 0
    and "main checkout" in out.stderr
    and "remove the task's manifest and worktree and provision it again" in out.stderr,
    out.stderr,
)
_out, _req = enq("th")
check(
    "a real request carries the manifest's generation",
    json.loads(_req.read_text()).get("generation")
    == json.loads((state / "tasks" / "th.json").read_text()).get("generation")
    == 1,
)
out = run("land", "--once", "--timeout", "120", cwd=repo)
check(
    "and lands",
    out.returncode == 0 and "1 landed, 0 failed" in out.stdout,
    out.stdout + out.stderr,
)


print("\nno git subprocess in this file may inherit the gate's environment")
# GIT_DIR + GIT_WORK_TREE reaching `git init` writes core.worktree into the
# SHARED config, aiming every linked worktree in the repo at one directory. It
# fails silently: unlike core.bare, every later command still succeeds, against
# the wrong tree. One unscrubbed call is enough, so the rule is checked here
# rather than left to review.
import ast as _ast

_source_lines = Path(__file__).read_text(encoding="utf-8").splitlines()
_unscrubbed = []
for _node in _ast.walk(_ast.parse("\n".join(_source_lines))):
    if not isinstance(_node, _ast.Call):
        continue
    _fn = _node.func
    if not (
        isinstance(_fn, _ast.Attribute)
        and _fn.attr == "run"
        and isinstance(_fn.value, _ast.Name)
        and _fn.value.id == "subprocess"
    ):
        continue
    _first = _node.args[0] if _node.args else None
    _head = _first.elts[0] if isinstance(_first, _ast.List) and _first.elts else None
    if isinstance(_head, _ast.Constant) and _head.value == "git":
        _marked = any(
            "# unscrubbed:" in _line
            for _line in _source_lines[max(0, _node.lineno - 2) : _node.lineno]
        )
        if not any(kw.arg == "env" for kw in _node.keywords) and not _marked:
            _unscrubbed.append(_node.lineno)
check(
    "every git subprocess passes an explicit environment",
    not _unscrubbed,
    f"unscrubbed at line(s) {_unscrubbed}",
)


tmpdir.cleanup()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
