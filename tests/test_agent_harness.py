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

import json
import os
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
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" - {detail}" if detail and not cond else ""))


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
    return subprocess.run([sys.executable, str(HARNESS), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=300,
                          env=_clean_env())


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env=_clean_env())


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
        "func/sources/*\n!func/sources/tracked.txt\n.env\nnested_cache/\n")
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
out = run("provision", "--task", "alpha", "--baseline", FIRST,
          "--path", str(tmp / "wt-alpha"), cwd=repo)
check("provision succeeds against an explicit baseline", out.returncode == 0,
      out.stdout + out.stderr)
manifest = json.loads((state / "tasks" / "alpha.json").read_text()) if \
    (state / "tasks" / "alpha.json").exists() else {}
check("the manifest records the resolved baseline SHA",
      manifest.get("baseline_sha") == FIRST,
      f"{manifest.get('baseline_sha')} != {FIRST}")
check("the baseline recorded is not HEAD", manifest.get("baseline_sha") != SECOND)
# The inputs come from a checkout that is still being edited, so a gate failure
# may be skew rather than the agent's change. The manifest has to say so.
check("the manifest records which checkout the inputs came from",
      manifest.get("source_checkout") == str(repo.resolve()),
      str(manifest.get("source_checkout")))
check("and that checkout's HEAD, which need not be the baseline",
      manifest.get("source_head") == SECOND and manifest["source_head"] != manifest["baseline_sha"],
      f"source_head={manifest.get('source_head')} baseline={manifest.get('baseline_sha')}")
check("and how many uncommitted paths it had at the time",
      isinstance(manifest.get("source_uncommitted_paths"), int),
      str(manifest.get("source_uncommitted_paths")))
check("provision warns out loud when the inputs are not from the baseline",
      "WARNING" in out.stdout and "not the baseline" in out.stdout, out.stdout)
check("the manifest records the gate the worktree will actually commit under",
      manifest.get("gate", "").endswith("pre-commit"), str(manifest.get("gate")))
# This repository points every worktree at one shared hooks directory, which is
# the shape the real one has, so the warning is the correct outcome here.
check("provision says so when the gate lies outside the worktree",
      "outside it" in out.stdout, out.stdout)
check("and gives the exact command that clears it",
      "config --worktree --unset core.hooksPath" in out.stdout, out.stdout)
check("the worktree is checked out at that baseline",
      git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip() == FIRST)
check("--baseline is mandatory, so HEAD can never be the accidental default",
      run("provision", "--task", "nob", "--path", str(tmp / "wt-nob"),
          cwd=repo).returncode != 0)

print("\nthe ignored inputs a checkout cannot carry are cloned in and verified")
inputs = tmp / "wt-alpha" / "func" / "sources"
check("the tracked file the checkout carried is still there",
      (inputs / "tracked.txt").read_text() == "tracked\n")
check("the untracked input beside it was cloned in, not skipped",
      (inputs / "supplied.bin").exists(),
      f"contents: {sorted(p.name for p in inputs.iterdir())}")
check("and so was one nested a directory deep",
      (inputs / "nested" / "deep.json").exists())
check("the supplied inputs are reported as cloned",
      any(c.startswith("func/sources/") for c in manifest.get("cloned_inputs", [])),
      str(manifest.get("cloned_inputs")))
# The provisioner asks git for the ignored paths, so anything ignored is a
# candidate for copying - including credentials. The deny-list is the only thing
# stopping a scratch worktree in /tmp from carrying the repository's .env.
check("the ignored .env exists in the checkout being cloned from",
      (repo / ".env").read_text() == "API_TOKEN=super-secret\n")
check("but credentials are never cloned into a worktree",
      not (tmp / "wt-alpha" / ".env").exists())
check("and .env is not even reported as a cloned input",
      ".env" not in manifest.get("cloned_inputs", []), str(manifest.get("cloned_inputs")))
check("a worktrees directory would be refused too, by name",
      not H.clonable(".claude/worktrees") and not H.clonable(".claude/worktrees/other"))
