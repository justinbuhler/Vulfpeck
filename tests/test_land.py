"""Lander tests. Throwaway repos in tmp only - never touches this repository,
but only because every git subprocess here runs with GIT_* scrubbed. `cwd=` does
not decide which repository git acts on; GIT_DIR does, and it wins. The suite
runs inside the pre-commit hook, which exports GIT_DIR and GIT_INDEX_FILE, so an
unscrubbed call commits into the repository being committed to. On 2026-09-02
that put this file's fixture commit - "base", author `t <t@t>` - onto another
session's branch ref. Every subprocess.run below must pass env=scrubbed_env().

The gate is overridden via LAND_GATE so these run in seconds; the real gate is
the verify spine and is exercised by landing for real, not here.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_harness_receipts import approval

LAND = Path(__file__).resolve().parents[1] / "scripts" / "land.sh"

# These tests run real hooks, which record decisions (scripts/harness_events.py).
# Point the record at a scratch file so fixtures never enter the real one.
os.environ["HARNESS_EVENTS_LOG"] = str(Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


# git reads these from the environment and ignores cwd when they are set. The
# pre-commit hook exports GIT_DIR and GIT_INDEX_FILE, and the whole suite runs
# inside that hook - so an unscrubbed helper does not operate on its throwaway
# repo, it commits into the repository being committed to. Observed 2026-09-02:
# this moved another session's branch ref to a commit named "base".
GIT_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX",
           "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
           "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


# The scrub list above is what this harness knows is dangerous; land.sh is what
# actually defends against it. They drifted: land.sh unset seven of these nine,
# leaving GIT_ALTERNATE_OBJECT_DIRECTORIES and GIT_CEILING_DIRECTORIES live, and
# nothing noticed because GIT_ENV was only ever used to clean this file's own
# subprocesses. Asserting one against the other is what keeps them together.
_land_src = (Path(__file__).resolve().parents[1] / "scripts" / "land.sh").read_text()
_unset = _land_src.split("unset GIT_DIR", 1)[1].split("\n\n", 1)[0]
_missing = [v for v in GIT_ENV if v != "GIT_DIR" and v not in _unset]
check("land.sh unsets every git environment variable this harness scrubs",
      not _missing, f"land.sh never unsets {_missing}")


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


LANDER_ROOT = tempfile.mkdtemp(prefix="harness-land-wt.")


def land(cwd, gate="true", **env):
    # Pin every knob land.sh reads. Inheriting os.environ is right for PATH and
    # HOME, and wrong for the lander's own switches: running the suite from a
    # shell that exported LAND_ALLOW_UNTRACKED=1 silently turned the "refused by
    # default" cases into lands, which read as flakiness rather than leakage.
    # `**env` comes last so a test can still set a knob on purpose.
    e = scrubbed_env()
    e.update(LAND_GATE=gate, LAND_LOCK_WAIT_SECS="10", LAND_WORKTREE_ROOT=LANDER_ROOT,
             LAND_ALLOW_UNTRACKED="0", LAND_ATTEMPTS="3", LAND_EXPECT="")
    e.update(env)
    candidate = git(cwd, "rev-parse", "HEAD")
    e["LAND_EXPECT"] = e.get("LAND_EXPECT") or candidate
    e["LAND_RECEIPT"] = str(approval(Path(cwd), candidate))
    if gate is None:
        e.pop("LAND_GATE")
    e.pop("VERIFY_LINT_ONLY", None)
    return subprocess.run(["bash", str(LAND)], cwd=str(cwd), capture_output=True, text=True, env=e)


def assert_contained(d):
    """Refuse to touch anything unless git resolves to this fixture's own repo.

    A scrub you cannot prove is working reads exactly like a scrub that works.
    This asserts containment positively, before any write, so a regression that
    strips or bypasses scrubbed_env() fails here instead of committing into
    whatever repository GIT_DIR happens to name.
    """
    got = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=str(d),
                         capture_output=True, text=True, env=scrubbed_env()).stdout.strip()
    want = str((Path(d) / ".git").resolve())
    if got != want:
        raise AssertionError(
            f"fixture escaped its sandbox: git resolves to {got!r}, expected {want!r}. "
            "Refusing to write - this is the ref-hijack of 2026-09-02.")


def repo():
    """A throwaway repo with `main` checked out at the root and one commit."""
    d = Path(tempfile.mkdtemp(prefix="harness-land-test."))
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "t@t")
    git(d, "config", "user.name", "t")
    git(d, "config", "commit.gpgsign", "false")
    assert_contained(d)          # before the first write, never after
    (d / "base.txt").write_text("base\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    return d


def agent(d, name, filename=None, body="work\n"):
    """A worktree on its own branch with one commit - the shape a real agent lands from."""
    wt = d / f"wt-{name}"
    git(d, "worktree", "add", "-q", "-b", name, str(wt), "main")
    (wt / (filename or f"{name}.txt")).write_text(body)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"{name} work")
    return wt


def ancestor(d, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(d),
                          env=scrubbed_env()).returncode == 0


# --- a clean land ------------------------------------------------------------
d = repo()
wt = agent(d, "alpha")
worktrees_before = len(git(d, "worktree", "list").splitlines())
cand = git(wt, "rev-parse", "HEAD")
r = land(wt)
check("clean land exits 0", r.returncode == 0, r.stdout + r.stderr)
check("clean land prints a LANDED receipt", "LANDED" in r.stdout, r.stdout)
check("receipt names the candidate sha", cand in r.stdout, r.stdout)
check("the reviewed candidate is an ancestor of main", ancestor(d, cand, "main"))
check("main advanced past the candidate (--no-ff kept a merge commit)",
      git(d, "rev-parse", "main") != cand)
check("main's tip is a merge commit", len(git(d, "rev-list", "--parents", "-1", "main").split()) == 3)
check("the agent's own branch is untouched", git(wt, "rev-parse", "HEAD") == cand)
check("the stable integration lock remains", (d / ".git/harness/locks/integration.lock").exists())
check("the lander worktree is kept for reuse",
      len(git(d, "worktree", "list").splitlines()) == worktrees_before + 1,
      git(d, "worktree", "list"))
check("the lander worktree lives outside .git/, /output/, /.venv/ and /worktrees/",
      not any(x in git(d, "worktree", "list").split("\n")[-1]
              for x in ("/.git/", "/output/", "/.venv/", "/worktrees/")),
      git(d, "worktree", "list"))

# Landing the same branch again must reuse the worktree, not build a second one:
# reuse is the whole point of making it persistent.
r_again = land(wt)
check("re-landing an already-landed branch is a no-op success",
      r_again.returncode == 0, r_again.stdout + r_again.stderr)
check("the second land reused the worktree", "reusing" in r_again.stdout, r_again.stdout)
check("reuse did not add a second lander worktree",
      len(git(d, "worktree", "list").splitlines()) == worktrees_before + 1,
      git(d, "worktree", "list"))
check("re-landing does not claim a LANDED receipt for work it did not do",
      "LANDED" not in r_again.stdout, r_again.stdout)
check("re-landing says it was already landed", "already landed" in r_again.stdout, r_again.stdout)

# The receipt is cosmetic; skipping the gate is the load-bearing half. A re-land
# merges to the tree main already has, so gating it re-tests main - tens of
# minutes on this repo. Count gate runs rather than trust the message.
d_g = repo()
wt_g = agent(d_g, "idem")
counter = Path(tempfile.mkdtemp(prefix="harness-land-gate.")) / "runs"
gate = f"printf x >> {counter}"
check("first land of the candidate exits 0", land(wt_g, gate=gate).returncode == 0)
runs_after_first = counter.read_text() if counter.exists() else ""
r_idem = land(wt_g, gate=gate)
check("re-landing exits 0", r_idem.returncode == 0, r_idem.stdout + r_idem.stderr)
check("re-landing did not re-run the gate",
      (counter.read_text() if counter.exists() else "") == runs_after_first,
      f"gate ran {len(counter.read_text())} times, expected {len(runs_after_first)}")

# --- a second land sees the first --------------------------------------------
main_after_first = git(d, "rev-parse", "main")
wt2 = agent(d, "bravo")
cand2 = git(wt2, "rev-parse", "HEAD")
r2 = land(wt2)
check("a second land exits 0", r2.returncode == 0, r2.stdout + r2.stderr)
check("the first candidate is still an ancestor of main", ancestor(d, cand, "main"))
check("the second candidate is an ancestor of main", ancestor(d, cand2, "main"))
check("the second land based itself on the first land's result",
      f"base={main_after_first}" in r2.stdout, r2.stdout)
check("both agents' files are present on main",
      git(d, "ls-tree", "--name-only", "main") .count("txt") >= 2, git(d, "ls-tree", "--name-only", "main"))

# --- the gate is load-bearing -------------------------------------------------
d3 = repo()
wt3 = agent(d3, "charlie")
before = git(d3, "rev-parse", "main")
r3 = land(wt3, gate="false")
check("a failing gate exits non-zero", r3.returncode != 0, r3.stdout + r3.stderr)
check("a failing gate leaves main untouched", git(d3, "rev-parse", "main") == before)
check("a failing gate prints no LANDED receipt", "LANDED" not in r3.stdout, r3.stdout)

# A refresh after the swap cannot turn an integrated candidate into a failed land.
with tempfile.TemporaryDirectory(prefix="land-refresh-tools.") as td:
    refresh_repo = repo()
    (refresh_repo / ".gitignore").write_text(".codegraph/\n")
    (refresh_repo / "scripts").mkdir()
    (refresh_repo / "scripts/codegraph_freshness.py").write_text('''
import json, os, subprocess, sys
from pathlib import Path
root = str(Path(sys.argv[sys.argv.index('--root') + 1]).resolve())
main = str(Path(os.environ['REFRESH_MAIN']).resolve())
integrated = subprocess.run(['git', '-C', main, 'merge-base', '--is-ancestor',
                            os.environ['REFRESH_CANDIDATE'], 'main']).returncode == 0
failed = root == main and (integrated or os.environ.get('REFRESH_FAIL_EARLY') == '1')
with open(os.environ['REFRESH_TRACE'], 'a') as stream:
    stream.write(json.dumps({'root': root, 'integrated': integrated, 'failed': failed}) + '\\n')
print('fixture refresh failed after integration' if failed else 'fixture index ready')
sys.exit(1 if failed else 0)
''')
    git(refresh_repo, "add", "-A")
    git(refresh_repo, "commit", "-qm", "refresh fixture")
    (refresh_repo / ".codegraph").mkdir()
    refresh_wt = agent(refresh_repo, "refresh-pending")
    refresh_candidate = git(refresh_wt, "rev-parse", "HEAD")
    tools = Path(td)
    uv = tools / "uv"
    uv.write_text(f'#!/bin/bash\nshift; shift; shift; exec {shlex.quote(sys.executable)} "$@"\n')
    uv.chmod(0o755)
    trace = tools / "refresh.jsonl"
    gate_ran = tools / "gate-ran"
    early = land(refresh_wt, gate=f"touch {shlex.quote(str(gate_ran))}",
                 PATH=f"{tools}:{os.environ['PATH']}", REFRESH_MAIN=str(refresh_repo),
                 REFRESH_CANDIDATE=refresh_candidate, REFRESH_TRACE=str(trace), REFRESH_FAIL_EARLY="1")
    check("pre-gate index failure refuses integration before running the gate",
          early.returncode != 0 and not gate_ran.exists()
          and not ancestor(refresh_repo, refresh_candidate, "main"), early.stdout + early.stderr)
    trace.write_text("")
    result = land(refresh_wt, PATH=f"{tools}:{os.environ['PATH']}",
                  REFRESH_MAIN=str(refresh_repo), REFRESH_CANDIDATE=refresh_candidate,
                  REFRESH_TRACE=str(trace), REFRESH_FAIL_EARLY="0")
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    check("only the post-swap refresh fails in the regression fixture",
          calls and calls[-1]["failed"] and all(not row["failed"] for row in calls[:-1]), str(calls))
    check("post-integration refresh failure keeps a successful land exit",
          result.returncode == 0, result.stdout + result.stderr)
    check("post-integration refresh failure preserves the integration receipt",
          f"LANDED candidate={refresh_candidate}" in result.stdout, result.stdout + result.stderr)
    check("post-integration refresh failure names the pending refresh",
          "land succeeded; CodeGraph refresh pending" in result.stdout, result.stdout + result.stderr)
    check("post-integration refresh failure leaves the reviewed candidate on main",
          ancestor(refresh_repo, refresh_candidate, "main"))

# --- compare-and-swap catches a main that moved under us ----------------------
# The gate runs while we hold the lock; a gate that moves `main` simulates a
# writer that bypassed the lock entirely. update-ref must refuse the swap.
d4 = repo()
wt4 = agent(d4, "delta")
r4 = land(wt4, gate=f"git -C {d4} commit -q --allow-empty -m intruder")
intruder = git(d4, "rev-parse", "main")
check("a main that moved mid-land is refused", r4.returncode != 0, r4.stdout + r4.stderr)
check("the intruding commit survives - we did not clobber it",
      git(d4, "log", "-1", "--format=%s", "main") == "intruder",
      git(d4, "log", "-1", "--format=%s", "main"))
check("the candidate never landed", not ancestor(d4, git(wt4, "rev-parse", "HEAD"), "main"))
check("the refusal says main moved", "moved" in (r4.stdout + r4.stderr).lower(), r4.stdout + r4.stderr)

# --- refusals -----------------------------------------------------------------
d5 = repo()
r5 = land(d5)
check("landing from main itself is refused", r5.returncode != 0, r5.stdout + r5.stderr)
check("the refusal names main", "main" in (r5.stdout + r5.stderr).lower())

wt6 = agent(d5, "echo")
(wt6 / "echo.txt").write_text("uncommitted\n")
r6 = land(wt6)
check("a dirty working tree is refused", r6.returncode != 0, r6.stdout + r6.stderr)
check("a dirty tree is not landed", not ancestor(d5, git(wt6, "rev-parse", "HEAD"), "main"))

git(wt6, "checkout", "-q", "--", ".")
git(wt6, "checkout", "-q", "--detach")
r7 = land(wt6)
check("a detached HEAD is refused", r7.returncode != 0, r7.stdout + r7.stderr)

# Untracked files block by default but must be nameable and overridable.
d12 = repo()
wt12 = agent(d12, "lima")
(wt12 / "stray.txt").write_text("a tool dropping\n")
cand12 = git(wt12, "rev-parse", "HEAD")
r12 = land(wt12)
check("an untracked file is refused by default", r12.returncode != 0, r12.stdout + r12.stderr)
check("the refusal names the untracked file", "stray.txt" in (r12.stdout + r12.stderr),
      r12.stdout + r12.stderr)
r13 = land(wt12, LAND_ALLOW_UNTRACKED="1")
check("LAND_ALLOW_UNTRACKED=1 lands anyway", r13.returncode == 0, r13.stdout + r13.stderr)
check("the override is announced in the output", "untracked" in r13.stdout, r13.stdout)
check("the untracked file did not land", "stray.txt" not in git(d12, "ls-tree", "--name-only", "main"),
      git(d12, "ls-tree", "--name-only", "main"))
check("the candidate still landed", ancestor(d12, cand12, "main"))

# --- a conflict fails the land, not the repository ----------------------------
d8 = repo()
wt8a = agent(d8, "foxtrot", filename="same.txt", body="from foxtrot\n")
wt8b = agent(d8, "golf", filename="same.txt", body="from golf\n")
land(wt8a)
main_after_first = git(d8, "rev-parse", "main")
cand8b = git(wt8b, "rev-parse", "HEAD")
r8 = land(wt8b)
check("a conflicting land exits non-zero", r8.returncode != 0, r8.stdout + r8.stderr)
check("a conflicting land leaves main untouched", git(d8, "rev-parse", "main") == main_after_first)
check("a conflicting land leaves the agent's branch untouched", git(wt8b, "rev-parse", "HEAD") == cand8b)
check("a conflicting land leaves the agent's worktree clean (merge ran elsewhere)",
      git(wt8b, "status", "--porcelain") == "", git(wt8b, "status", "--porcelain"))
check("a conflicting land releases the lock so the next agent can proceed",
      land(agent(d8, "hotel")).returncode == 0)

# --- the lock actually serialises -------------------------------------------
# Two landers racing off the same base. With the lock, both land: the second
# reads a main the first already moved. Without it, the second's compare-and-swap
# is refused and its work is lost. A slow gate widens the window.
import threading

d9 = repo()
wt9a, wt9b = agent(d9, "india"), agent(d9, "juliett")
c9a, c9b = git(wt9a, "rev-parse", "HEAD"), git(wt9b, "rev-parse", "HEAD")
out = {}
race_errors = {}


def race(key, wt):
    try:
        out[key] = land(wt, gate="sleep 2")
    except Exception:
        import traceback
        race_errors[key] = traceback.format_exc()


ts = [threading.Thread(target=race, args=(k, w)) for k, w in (("a", wt9a), ("b", wt9b))]
for t in ts:
    t.start()
for t in ts:
    t.join()

if race_errors:
    raise AssertionError("concurrent lander helper failed:\n" + "\n".join(f"{key}: {error}" for key, error in race_errors.items()))

check("both concurrent landers exit 0", out["a"].returncode == 0 and out["b"].returncode == 0,
      f"a={out['a'].returncode} {out['a'].stderr} / b={out['b'].returncode} {out['b'].stderr}")
check("neither concurrent land was lost from main",
      ancestor(d9, c9a, "main") and ancestor(d9, c9b, "main"))
check("the two lands serialised into a chain, not a race",
      len(git(d9, "rev-list", "main").splitlines()) == 5,
      git(d9, "log", "--oneline", "main"))
check("the lock is released after a race", not (d9 / ".git" / "land.lock").exists())
check("contended lands serialize verification before reading their bases",
      "attempts=1" in out["a"].stdout and "attempts=1" in out["b"].stdout,
      out["a"].stdout + "\n---\n" + out["b"].stdout)

# --- the hook enforces the lander, without blocking ordinary agent commits ----
HOOKS = Path(__file__).resolve().parents[1] / ".githooks"

d10 = repo()
git(d10, "config", "core.hooksPath", str(HOOKS))
(d10 / "direct.txt").write_text("committed straight onto main\n")
git(d10, "add", "-A")
r10 = subprocess.run(["git", "commit", "-m", "direct"], cwd=str(d10), capture_output=True,
                     text=True, env=scrubbed_env())
check("the hook blocks a direct commit on main", r10.returncode != 0, r10.stdout + r10.stderr)
check("the block names the lander", "land.sh" in (r10.stdout + r10.stderr), r10.stdout + r10.stderr)
check("nothing was committed to main", git(d10, "rev-list", "--count", "main") == "1")

# The same hook must leave agent branches alone - ORCH-03 requires agents to
# commit their candidate before review.
git(d10, "checkout", "-q", "-b", "kilo")
r11 = subprocess.run(["git", "commit", "-m", "agent work"], cwd=str(d10), capture_output=True,
                     text=True, env=scrubbed_env())
check("the hook allows a commit on an agent branch", r11.returncode == 0, r11.stdout + r11.stderr)
check("the agent's commit landed on its own branch", git(d10, "rev-list", "--count", "kilo") == "2")
before_lint_failure = git(d10, "rev-parse", "HEAD")
(d10 / "invalid.py").write_text("broken =\n")
git(d10, "add", "invalid.py")
lint_failure = subprocess.run(["git", "commit", "-m", "invalid candidate"], cwd=str(d10),
                             capture_output=True, text=True, env=scrubbed_env())
check("the self-contained hook actually rejects invalid Python",
      lint_failure.returncode != 0 and "invalid.py" in lint_failure.stdout + lint_failure.stderr)
check("a lint failure leaves the candidate commit unchanged",
      git(d10, "rev-parse", "HEAD") == before_lint_failure)

# Exercise the real default command through the lander; fake only external
# tooling and the fixture's suite, so short-circuiting and exit propagation matter.
for lint_status, test_status in ((0, 0), (23, 0), (0, 24)):
    with tempfile.TemporaryDirectory() as temporary:
        tools_dir = Path(temporary)
        for tool in ("uv", "uvx"):
            executable = tools_dir / tool
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "' + tool + ' $*" >> "$GATE_LOG"\n'
                                  + ('exit "$LINT_STATUS"\n' if tool == "uvx" else 'exit 0\n'))
            executable.chmod(0o755)
        fixture = repo()
        (fixture / "scripts").mkdir()
        suite = fixture / "scripts/run.sh"
        suite.write_text('#!/bin/sh\nprintf "%s\\n" "suite $*" >> "$GATE_LOG"\nexit "$TEST_STATUS"\n')
        suite.chmod(0o755)
        git(fixture, "add", "scripts/run.sh")
        git(fixture, "commit", "-qm", "declare repository suite")
        candidate = agent(fixture, f"default-gate-{lint_status}-{test_status}")
        previous = git(fixture, "rev-parse", "main")
        log = tools_dir / "gate.log"
        result = land(candidate, gate=None, PATH=f"{tools_dir}:{os.environ['PATH']}",
                      GATE_LOG=str(log), LINT_STATUS=str(lint_status), TEST_STATUS=str(test_status))
        expected = ["uv sync --frozen --quiet", "uvx ruff check ."]
        if not lint_status:
            expected.append("suite --tests")
        check(f"default land runs sync, lint and suite in order ({lint_status}, {test_status})",
              log.exists() and log.read_text().splitlines() == expected, result.stdout + result.stderr)
        check(f"default land propagates every gate failure ({lint_status}, {test_status})",
              (result.returncode == 0) == (lint_status == test_status == 0))
        if lint_status or test_status:
            check("a failed default gate leaves main unchanged", git(fixture, "rev-parse", "main") == previous)

# run.sh keeps only the tail of a failing file's output, and this file prints a
# line per check - so failures scroll out of view and a red run says nothing about
# what broke. Repeat the names last, where the tail will always catch them.
if FAIL:
    print("\n  failed checks:")
    for name in FAIL:
        print("    - " + name)
# --- copy_untracked: listed inputs arrive, tracked files are never overwritten --
# harness.toml land.copy_untracked names what the gate needs that git does not
# carry. Listed inputs must arrive as independent copies so gate writes cannot
# reach the main checkout, and a tracked file's candidate bytes must survive.
COPY_CONFIG = (
    "[land]\n"
    'copy_untracked = ["fn/sources", "fn/generated.json", "fn/generated\u2013v1.1.json",'
    ' "fn/tracked.json"]\n'
)
d14 = repo()
(d14 / "fn" / "sources").mkdir(parents=True)
(d14 / "fn" / "sources" / "tracked.txt").write_text("committed\n")
(d14 / "fn" / "sources" / "untouched.txt").write_text("committed\n")
(d14 / "fn" / "sources" / "candidate.txt").write_text("committed\n")
(d14 / "fn" / "sources" / "deep").mkdir()
(d14 / "fn" / "tracked.json").write_text("committed\n")
(d14 / "harness.toml").write_text(COPY_CONFIG)
(d14 / ".gitignore").write_text("fn/generated.json\nfn/generated\u2013v1.1.json\nfn/unlisted.json\n.omc/\n")
git(d14, "add", "-A"); git(d14, "commit", "-qm", "tracked sources")
(d14 / "fn" / "sources" / "untracked.txt").write_text("provision me\n")
# Same name as a tracked file one level up: an unanchored exclude would skip it.
(d14 / "fn" / "sources" / "deep" / "tracked.txt").write_text("deep untracked\n")
(d14 / "fn" / "generated.json").write_text("generated input\n")
(d14 / "fn" / "unlisted.json").write_text("not listed\n")
(d14 / ".omc").mkdir(); (d14 / ".omc" / "state.json").write_text("live session state\n")
# An en dash in an ignored input: git quotes such a path unless the lister
# asks otherwise, and the quoted literal names no file.
(d14 / "fn" / "generated\u2013v1.1.json").write_text("non-ascii input\n")
# the main checkout is dirty in tracked files - uncommitted, must never leak
(d14 / "fn" / "sources" / "tracked.txt").write_text("UNCOMMITTED main edit\n")
(d14 / "fn" / "tracked.json").write_text("UNCOMMITTED main edit\n")

wt14 = d14 / "wt-mike"
git(d14, "worktree", "add", "-q", "-b", "mike", str(wt14), "main")
(wt14 / "work.txt").write_text("x\n")
(wt14 / "fn" / "sources" / "candidate.txt").write_text("candidate\n")
git(wt14, "add", "-A"); git(wt14, "commit", "-qm", "candidate work")

probe = Path(LANDER_ROOT) / "inode-probe"
r14 = land(wt14, gate=(
    f"grep -q '^committed$' fn/sources/tracked.txt && "
    f"grep -q '^committed$' fn/tracked.json && "
    f"grep -q '^candidate$' fn/sources/candidate.txt && "
    f"{shlex.quote(sys.executable)} -c 'from pathlib import Path; "
    f"print(Path(\"fn/sources/untouched.txt\").stat().st_ino)' > {shlex.quote(str(probe))} && "
    "printf 'gate source write\\n' > fn/sources/untracked.txt && "
    "printf 'gate root write\\n' > fn/generated.json"))
check("the gate never sees the main checkout's uncommitted tracked edits",
      r14.returncode == 0, r14.stdout + r14.stderr)
lwt14 = Path(LANDER_ROOT) / "mike"
for relative in ("fn/sources/untracked.txt", "fn/sources/deep/tracked.txt",
                 "fn/generated.json", "fn/generated\u2013v1.1.json"):
    source = d14 / relative
    copied = lwt14 / relative
    check(f"listed input {relative} arrives with an independent inode",
          copied.exists() and not source.samefile(copied))
check("an unlisted ignored file is NOT copied", not (lwt14 / "fn" / "unlisted.json").exists())
check("another session's live state is NOT copied", not (lwt14 / ".omc").exists())
check("successful provisioning retains an input manifest",
      (d14 / ".git/harness/land-inputs-mike.json").is_file())
check("a listed path the candidate tracks is reported and not copied",
      "tracked input excluded: fn/tracked.json" in r14.stderr, r14.stdout)
check("gate writes leave the primary untracked input unchanged",
      (d14 / "fn/sources/untracked.txt").read_text() == "provision me\n")
check("gate writes leave the primary ignored input unchanged",
      (d14 / "fn/generated.json").read_text() == "generated input\n")
check("the candidate's tracked bytes survive copying",
      (lwt14 / "fn/sources/candidate.txt").read_text() == "candidate\n")
main_inode = (d14 / "fn" / "sources" / "untouched.txt").stat().st_ino
check("a tracked file is not inode-shared with the main checkout during the gate",
      probe.exists() and probe.read_text().strip() != str(main_inode),
      f"gate saw inode {probe.read_text().strip() if probe.exists() else '?'}, main is {main_inode}")

# Nothing listed means nothing copied, and the land still succeeds.
d14b = repo()
(d14b / ".gitignore").write_text("generated.json\n")
git(d14b, "add", "-A"); git(d14b, "commit", "-qm", "ignore")
(d14b / "generated.json").write_text("{}\n")
r14b = land(agent(d14b, "mike-b"))
check("with no harness.toml nothing is copied", r14b.returncode == 0
      and not (Path(LANDER_ROOT) / "mike-b" / "generated.json").exists(), r14b.stdout + r14b.stderr)

# A failed transfer must stop before the gate or main can move. Shadow only
# the transfer command; the real lander and its Git operations still execute.
for command in ("missing", "symlink"):
    with tempfile.TemporaryDirectory() as td:
        fixture = repo()
        (fixture / "fn/sources").mkdir(parents=True)
        (fixture / "harness.toml").write_text(
            '[land]\ncopy_untracked = ["fn/sources", "fn/generated.json"]\n')
        (fixture / ".gitignore").write_text("fn/generated.json\nfn/sources/\n")
        git(fixture, "add", "-A")
        git(fixture, "commit", "-qm", "declare inputs")
        (fixture / "fn/sources/raw.txt").write_text("source\n")
        (fixture / "fn/generated.json").write_text("generated\n")
        candidate = agent(fixture, f"failed-{command}")
        original_main = git(fixture, "rev-parse", "main")
        (fixture / "fn/generated.json").unlink()
        if command == "symlink":
            (fixture / "fn/generated.json").symlink_to(fixture / "fn/sources/raw.txt")
        gate_marker = Path(td) / "gate-ran"
        result = land(candidate, gate=f"touch {shlex.quote(str(gate_marker))}",
                      PATH=f"{td}:{os.environ['PATH']}")
        check(f"a failed {command} transfer stops the land",
              result.returncode != 0, result.stdout + result.stderr)
        check(f"a failed {command} transfer never runs the gate", not gate_marker.exists())
        check(f"a failed {command} transfer leaves main unchanged",
              git(fixture, "rev-parse", "main") == original_main)

# --- an inherited GIT_DIR must not escape into the real repository ----------
# The suite runs inside the pre-commit hook, which exports GIT_DIR and
# GIT_INDEX_FILE. Unscrubbed, these helpers commit into the repository being
# committed to rather than their own throwaway one.
victim = repo()
scratch = repo()
victim_before = git(victim, "rev-parse", "HEAD")
os.environ["GIT_DIR"] = str(victim / ".git")
os.environ["GIT_INDEX_FILE"] = str(victim / ".git" / "index")
try:
    (scratch / "scoped.txt").write_text("stays here\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "scoped")
    check("the git helper ignores an inherited GIT_DIR",
          git(victim, "rev-parse", "HEAD") == victim_before,
          f"victim moved {victim_before[:8]} -> {git(victim, 'rev-parse', 'HEAD')[:8]}")
    check("the commit landed in its own throwaway repo",
          git(scratch, "log", "-1", "--format=%s") == "scoped")
    # land.sh must scrub for itself too: a caller can pass GIT_DIR straight in.
    wt = agent(scratch, "november")
    r = land(wt, GIT_DIR=str(victim / ".git"), GIT_INDEX_FILE=str(victim / ".git" / "index"))
    check("land.sh ignores an inherited GIT_DIR", r.returncode == 0, r.stdout + r.stderr)
    check("a land with GIT_DIR set did not touch the other repository",
          git(victim, "rev-parse", "HEAD") == victim_before,
          f"victim now {git(victim, 'rev-parse', 'HEAD')[:8]}")
finally:
    os.environ.pop("GIT_DIR", None)
    os.environ.pop("GIT_INDEX_FILE", None)

# --- landing must not leave the main checkout stale ------------------------
# `main` is checked out in the main working tree. update-ref moves the ref out
# from under it, so its index and work tree still describe the old commit and
# every landed change reads as an uncommitted reversal. git refuses this for
# `git branch -f`; update-ref bypasses that safety.
d15 = repo()
wt15 = agent(d15, "oscar")
cand15 = git(wt15, "rev-parse", "HEAD")
# unrelated uncommitted work in the main checkout, which must survive
(d15 / "human-notes.md").write_text("work in progress\n")
git(d15, "add", "human-notes.md")
(d15 / "base.txt").write_text("locally edited, uncommitted\n")
r15 = land(wt15)
check("the land succeeds with main checked out", r15.returncode == 0, r15.stdout + r15.stderr)
check("the main checkout is not left reporting phantom changes",
      git(d15, "status", "--porcelain", "--", "oscar.txt") == "",
      git(d15, "status", "--porcelain"))
check("the landed file is present in the main checkout's work tree",
      (d15 / "oscar.txt").exists())
check("the human's staged file survived the land",
      "human-notes.md" in git(d15, "diff", "--cached", "--name-only"))
check("the human's uncommitted edit survived the land",
      (d15 / "base.txt").read_text() == "locally edited, uncommitted\n")

# --- abandoned lander worktrees are reclaimed ------------------------------
# They are per-branch and persistent, so one accumulates per branch ever landed
# and nothing reclaimed them. A worktree whose branch is gone can never be
# reused; a worktree whose branch still exists may be landed again tomorrow.
d16 = repo()
wt16 = agent(d16, "papa")
land(wt16)
papa_wt = Path(LANDER_ROOT) / "papa"
check("landing creates a lander worktree for the branch", papa_wt.is_dir())

# the branch goes away, as an abandoned one does
git(d16, "worktree", "remove", "--force", str(wt16))
git(d16, "branch", "-D", "papa")

wt16b = agent(d16, "quebec")
r16 = land(wt16b)
check("a later land still succeeds", r16.returncode == 0, r16.stdout + r16.stderr)
check("the abandoned branch's lander worktree is reclaimed", not papa_wt.exists(),
      f"{papa_wt} still present")
check("the live branch's lander worktree is kept", (Path(LANDER_ROOT) / "quebec").is_dir())
check("pruning is announced, not silent", "pruned" in r16.stdout, r16.stdout)

# --- a poisoned core.worktree must stop the land before it writes ----------
# `GIT_DIR=... GIT_WORK_TREE=... git init` - the pair a pre-commit hook exports -
# writes core.worktree into the SHARED config, aiming every worktree in the
# repository at one directory. Unlike core.bare it fails silently: commands
# succeed against the wrong tree and nothing reports it. The lander merges and
# moves refs, so under it a land would write into someone else's checkout.
d17 = repo()
wt17 = agent(d17, "romeo")
main_before = git(d17, "rev-parse", "main")
git(d17, "config", "core.worktree", "/tmp/some-other-directory")
r17 = land(wt17)
check("a poisoned core.worktree is refused", r17.returncode != 0, r17.stdout + r17.stderr)
check("the refusal names core.worktree",
      "core.worktree" in (r17.stdout + r17.stderr), r17.stdout + r17.stderr)
check("nothing landed while the config was poisoned",
      git(d17, "rev-parse", "main") == main_before)
git(d17, "config", "--unset", "core.worktree")
r17b = land(wt17)
check("landing works again once core.worktree is cleared",
      r17b.returncode == 0, r17b.stdout + r17b.stderr)

# --- pruning must not delete a worktree someone is working in --------------
# The branch being gone makes a worktree eligible, but something can still be
# live inside it - a control run, a debug session. Deleting it then does not
# raise: the run continues against a vanishing tree and reports a plausible
# wrong answer, which reads as a defect in the candidate rather than a lost
# workspace. Removals are named so a later implausible result has a breadcrumb.
d18 = repo()
wt18 = agent(d18, "sierra")
land(wt18)
sierra = Path(LANDER_ROOT) / "sierra"
check("the abandoned-branch worktree exists to begin with", sierra.is_dir())
git(d18, "worktree", "remove", "--force", str(wt18))
git(d18, "branch", "-D", "sierra")

busy = subprocess.Popen(["sleep", "45"], cwd=str(sierra))
try:
    wt18b = agent(d18, "tango")
    r18 = land(wt18b)
    check("a land still succeeds while another worktree is busy",
          r18.returncode == 0, r18.stdout + r18.stderr)
    check("a lander worktree with a live process inside is not pruned",
          sierra.is_dir(), "sierra was deleted out from under a running process")
finally:
    busy.terminate()
    busy.wait()

wt18c = agent(d18, "uniform")
r18c = land(wt18c)
check("once idle, the abandoned worktree is pruned", not sierra.exists())
check("the prune names what it removed", "sierra" in r18c.stdout, r18c.stdout)

# --- local main must not fall behind origin/main ---------------------------
# Two trunks exist: the lander gates local refs/heads/main, while origin/main is
# what everyone clones and what the merge path writes. Nothing reconciled them,
# so they drifted apart - 57 commits at the worst observed. Landing onto a base
# that is behind origin produces commits that can never converge. Being AHEAD of
# origin is fine; the push path carries those. Only "behind" is the pathology.
def with_origin():
    """A repo whose origin is a real bare remote we can advance independently."""
    bare = Path(tempfile.mkdtemp(prefix="harness-origin.")) / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   capture_output=True, env=scrubbed_env())
    d = repo()
    git(d, "remote", "add", "origin", str(bare))
    git(d, "push", "-q", "origin", "main")
    git(d, "fetch", "-q", "origin")
    return d, bare

d19, bare19 = with_origin()
# origin advances without us: a clone lands something there
clone = Path(tempfile.mkdtemp(prefix="harness-clone."))
subprocess.run(["git", "clone", "-q", str(bare19), str(clone)], capture_output=True, env=scrubbed_env())
git(clone, "config", "user.email", "o@o"); git(clone, "config", "user.name", "o")
(clone / "from-origin.txt").write_text("landed elsewhere\n")
git(clone, "add", "-A"); git(clone, "commit", "-qm", "origin moved on")
git(clone, "push", "-q", "origin", "main")

wt19 = agent(d19, "victor")
r19 = land(wt19)
check("a land succeeds when local main is behind origin", r19.returncode == 0,
      r19.stdout + r19.stderr)
check("the land reports fast-forwarding to origin", "origin/main" in r19.stdout, r19.stdout)
check("origin's commit is now an ancestor of local main",
      subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "main"],
                     cwd=str(d19), env=scrubbed_env()).returncode == 0)
check("the candidate landed on top of origin's work",
      (d19 / "from-origin.txt").is_file(),
      "origin's file is absent from the main checkout after the land")

# true divergence must be refused, not guessed at
d20, bare20 = with_origin()
clone2 = Path(tempfile.mkdtemp(prefix="harness-clone2."))
subprocess.run(["git", "clone", "-q", str(bare20), str(clone2)], capture_output=True, env=scrubbed_env())
git(clone2, "config", "user.email", "o@o"); git(clone2, "config", "user.name", "o")
(clone2 / "theirs.txt").write_text("theirs\n")
git(clone2, "add", "-A"); git(clone2, "commit", "-qm", "origin side"); git(clone2, "push", "-q", "origin", "main")
(d20 / "ours.txt").write_text("ours\n")            # local main gets its own commit too
git(d20, "add", "-A"); git(d20, "commit", "-qm", "local side")
wt20 = agent(d20, "whiskey")
main_before20 = git(d20, "rev-parse", "main")
r20 = land(wt20)
check("a diverged main is merged, not refused", r20.returncode == 0, r20.stdout + r20.stderr)
check("the merge is announced", "origin/main" in r20.stdout, r20.stdout)
# The content checks are the point. A merge that records origin as a parent while
# keeping only our tree "succeeds" and silently reverts the other side - which is
# exactly what a hand-rolled commit-tree did to main on 2026-09-02.
check("origin is an ancestor of main after the merge",
      subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "main"],
                     cwd=str(d20), env=scrubbed_env()).returncode == 0)
check("our own prior main is still an ancestor",
      subprocess.run(["git", "merge-base", "--is-ancestor", main_before20, "main"],
                     cwd=str(d20), env=scrubbed_env()).returncode == 0)
check("origin's FILE survived the merge, not just its commit",
      git(d20, "show", "main:theirs.txt").strip() == "theirs",
      "origin's content was reverted while claiming it as a parent")
check("our FILE survived the merge", git(d20, "show", "main:ours.txt").strip() == "ours")
check("the candidate landed too",
      ancestor(d20, git(wt20, "rev-parse", "HEAD"), "main"))

# The same divergence with main checked out NOWHERE. This is a different code
# path - no worktree to merge in, so the lander builds one - and it is the path
# that silently reverted origin's work on 2026-09-02 by keeping the local tree.
d24, bare24 = with_origin()
clone4 = Path(tempfile.mkdtemp(prefix="harness-clone4."))
subprocess.run(["git", "clone", "-q", str(bare24), str(clone4)], capture_output=True, env=scrubbed_env())
git(clone4, "config", "user.email", "o@o"); git(clone4, "config", "user.name", "o")
(clone4 / "origin-only.txt").write_text("origin content\n")
git(clone4, "add", "-A"); git(clone4, "commit", "-qm", "origin work"); git(clone4, "push", "-q", "origin", "main")
(d24 / "local-only.txt").write_text("local content\n")
git(d24, "add", "-A"); git(d24, "commit", "-qm", "local work")
wt24 = agent(d24, "bravo2")
git(d24, "checkout", "-q", "--detach")          # main is now checked out nowhere
check("main is checked out nowhere for this case",
      "refs/heads/main" not in git(d24, "worktree", "list", "--porcelain"))
before24 = git(d24, "rev-parse", "main")
r24 = land(wt24)
check("a diverged main is merged when checked out nowhere", r24.returncode == 0,
      r24.stdout + r24.stderr)
_o24 = git(d24, "show", "main:origin-only.txt", check_rc=False).strip()
check("origin's file survived the no-worktree merge", _o24 == "origin content",
      f"got {_o24!r} - origin's content was discarded while claiming it as a parent")
check("our file survived the no-worktree merge",
      git(d24, "show", "main:local-only.txt", check_rc=False).strip() == "local content")
check("previous main is an ancestor after the no-worktree merge",
      subprocess.run(["git", "merge-base", "--is-ancestor", before24, "main"],
                     cwd=str(d24), env=scrubbed_env()).returncode == 0)

# a conflicting divergence must still fail, with main untouched
d23, bare23 = with_origin()
clone3 = Path(tempfile.mkdtemp(prefix="harness-clone3."))
subprocess.run(["git", "clone", "-q", str(bare23), str(clone3)], capture_output=True, env=scrubbed_env())
git(clone3, "config", "user.email", "o@o"); git(clone3, "config", "user.name", "o")
(clone3 / "clash.txt").write_text("from origin\n")
git(clone3, "add", "-A"); git(clone3, "commit", "-qm", "origin clash"); git(clone3, "push", "-q", "origin", "main")
(d23 / "clash.txt").write_text("from local\n")
git(d23, "add", "-A"); git(d23, "commit", "-qm", "local clash")
wt23 = agent(d23, "alfa2")
before23 = git(d23, "rev-parse", "main")
r23 = land(wt23)
check("a conflicting divergence fails the land", r23.returncode != 0, r23.stdout + r23.stderr)
check("main is untouched after a conflicting divergence",
      git(d23, "rev-parse", "main") == before23)

# --- a merge commit on main is a hole the pre-commit hook cannot see -------
# pre-commit does not run for merges; git runs pre-merge-commit instead. So
# `git merge <branch>` on main created a commit on the trunk while the rule said
# the lander was the only way to move it. A fast-forward creates no commit and
# must stay allowed, because that is exactly how the lander moves main.
d21 = repo()
# Build the divergence BEFORE arming the hook: pre-commit refuses commits on
# main, so committing the trunk side afterwards would fail and leave the merge a
# fast-forward - which is allowed, and would prove nothing.
git(d21, "checkout", "-q", "-b", "xray")
(d21 / "xray.txt").write_text("x\n")
git(d21, "add", "-A"); git(d21, "commit", "-qm", "xray work")
git(d21, "checkout", "-q", "main")
(d21 / "trunk.txt").write_text("t\n")
git(d21, "add", "-A"); git(d21, "commit", "-qm", "trunk work")
main_at = git(d21, "rev-parse", "main")
git(d21, "config", "core.hooksPath", str(HOOKS))

r21 = subprocess.run(["git", "merge", "--no-edit", "xray"], cwd=str(d21),
                     capture_output=True, text=True, env=scrubbed_env())
check("a merge commit on main is refused", r21.returncode != 0, r21.stdout + r21.stderr)
check("the merge refusal names the lander", "land.sh" in (r21.stdout + r21.stderr),
      r21.stdout + r21.stderr)
check("main did not move", git(d21, "rev-parse", "main") == main_at)

# a fast-forward creates no commit, so the lander's own swap must still work
git(d21, "merge", "--abort", check_rc=False)
git(d21, "checkout", "-q", "-b", "yankee")
(d21 / "y.txt").write_text("y\n")
git(d21, "add", "-A")
subprocess.run(["git", "commit", "-m", "yankee"], cwd=str(d21), capture_output=True,
               text=True, env=scrubbed_env())
git(d21, "checkout", "-q", "main")
git(d21, "reset", "-q", "--hard", main_at)
r21b = subprocess.run(["git", "merge", "--ff-only", "yankee"], cwd=str(d21),
                      capture_output=True, text=True, env=scrubbed_env())
check("a fast-forward onto main is still allowed", r21b.returncode == 0,
      r21b.stdout + r21b.stderr)

# and a merge on someone's own branch is none of the hook's business
git(d21, "checkout", "-q", "xray")
r21c = subprocess.run(["git", "merge", "--no-edit", "main"], cwd=str(d21),
                      capture_output=True, text=True, env=scrubbed_env())
check("a merge commit on an agent branch is allowed", r21c.returncode == 0,
      r21c.stdout + r21c.stderr)

# LAND_BASE: land.sh exports the main this attempt merges onto, so a gate step that
# diffs against it resolves its base even in a repo with no origin, and a failing
# gate step leaves main unchanged.
d23 = repo()
wt23 = agent(d23, "yankee")
main23 = git(d23, "rev-parse", "main")
r23 = land(wt23, gate='git rev-parse --verify -q "$LAND_BASE" >/dev/null && echo "base=$LAND_BASE"; exit 1')
check("a land whose gate fails is refused", r23.returncode != 0, r23.stdout + r23.stderr)
check("the gate saw LAND_BASE as the main it merges onto",
      f"base={main23}" in r23.stdout + r23.stderr, r23.stdout + r23.stderr)
check("a failed gate leaves main unchanged", git(d23, "rev-parse", "main") == main23)

# Every land outcome is recorded in the decision log (scripts/harness_events.py).
_events = [json.loads(line) for line in Path(os.environ["HARNESS_EVENTS_LOG"]).read_text().splitlines()
           if line.strip()] if Path(os.environ["HARNESS_EVENTS_LOG"]).exists() else []
_land = [e for e in _events if e.get("hook") == "land"]
check("a successful land is recorded as landed",
      any(e["decision"] == "landed" and " -> " in e["subject"] for e in _land), str(_land[-3:]))
check("a refused land is recorded with its reason",
      any(e["decision"] == "refused" and "gate failed" in e["reason"] for e in _land), str(_land[-3:]))
check("an already-landed candidate is recorded as a no-op", any(e["decision"] == "noop" for e in _land))

# --- --expect: a reviewed candidate lands exactly as reviewed ----------------------
d = repo()
wt = agent(d, "reviewed")
reviewed_sha = git(wt, "rev-parse", "HEAD")
(wt / "after-review.txt").write_text("committed after the review\n")
git(wt, "add", "-A")
git(wt, "commit", "-qm", "after review")
main_before = git(d, "rev-parse", "main")
r = land(wt, LAND_EXPECT=reviewed_sha)
check("a candidate that moved after review is refused", r.returncode != 0
      and "the candidate moved since review" in r.stderr, r.stdout + r.stderr)
check("and main does not move", git(d, "rev-parse", "main") == main_before)
r = subprocess.run(["bash", str(LAND), "--expect", git(wt, "rev-parse", "HEAD")], cwd=str(wt),
                   capture_output=True, text=True,
                   env={**scrubbed_env(), "LAND_GATE": "true", "LAND_WORKTREE_ROOT": LANDER_ROOT,
                        "LAND_LOCK_WAIT_SECS": "10", "LAND_ALLOW_UNTRACKED": "0", "LAND_EXPECT": "",
                        "LAND_RECEIPT": str(approval(wt, git(wt, "rev-parse", "HEAD")))})
check("--expect naming the tip lands", r.returncode == 0 and "LANDED" in r.stdout, r.stdout + r.stderr)
r = subprocess.run(["bash", str(LAND), "--bogus"], cwd=str(wt), capture_output=True, text=True, env=scrubbed_env())
check("an unknown argument is refused", r.returncode != 0 and "unknown argument" in r.stderr, r.stderr)

# --- after a land, the ADRs the land touched are listed ------------------------
# main is only a ref here, checked out nowhere, the usual shape in a shared
# checkout: the list must not depend on a worktree that has main checked out.
import shutil  # noqa: E402

d = repo()
SRC = LAND.parents[1]
(d / "scripts").mkdir()
for name in ("adr.py", "harness_config.py", "paths.py"):
    shutil.copy2(SRC / "scripts" / name, d / "scripts" / name)
(d / "harness.toml").write_text('[adr]\nrequire_for = []\n')
(d / "docs" / "adr").mkdir(parents=True)
(d / "docs" / "adr" / "0001-touch.md").write_text(
    "# 0001. Touch\n\nStatus: accepted\nDate: 2026-10-22\nComponents: touched.txt\n\n"
    "## Context\n\nc\n\n## Decision\n\nd\n\n## Consequences\n\nq\n")
git(d, "add", "-A")
git(d, "commit", "-qm", "adr fixture")
git(d, "switch", "-q", "-c", "elsewhere")
wt = agent(d, "adr-touch", filename="touched.txt")
r = land(wt)
check("a land whose candidate changes an ADR's component exits 0", r.returncode == 0, r.stdout + r.stderr)
check("after LANDED, the land lists the ADR whose component it changed, with main checked out nowhere",
      "ADR 0001 [accepted] Touch: touched.txt" in r.stdout, r.stdout)

# Required launch evidence is checked before integration work starts.
d_required = repo()
w_required = agent(d_required, "required-proof")
c_required = git(w_required, "rev-parse", "HEAD")
r_required = approval(w_required, c_required)
main_required = git(d_required, "rev-parse", "main")
for extra, label in (([], "missing candidate"), (["--expect", c_required], "missing receipt")):
    result = subprocess.run(["bash", str(LAND), *extra], cwd=w_required, capture_output=True, text=True,
                            env=scrubbed_env(LAND_EXPECT="", LAND_RECEIPT="", LAND_GATE="true"))
    check(f"land refuses {label} evidence", result.returncode != 0, result.stdout + result.stderr)
    check(f"{label} leaves main unchanged", git(d_required, "rev-parse", "main") == main_required)
for divergent in (False, True):
    origin_root, origin_bare = with_origin()
    peer = Path(tempfile.mkdtemp(prefix="origin-failure-peer."))
    subprocess.run(["git", "clone", "-q", str(origin_bare), str(peer)], check=True, env=scrubbed_env())
    git(peer, "config", "user.name", "peer"); git(peer, "config", "user.email", "p@p")
    (peer / "origin-new.txt").write_text("origin\n")
    git(peer, "add", "origin-new.txt"); git(peer, "commit", "-qm", "origin move")
    git(peer, "push", "-q", "origin", "main")
    if divergent:
        (origin_root / "local-new.txt").write_text("local\n")
        git(origin_root, "add", "local-new.txt"); git(origin_root, "commit", "-qm", "local move")
    candidate = agent(origin_root, f"failed-reconcile-{divergent}")
    before_ref = git(origin_root, "rev-parse", "main")
    before_index = git(origin_root, "ls-files", "--stage")
    result = land(candidate, gate="false")
    check(f"failed {'divergent' if divergent else 'behind'} origin gate does not move main",
          result.returncode != 0 and git(origin_root, "rev-parse", "main") == before_ref, result.stdout + result.stderr)
    check("failed reconciliation preserves authoritative index", git(origin_root, "ls-files", "--stage") == before_index)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