check("rebuildable caches are skipped", not H.clonable("a/__pycache__/b.pyc"))
check("but real ignored data is cloned", H.clonable("output/x.json"))
# Each platform is pinned here because each CI runner only exercises its own.
_platform = H.sys.platform
try:
    H.sys.platform = "darwin"
    check("macOS clones with the full-path /bin/cp -c, never a GNU cp on PATH",
          H.clone_argv("-R", "-n") == ["/bin/cp", "-cRn"] and H.clone_argv() == ["/bin/cp", "-c"],
          f"{H.clone_argv('-R', '-n')} {H.clone_argv()}")
    H.sys.platform = "linux"
    check("Linux never passes -c, which GNU cp rejects, and reflinks where it can",
          H.clone_argv("-R", "-n") == ["cp", "-R", "-n", "--reflink=auto"]
          and H.clone_argv() == ["cp", "--reflink=auto"],
          f"{H.clone_argv('-R', '-n')} {H.clone_argv()}")
finally:
    H.sys.platform = _platform
check("the clone gave the worktree its own inode, not a shared one",
      (inputs / "supplied.bin").stat().st_ino
      != (repo / "func" / "sources" / "supplied.bin").stat().st_ino)
(inputs / "supplied.bin").write_text("worktree-local change\n")
check("writing through the clone does not reach the source",
      (repo / "func" / "sources" / "supplied.bin").read_text() == "supplied by hand\n")
check("missing_after_clone finds a file the copy left behind",
      H.missing_after_clone(repo / "func" / "sources", tmp / "empty-dir")
      if (tmp / "empty-dir").mkdir() or True else False)

print("\na live socket among the inputs is not an incomplete clone")
sock_path = repo / "func" / "sources" / "live.sock"
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(sock_path))
left_behind = H.missing_after_clone(repo / "func" / "sources",
                                    tmp / "wt-alpha" / "func" / "sources")
check("the socket really is present in the source", sock_path.exists())
check("cp cannot carry it, and it is not counted as missing",
      "live.sock" not in left_behind, str(left_behind))
check("a genuinely missing regular file would still be counted",
      "supplied.bin" in H.missing_after_clone(repo / "func" / "sources",
                                              tmp / "no-such-worktree"))
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
listed = subprocess.run(["git", "ls-files", "--others", "--ignored",
                         "--exclude-standard", "--directory"],
                        cwd=dirt_repo, capture_output=True, text=True,
                        env=_clean_env()).stdout
check("git reports the content as ignored in the source checkout",
      "flow/logs/" in listed, listed)
out = run("provision", "--task", "dirty", "--baseline", dirt_first,
          "--path", str(dirt_tmp / "wt-dirty"), cwd=dirt_repo)
check("provision refuses rather than handing over a dirty worktree",
      out.returncode != 0, out.stdout)
check("and names what it carried in", "untracked path" in out.stderr, out.stderr.strip())
check("no manifest is written for the refused task",
      not (dirt_repo / ".git" / "agent-harness" / "tasks" / "dirty.json").exists())
dirt_dir.cleanup()

print("\na dirty target directory stops provisioning before anything is created")
dirty = tmp / "dirty"
dirty.mkdir()
(dirty / "someones-work.txt").write_text("uncommitted\n")
out = run("provision", "--task", "beta", "--baseline", FIRST, "--path", str(dirty), cwd=repo)
check("provision refuses a non-empty target", out.returncode != 0, out.stdout)
check("and says why", "not empty" in out.stderr, out.stderr.strip())
check("the target is left exactly as it was",
      (dirty / "someones-work.txt").read_text() == "uncommitted\n")
check("and no manifest was written for the refused task",
      not (state / "tasks" / "beta.json").exists())
out = run("provision", "--task", "gamma", "--baseline", FIRST,
          "--path", str(tmp / "wt-alpha"), cwd=repo)
check("provision refuses an existing worktree as a target", out.returncode != 0)
check("a task name cannot be provisioned twice",
      run("provision", "--task", "alpha", "--baseline", FIRST,
          "--path", str(tmp / "wt-alpha2"), cwd=repo).returncode != 0)

print("\nan agent cannot queue a path belonging to another worktree")
run("provision", "--task", "delta", "--baseline", FIRST,
    "--path", str(tmp / "wt-delta"), cwd=repo)
(tmp / "wt-alpha" / "a.txt").write_text("alpha edit\n")
(tmp / "wt-delta" / "a.txt").write_text("delta edit\n")
out = run("enqueue", "--task", "delta", "-m", "reach across", "--file",
          str(tmp / "wt-alpha" / "a.txt"), cwd=tmp / "wt-delta")
check("enqueue refuses a file outside the task's worktree", out.returncode != 0)
check("and names the boundary it refused", "outside task" in out.stderr, out.stderr.strip())
check("no request reached the queue", not list((state / "queue").glob("*delta*")))
check("enqueue requires at least one file",
      run("enqueue", "--task", "delta", "-m", "empty", cwd=tmp / "wt-delta").returncode != 0)

print("\nthe two worktrees hold different content at the same relative path")
check("wt-alpha keeps its own a.txt", (tmp / "wt-alpha" / "a.txt").read_text() == "alpha edit\n")
check("wt-delta keeps its own a.txt", (tmp / "wt-delta" / "a.txt").read_text() == "delta edit\n")

print("\nverification re-reads the log and disagrees with a lying exit code")
before = git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip()
git("add", "-A", cwd=tmp / "wt-alpha")
git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "alpha edit", cwd=tmp / "wt-alpha")
landed, sha, oneline, detail = H.verify_landed(tmp / "wt-alpha", "alpha edit", before)
check("a real commit with the requested subject verifies", landed, detail)
check("the verified SHA is the one git log reports",
      sha == git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip())
check("the abbreviated SHA on the log line prefixes it", sha.startswith(oneline.split(" ")[0]))
bad, _, _, why = H.verify_landed(tmp / "wt-alpha", "a subject nobody committed", before)
check("a different subject is not accepted as landed", not bad, why)
check("and the mismatch is named", "subject is" in why, why)
still, _, _, why = H.verify_landed(tmp / "wt-alpha", "alpha edit", sha)
check("an unmoved HEAD is not accepted as landed", not still, why)
check("and the reason is that HEAD did not move", "did not move" in why, why)

print("\nindex.lock is retried with backoff, not reported as a gate failure")
check("the backoff seam exists to be controlled",
      all(hasattr(H, n) for n in ("LOCK_TRIES", "LOCK_BACKOFF", "LOCK_BACKOFF_MAX")))
check("the retry budget is a parameter an operator can set, not a constant",
      "tries" in H.with_lock_backoff.__code__.co_varnames)
lock = Path(git("rev-parse", "--absolute-git-dir", cwd=tmp / "wt-delta").stdout.strip()) / "index.lock"
lock.write_text("held\n")
threading.Timer(1.2, lambda: lock.unlink(missing_ok=True)).start()
started = time.monotonic()
proc, retries = H.with_lock_backoff(
    ["git", "-C", str(tmp / "wt-delta"), "add", "--", "a.txt"], 60)
check("the command succeeds once the lock clears", proc.returncode == 0, proc.stderr.strip())
check("and it took at least one retry to get there", retries >= 1, f"retries={retries}")
check("which means it waited rather than failing fast",
      time.monotonic() - started >= 1.0)

lock.write_text("held forever\n")
H.LOCK_BACKOFF, H.LOCK_BACKOFF_MAX = 0.01, 0.01
proc, retries = H.with_lock_backoff(
    ["git", "-C", str(tmp / "wt-delta"), "add", "--", "a.txt"], 60, tries=3)
check("a lock that never clears exhausts the retries", retries == 3, f"retries={retries}")
check("and the real git exit code survives, unmasked", proc.returncode != 0)
check("with git's own words kept", "index.lock" in proc.stderr, proc.stderr.strip())
lock.unlink(missing_ok=True)
H.LOCK_BACKOFF, H.LOCK_BACKOFF_MAX = 0.5, 8.0

print("\na rejected gate lands nothing and says so with git's own stderr")
set_gate(repo, 1)
run("enqueue", "--task", "delta", "-m", "delta change", "--file", "a.txt", cwd=tmp / "wt-delta")
head_before = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
rejected = [r for r in ledger if r["task"] == "delta"]
check("the attempt is recorded", len(rejected) == 1, str(len(rejected)))
check("its status is FAILED", rejected and rejected[0]["status"] == "FAILED")
check("the ledger carries the gate's raw stderr",
      rejected and "gate ran" in rejected[0]["stderr"], str(rejected))
check("the row records when the attempt finished, not only when it began",
      rejected and rejected[0].get("finished", "") >= rejected[0]["ts"],
      str(rejected[0].get("finished")) if rejected else "")
check("and git's real exit code, not a rewritten one",
      rejected and rejected[0]["exit_code"] not in (0, None), str(rejected[0]["exit_code"]))
check("no commit was made", git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == head_before)
check("land exits non-zero when anything failed", out.returncode != 0, out.stdout)
check("the failure is visible on stdout too", "FAILED" in out.stdout, out.stdout)

print("\na passing gate lands, and the ledger SHA is the one git log reports")
set_gate(repo, 0)
run("enqueue", "--task", "delta", "-m", "delta change", "--file", "a.txt", cwd=tmp / "wt-delta")
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
good = [r for r in ledger if r["task"] == "delta" and r["status"] == "LANDED"]
check("the commit landed", len(good) == 1, out.stdout + out.stderr)
real = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
check("the ledger SHA matches the worktree HEAD", good and good[0]["sha"] == real,
      f"{good[0]['sha'] if good else None} != {real}")
check("the ledger message matches the commit subject",
      git("log", "--oneline", "-1", cwd=tmp / "wt-delta").stdout.strip().split(" ", 1)[1]
      == "delta change")
check("land exits zero when everything landed", out.returncode == 0, out.stdout)
# The repository under test sets a RELATIVE core.hooksPath, and the lander
# commits with `git -C <worktree>`. Git resolves a relative hooksPath against the
# process working directory, so before the lander pinned the verified directory
# onto the commit this hook was silently skipped and the commit landed ungated.
check("the gate actually executed on the landed commit",
      good and "gate ran" in good[0]["stderr"], str(good[0]["stderr"] if good else None))
check("and the ledger names the gate that ran",
      good and good[0]["gate"].endswith("hooks/pre-commit"), str(good[0].get("gate") if good else None))
check("the consumed request left the queue", not list((state / "queue").glob("*.json")))
check("and is retained for forensics", len(list((state / "done").glob("*.json"))) >= 2)
check("wt-alpha was untouched by landing wt-delta",
      (tmp / "wt-alpha" / "a.txt").read_text() == "alpha edit\n")

print("\nreconcile separates landed, queued and orphaned")
out = run("reconcile", "--json", cwd=repo)
report = json.loads(out.stdout)
states = {row["task"]: row["state"] for row in report["tasks"]}
check("the task whose commit landed reads LANDED", states.get("delta") == "LANDED", str(states))
check("the task holding an uncommitted edit reads ORPHANED",
      states.get("alpha") == "ORPHANED", str(states))
run("enqueue", "--task", "alpha", "-m", "alpha pending", "--file", "a.txt", cwd=tmp / "wt-alpha")
states = {row["task"]: row["state"]
          for row in json.loads(run("reconcile", "--json", cwd=repo).stdout)["tasks"]}
check("a waiting request outranks the orphan reading", states.get("alpha") == "QUEUED", str(states))
check("reconcile reports every provisioned task and no others",
      set(states) == {"alpha", "delta"}, str(set(states)))

print("\nan unwired gate lands nothing and says so")
# The gate is a property of the worktree, so a missing one fails that request
# rather than stopping the queue - another task's worktree may be wired fine.
git("config", "--unset", "core.hooksPath", cwd=repo)
head_before = git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("land exits non-zero when no pre-commit hook will run", out.returncode != 0, out.stdout)
check("the ledger records it rather than the lander dying silently",
      ledger and ledger[-1]["status"] == "FAILED", str(ledger[-1] if ledger else None))
check("and names the fix", "core.hooksPath" in ledger[-1]["detail"], ledger[-1]["detail"])
check("no commit was made",
      git("rev-parse", "HEAD", cwd=tmp / "wt-alpha").stdout.strip() == head_before)
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
git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "accepting hook", cwd=own_repo)
baseline = git("rev-parse", "HEAD", cwd=own_repo).stdout.strip()
# main then moves on to a hook that refuses everything.
accepting.write_text("#!/bin/sh\necho 'main hook refused' >&2\nexit 1\n")
accepting.chmod(0o755)
git("add", "-A", cwd=own_repo)
git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "refusing hook", cwd=own_repo)

own_wt = own_tmp / "wt-own"
out = run("provision", "--task", "own", "--baseline", baseline, "--path", str(own_wt),
          cwd=own_repo)
check("a worktree can be provisioned at the older, accepting baseline",
      out.returncode == 0, out.stdout + out.stderr)
check("main's checked-out hook refuses",
      "exit 1" in (own_repo / ".githooks" / "pre-commit").read_text())
check("the worktree's own checked-out hook accepts",
      "exit 0" in (own_wt / ".githooks" / "pre-commit").read_text())
resolved_dir, resolved_hook, problem = H.resolve_gate(own_wt)
check("the gate resolves inside the worktree, not the main checkout",
      problem is None and str(resolved_hook).startswith(str(own_wt.resolve())),
      f"{resolved_hook} (problem: {problem})")
check("a worktree gating on its own hook draws no foreign-gate warning",
      "outside it" not in out.stdout, out.stdout)

(own_wt / "a.txt").write_text("edited in the worktree\n")
run("enqueue", "--task", "own", "-m", "test: gated by my own hook", "--file", "a.txt",
    cwd=own_wt)
out = run("land", "--once", "--timeout", "120", cwd=own_repo)
own_state = own_repo / ".git" / "agent-harness"
own_ledger = [json.loads(x) for x in
              (own_state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("the commit lands, so the accepting worktree hook ran",
      own_ledger and own_ledger[-1]["status"] == "LANDED",
      str(own_ledger[-1] if own_ledger else out.stdout + out.stderr))
check("and it was the worktree's hook, not main's, that spoke",
      own_ledger and "baseline hook accepted" in own_ledger[-1]["stderr"],
      str(own_ledger[-1]["stderr"] if own_ledger else None))
check("main's refusing hook never ran",
      own_ledger and "main hook refused" not in own_ledger[-1]["stderr"])
check("the ledger names the exact hook file that gated the commit",
      own_ledger and own_ledger[-1]["gate"].startswith(str(own_wt.resolve())),
      str(own_ledger[-1]["gate"] if own_ledger else None))
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
    leaked = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=probe_repo,
                            capture_output=True, text=True).stdout.strip()
finally:
    os.environ.pop("GIT_DIR", None)
check("a scrubbed call lands in its own repository",
      Path(scrubbed).resolve() == (probe_repo / ".git").resolve(), scrubbed)
check("while an unscrubbed one is captured by GIT_DIR, so the scrub does real work",
      Path(leaked).resolve() == (decoy / ".git").resolve(), leaked)

print("\na ref moved by something inside the gate is reported, not hidden")
# The peer defect this reproduces: a test inside the verify spine inherits
# GIT_DIR and commits through the branch being landed on. The commit fails, so
# the operator sees only a red gate, while the ref has moved underneath them.
hijack = repo / "hooks" / "pre-commit"
hijack.write_text("#!/bin/sh\n"
                  "git commit --no-verify --allow-empty -m 'hijacked by the gate' "
                  ">/dev/null 2>&1\n"
                  "echo 'gate ran' >&2\n"
                  "exit 1\n")
hijack.chmod(0o755)
(tmp / "wt-delta" / "a.txt").write_text("delta once more\n")
run("enqueue", "--task", "delta", "-m", "test: delta hijack probe", "--file", "a.txt",
    cwd=tmp / "wt-delta")
before_hijack = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
hijacked = ledger[-1]
after_hijack = git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip()
check("the gate really did move the ref", after_hijack != before_hijack,
      f"{before_hijack} -> {after_hijack}")
check("the landing is recorded as FAILED, not LANDED", hijacked["status"] == "FAILED",
      str(hijacked))
check("and the ledger says the ref moved rather than only that the gate failed",
      "HEAD moved from" in hijacked["detail"], hijacked["detail"])
check("it names the branch that was written to",
      "agent/delta" in hijacked["detail"], hijacked["detail"])
set_gate(repo, 0)

print("\na commit made outside the queue is refused, not landed on top of")
# The hijack above left wt-delta one commit past its last landed SHA, which is
# exactly the state a task agent running `git commit` itself produces.
(tmp / "wt-delta" / "a.txt").write_text("delta after a direct commit\n")
run("enqueue", "--task", "delta", "-m", "test: delta after direct commit", "--file", "a.txt",
    cwd=tmp / "wt-delta")
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
outside = ledger[-1]
check("a request whose worktree HEAD moved outside the queue is FAILED",
      outside["status"] == "FAILED" and outside["message"] == "test: delta after direct commit", str(outside))
check("and the ledger says a commit was made outside the queue",
      "outside the queue" in outside["detail"], outside["detail"])
check("nothing was committed on top of it",
      git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == after_hijack)

# The request file is written by the agent, so a forged baseline must not move
# the lander's expectation: the manifest written at provision decides.
(tmp / "wt-delta" / "a.txt").write_text("delta with a forged baseline\n")
run("enqueue", "--task", "delta", "-m", "test: forged baseline", "--file", "a.txt", cwd=tmp / "wt-delta")
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["baseline_sha"] = after_hijack
forged.write_text(json.dumps(request))
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a request whose baseline differs from the task manifest is FAILED",
      ledger[-1]["status"] == "FAILED" and "does not match the task manifest" in ledger[-1]["detail"],
      ledger[-1].get("detail", ""))

# Repair the fixture: back to the last landed commit, so later cases start clean.
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")

print("\na task name cannot leave the harness directories")
for bad in ("../escape", "a/b", ".hidden", "x..y", ""):
    out = run("provision", "--task", bad, "--baseline", FIRST, "--path", str(tmp / "wt-bad"), cwd=repo)
    check(f"provision refuses task name {bad!r}", out.returncode != 0 and "invalid task name" in out.stderr,
          out.stderr)
out = run("enqueue", "--task", "../../tasks/delta", "-m", "x", "--file", "a.txt", cwd=tmp / "wt-delta")
check("enqueue refuses a traversing task name", out.returncode != 0 and "invalid task name" in out.stderr,
      out.stderr)
# A request file is agent-written, so a traversing task inside it must fail at land.
(tmp / "wt-delta" / "a.txt").write_text("delta with a traversing task\n")
run("enqueue", "--task", "delta", "-m", "test: traversing task", "--file", "a.txt", cwd=tmp / "wt-delta")
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["task"] = "../tasks/delta"
forged.write_text(json.dumps(request))
out = run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a queued request with a traversing task name is FAILED at land",
      ledger[-1]["status"] == "FAILED" and "invalid task name" in ledger[-1]["detail"], ledger[-1].get("detail", ""))
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
git("checkout", "-q", "--", ".", cwd=tmp / "wt-delta")

print("\nthe lander commits only what was queued")
(tmp / "wt-delta" / "deep").mkdir(exist_ok=True)
for name in (".", "deep"):
    out = run("enqueue", "--task", "delta", "-m", "x", "--file", name, cwd=tmp / "wt-delta")
    check(f"enqueue refuses the directory {name!r}", out.returncode != 0 and "is a directory" in out.stderr, out.stderr)
(tmp / "wt-delta" / "deep").rmdir()
for pattern in ("*.txt", ":(glob)**", "a?.txt"):
    out = run("enqueue", "--task", "delta", "-m", "x", "--file", pattern, cwd=tmp / "wt-delta")
    check(f"enqueue refuses the pattern {pattern!r}", out.returncode != 0 and "is a pattern" in out.stderr, out.stderr)
(tmp / "wt-delta" / "sneaky.txt").write_text("never queued\n")
(tmp / "wt-delta" / "a.txt").write_text("delta via a forged pattern\n")
run("enqueue", "--task", "delta", "-m", "test: forged pattern", "--file", "a.txt", cwd=tmp / "wt-delta")
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = ["*.txt"]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a pattern forged into a request lands nothing", ledger[-1]["status"] == "FAILED"
      and "not one literal file" in ledger[-1]["detail"], str(ledger[-1]))
run("enqueue", "--task", "delta", "-m", "test: forged dot", "--file", "a.txt", cwd=tmp / "wt-delta")
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = ["."]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a '.' forged into a request lands nothing", ledger[-1]["status"] == "FAILED"
      and "not one literal file" in ledger[-1]["detail"], str(ledger[-1]))
run("enqueue", "--task", "delta", "-m", "test: forged type", "--file", "a.txt", cwd=tmp / "wt-delta")
forged = sorted((state / "queue").glob("*delta*"))[-1]
request = json.loads(forged.read_text())
request["files"] = [5]
forged.write_text(json.dumps(request))
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a non-string path forged into a request is FAILED, not a lander crash", ledger[-1]["status"] == "FAILED"
      and "not one literal file" in ledger[-1]["detail"], str(ledger[-1]))
check("and commits no file it matches", git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == before_hijack)
check("and stages no file it matches, so the task's index stays clean",
      git("diff", "--cached", "--name-only", cwd=tmp / "wt-delta").stdout.strip() == "",
      git("diff", "--cached", "--name-only", cwd=tmp / "wt-delta").stdout)
(tmp / "wt-delta" / "sneaky.txt").unlink()
git("checkout", "-q", "--", ".", cwd=tmp / "wt-delta")
git("mv", "a.txt", "renamed.txt", cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "renamed.txt").write_text("renamed and edited\n")
run("enqueue", "--task", "delta", "-m", "test: rename half-queued", "--file", "renamed.txt", cwd=tmp / "wt-delta")
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a staged rename whose source was not queued is refused, naming the source",
      ledger[-1]["status"] == "FAILED" and "a.txt" in ledger[-1]["detail"], ledger[-1].get("detail", ""))
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "renamed.txt").unlink(missing_ok=True)

(tmp / "wt-delta" / "smuggled.txt").write_text("staged by the agent, never queued\n")
git("add", "smuggled.txt", cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "a.txt").write_text("delta, queued\n")
run("enqueue", "--task", "delta", "-m", "test: queued only", "--file", "a.txt", cwd=tmp / "wt-delta")
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a request whose worktree index holds unqueued staged files is FAILED",
      ledger[-1]["status"] == "FAILED" and "smuggled.txt" in ledger[-1]["detail"], ledger[-1].get("detail", ""))
check("and nothing was committed", git("rev-parse", "HEAD", cwd=tmp / "wt-delta").stdout.strip() == before_hijack)
git("reset", "-q", "--hard", before_hijack, cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "smuggled.txt").unlink(missing_ok=True)
(tmp / "wt-delta" / "a.txt").write_text("delta, queued and staged by the agent\n")
git("add", "a.txt", cwd=tmp / "wt-delta")
run("enqueue", "--task", "delta", "-m", "test: queued and staged", "--file", "a.txt", cwd=tmp / "wt-delta")
run("land", "--once", "--timeout", "120", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
check("a queued file the agent also staged still lands", ledger[-1]["status"] == "LANDED", str(ledger[-1]))
files = git("show", "--name-only", "--format=", "HEAD", cwd=tmp / "wt-delta").stdout.split()
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
slow.write_text("#!/bin/sh\n"
                "sleep 300 &\n"
                f"echo $! > {marker}\n"
                "sleep 300\n")
slow.chmod(0o755)
(tmp / "wt-delta" / "a.txt").write_text("delta slow gate\n")
run("enqueue", "--task", "delta", "-m", "test: delta slow gate", "--file", "a.txt",
    cwd=tmp / "wt-delta")
out = run("land", "--once", "--timeout", "3", cwd=repo)
ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines() if x.strip()]
# Select by this request's own message rather than by position - the row order
# is not this check's business.
mine = [r for r in ledger if r["message"] == "test: delta slow gate"]
check("the attempt was recorded at all", len(mine) == 1,
      f"{len(mine)} rows. lander rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}")
timed_out = mine[-1] if mine else {"status": "?", "detail": "no row"}
check("the landing is FAILED on the gate timeout",
      timed_out["status"] == "FAILED" and "exceeded" in timed_out["detail"],
      timed_out["detail"])
check("the gate really did spawn a grandchild that outlives its parent",
      marker.exists() and marker.read_text().strip().isdigit(),
      f"marker: {marker.read_text() if marker.exists() else 'absent'}")
grandchild = marker.read_text().strip()
for _ in range(30):                       # the kill escalates, so allow for it
    if not H.alive(grandchild):
        break
    time.sleep(0.1)
check("and it was killed with the rest of the tree, not orphaned",
      not H.alive(grandchild), f"pid {grandchild} still running")
check("kill_group reports success on a group that is already gone",
      H.kill_group(int(grandchild)) is True)
check("kill_group on None is a no-op rather than an error", H.kill_group(None) is True)
# It runs from `finally` and from a signal handler. The first version let
# PermissionError escape its own liveness probe, which killed the lander
# mid-landing and lost the ledger row - the exact evidence it exists to write.
check("a group that exists but is not ours to signal is handled, not raised",
      H._group_gone(1) is True)
check("kill_group never raises on a group it cannot touch", H.kill_group(1) is True)
# On Linux killpg(1) is kill(-1): every process this user owns, the test runner
# included. Group 1 must be refused before any signal is sent, on every platform.
_killpg, _sent = H.os.killpg, []
try:
    H.os.killpg = lambda pgid, sig: _sent.append((pgid, sig))
    H.kill_group(1), H.kill_group(0), H._group_gone(1)
finally:
    H.os.killpg = _killpg
check("group 1 and below are never signalled at all", _sent == [], str(_sent))
def _refuse(pgid, sig):
    raise PermissionError(1, "not ours")
try:
    H.os.killpg = _refuse
    check("a PermissionError from a real group is handled, not raised",
          H._group_gone(424242) is True and H.kill_group(424242) is True)
finally:
    H.os.killpg = _killpg
check("the lander installs a shutdown handler, so a stop is not a leak",
      "signal.signal" in (ROOT / ".claude" / "harness" / "agent_harness.py").read_text())
set_gate(repo, 0)

print("\ntwo live landers cannot run at once, but a dead one cannot wedge the queue")
check("a running process reads as alive", H.alive(str(os.getpid())))
check("a process that has exited does not", not H.alive("999999"))
check("and neither does an unreadable holder", not H.alive("") and not H.alive("nonsense"))

lander_lock = state / "lander.lock"
lander_lock.write_text(f"{os.getpid()}\n")
out = run("land", "--once", cwd=repo)
check("a second lander refuses while a live process holds the lock",
      out.returncode != 0, out.stdout)
check("and says who holds it", "already holds" in out.stderr, out.stderr.strip())

# A lander killed between taking the lock and releasing it leaves this behind.
# Before the takeover check, every later lander refused forever and the queue
# needed a human to unwedge it. That happened during the harness's trial run.
lander_lock.write_text("999999\n")
run("enqueue", "--task", "delta", "-m", "after a crashed lander", "--file", "a.txt",
    cwd=tmp / "wt-delta")
(tmp / "wt-delta" / "a.txt").write_text("delta again\n")
out = run("land", "--once", "--timeout", "120", cwd=repo)
check("a lock left by a dead process is taken over, not obeyed forever",
      "taking over" in out.stdout, out.stdout + out.stderr)
check("and the queue drains rather than wedging",
      not list((state / "queue").glob("*.json")), out.stdout)
check("the lock is released when that lander finishes", not lander_lock.exists())

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
    if not (isinstance(_fn, _ast.Attribute) and _fn.attr == "run"
            and isinstance(_fn.value, _ast.Name) and _fn.value.id == "subprocess"):
        continue
    _first = _node.args[0] if _node.args else None
    _head = (_first.elts[0] if isinstance(_first, _ast.List) and _first.elts else None)
    if isinstance(_head, _ast.Constant) and _head.value == "git":
        _marked = any("# unscrubbed:" in _line for _line in
                      _source_lines[max(0, _node.lineno - 2):_node.lineno])
        if not any(kw.arg == "env" for kw in _node.keywords) and not _marked:
            _unscrubbed.append(_node.lineno)
check("every git subprocess passes an explicit environment",
      not _unscrubbed, f"unscrubbed at line(s) {_unscrubbed}")


tmpdir.cleanup()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
