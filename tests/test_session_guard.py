"""session_guard: lint failures block a stop once; unlanded work is reported, never blocked.

Every case runs the real hook as a subprocess against a throwaway repository in a
temp directory. Expectations are literals (CLAUDE.md Rule 11).
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "session_guard.py"
SCRATCH = Path(tempfile.mkdtemp(prefix="harness-session."))
os.environ["HARNESS_EVENTS_LOG"] = str(SCRATCH / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV
    )
    if done.returncode:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout.strip()


# Lint fails exactly when a file named BAD exists, and says so.
TOML = '[commands]\nlint = "if [ -e BAD ]; then echo lint-error-in-BAD; exit 1; fi"\n'


def repo():
    d = Path(tempfile.mkdtemp(prefix="session-repo.", dir=SCRATCH))
    git(d, "init", "-q", "-b", "main")
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        git(d, "config", k, v)
    (d / "harness.toml").write_text(TOML)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    return d


def hook(event, d, session="s1", active=False, project=None, path=None):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project or d)}
    if path:
        env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        capture_output=True,
        text=True,
        env=env,
        input=json.dumps(
            {
                "hook_event_name": event,
                "session_id": session,
                "cwd": str(d),
                "stop_hook_active": active,
            }
        ),
    )
    try:
        out = json.loads(done.stdout) if done.stdout.strip() else {}
    except ValueError:
        out = {"unparsed": done.stdout}
    return done.returncode, out


def events():
    path = Path(os.environ["HARNESS_EVENTS_LOG"])
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def memo(d, session="s1"):
    return (
        Path(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "harness"
        / "sessions"
        / f"{session}.json"
    )


d = repo()
rc, out = hook("Stop", d)
check("a clean repository on main stops silently", rc == 0 and out == {}, str(out))

(d / "BAD").write_text("x\n")
rc, out = hook("Stop", d)
check(
    "failing lint blocks the stop", rc == 0 and out.get("decision") == "block", str(out)
)
check(
    "the block carries the lint output",
    "lint-error-in-BAD" in out.get("reason", ""),
    out.get("reason", ""),
)
check(
    "the block is recorded",
    events()[-1]["hook"] == "session_guard" and events()[-1]["decision"] == "block",
)
rc, out = hook("Stop", d, active=True)
check("on the continuation the session may stop", "decision" not in out, str(out))
check(
    "and is told lint still fails",
    "lint still fails" in out.get("systemMessage", ""),
    str(out),
)
check("the override is recorded", events()[-1]["decision"] == "block-overridden")
rc, out = hook("Stop", d)
check(
    "lint that still fails is rechecked on the next reply, not trusted from memory",
    out.get("decision") == "block",
    str(out),
)

(d / "BAD").unlink()
rc, out = hook("Stop", d)
check("fixed lint no longer blocks", "decision" not in out, str(out))

# --- lint only what this session or branch changed ---------------------------------
# A fake `uvx` records its arguments and fails exactly when bad.py is among them, so
# the test sees both which paths were passed and whether a pre-existing failure bit.
FAKE = SCRATCH / "fakebin"
FAKE.mkdir()
ARGV = SCRATCH / "uvx-argv.txt"
(FAKE / "uvx").write_text(
    f'#!/bin/bash\n(IFS=\\|; echo "$*") >> "{ARGV}"\n'
    'for a in "$@"; do [ "$a" = bad.py ] && { echo "ruff: bad.py failed"; exit 1; }; done\nexit 0\n'
)
(FAKE / "uvx").chmod(0o755)
RUFF_TOML = '[commands]\nlint = "uvx ruff check ."\n'


def ruff_repo():
    """A repository whose main already contains a lint-failing bad.py, on branch feature."""
    d = repo()
    (d / "harness.toml").write_text(RUFF_TOML)
    (d / "bad.py").write_text("bad = 1\n")
    (d / "ok.py").write_text("ok = 1\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base with a pre-existing lint failure")
    git(d, "checkout", "-q", "-b", "feature")
    return d


def stop(d):
    """Run Stop with the fake uvx; return (hook output, the argument lines uvx was called with)."""
    ARGV.write_text("")
    out = hook("Stop", d, path=FAKE)[1]
    return out, ARGV.read_text().splitlines()


d = ruff_repo()
(d / "ok.py").write_text("ok = 2\n")
out, seen = stop(d)
check(
    "a pre-existing failure in an untouched file does not block the stop",
    "decision" not in out,
    str(out),
)
check(
    "only the changed .py path is linted, with --force-exclude",
    seen == ["ruff|check|--force-exclude|ok.py"],
    str(seen),
)

d = ruff_repo()
(d / "bad.py").write_text("bad = 2\n")
rc, out = hook("Stop", d, path=FAKE)
check(
    "a failure in a file this session changed still blocks the stop",
    out.get("decision") == "block" and "bad.py failed" in out.get("reason", ""),
    str(out),
)

d = ruff_repo()
(d / "ok.py").write_text("ok = 2\n")
git(d, "commit", "-qam", "edit ok")
(d / "new file.py").write_text("n = 1\n")
(d / "bad.py").unlink()
out, seen = stop(d)
check(
    "a committed branch change and an untracked file are both linted, whole, and a deleted file is not",
    seen == ["ruff|check|--force-exclude|new file.py|ok.py"],
    str(seen),
)

d = ruff_repo()
(d / "notes.md").write_text("n\n")
out, seen = stop(d)
check(
    "a session that changed no .py file runs no lint and does not block",
    seen == [] and "decision" not in out,
    str(seen) + str(out),
)

d = ruff_repo()
git(d, "checkout", "-q", "main")
(d / "ok.py").write_text("ok = 3\n")
out, seen = stop(d)
check(
    "on main, only the uncommitted .py change is linted",
    seen == ["ruff|check|--force-exclude|ok.py"],
    str(seen),
)

d = ruff_repo()
(d / "harness.toml").write_text('[commands]\nlint = "uvx ruff check src tests"\n')
git(d, "commit", "-qam", "explicit paths")
(d / "ok.py").write_text("ok = 2\n")
out, seen = stop(d)
check(
    "a lint command that is not `ruff check .` runs unchanged on the whole tree",
    seen == ["ruff|check|src|tests"],
    str(seen),
)

d = repo()
calls = d.parent / (d.name + "-lint-calls")
command = (
    f"printf x >> {shlex.quote(str(calls))}; {shlex.quote(sys.executable)} "
    "-c 'import ast; ast.parse(open(\"module.py\").read())'"
)
(d / "harness.toml").write_text(f"[commands]\nlint = {json.dumps(command)}\n")
(d / "module.py").write_text("value = 1\n")
git(d, "add", "harness.toml", "module.py")
git(d, "commit", "-qm", "syntax lint")
rc, out = hook("Stop", d)
check(
    "the first clean stop runs the configured lint",
    rc == 0 and out == {} and calls.read_text() == "x",
    str(out),
)
rc, out = hook("Stop", d)
check(
    "an unchanged clean stop reuses successful lint",
    rc == 0 and out == {} and calls.read_text() == "x",
    str(out),
)
(d / "module.py").write_text("value = 2\n")
before = git(d, "status", "--porcelain=v1", "--untracked-files=all")
rc, out = hook("Stop", d)
check(
    "valid dirty code is linted without blocking",
    rc == 0 and "decision" not in out and calls.read_text() == "xx",
    str(out),
)
(d / "module.py").write_text("value = (\n")
after = git(d, "status", "--porcelain=v1", "--untracked-files=all")
check(
    "different dirty bytes retain the same Git status",
    before == after == "M module.py",
    f"{before!r} {after!r}",
)
rc, out = hook("Stop", d)
check(
    "same-status dirty edits rerun lint and block actual syntax errors",
    rc == 0
    and out.get("decision") == "block"
    and "SyntaxError" in out.get("reason", "")
    and calls.read_text() == "xxx",
    str(out),
)

d = repo()
calls = d.parent / (d.name + "-lint-calls")
command = f"printf x >> {shlex.quote(str(calls))}; echo clean-lint-error; exit 1"
(d / "harness.toml").write_text(f"[commands]\nlint = {json.dumps(command)}\n")
git(d, "commit", "-qam", "failing clean lint")
_, first = hook("Stop", d)
rc, out = hook("Stop", d)
check(
    "failed lint is retried even on an unchanged clean tree",
    git(d, "status", "--porcelain=v1") == ""
    and rc == 0
    and first.get("decision") == out.get("decision") == "block"
    and calls.read_text() == "xx",
    str(out),
)

d = repo()
git(d, "checkout", "-q", "-b", "feature")
(d / "work.txt").write_text("w\n")
rc, out = hook("Stop", d)
check(
    "an uncommitted path is reported, not blocked",
    "decision" not in out and "1 uncommitted path(s)" in out.get("systemMessage", ""),
    str(out),
)
rc, out = hook("Stop", d)
check("the same state is not reported twice", out == {}, str(out))
git(d, "add", "work.txt")
git(d, "commit", "-qm", "work")
rc, out = hook("Stop", d)
check(
    "a commit not on local main is reported with the land command",
    "1 commit(s) on feature not on local main (./scripts/land.sh)"
    in out.get("systemMessage", ""),
    str(out),
)
check("unlanded work never blocks", "decision" not in out, str(out))

rc, out = hook("SessionEnd", d)
last = events()[-1]
check(
    "SessionEnd records where the session left the repository",
    rc == 0
    and last["decision"] == "session-end"
    and last["reason"] == "dirty=0 unlanded=1 unpublished=None",
    str(last),
)
check("SessionEnd names the branch", last["subject"].startswith("feature@"), str(last))
check("SessionEnd removes the session's state", not memo(d).exists())

d = repo()
memo(d).parent.mkdir(parents=True, exist_ok=True)
memo(d).write_text("{not json")
(d / "BAD").write_text("x\n")
rc, out = hook("Stop", d)
check(
    "a corrupt session state file is replaced, and lint still runs",
    out.get("decision") == "block",
    str(out),
)

d = repo()
(d / "harness.toml").write_text("[commands\n")
rc, out = hook("Stop", d)
check(
    "an unreadable harness.toml is said out loud, not passed silently",
    rc == 0 and "harness.toml unreadable" in out.get("systemMessage", ""),
    str(out),
)

d = repo()
(d / "harness.toml").write_text('[commands]\nlint = ""\n')
git(d, "commit", "-qam", "no lint")
rc, out = hook("Stop", d)
check("an empty lint command runs nothing and stays quiet", out == {}, str(out))

d, other = repo(), repo()
(other / "BAD").write_text("x\n")
rc, out = hook("Stop", other, project=d)
check(
    "a repository other than the project is never linted, so its lint command never runs",
    out == {},
    str(out),
)
wt = d.parent / (d.name + "-wt")
git(d, "worktree", "add", "-q", "-b", "side", str(wt))
(wt / "BAD").write_text("x\n")
rc, out = hook("Stop", wt, project=d)
check("a worktree of the project is linted", out.get("decision") == "block", str(out))

outside = Path(tempfile.mkdtemp(dir=SCRATCH))
rc, out = hook("Stop", outside)
check("outside a repository the hook does nothing", rc == 0 and out == {}, str(out))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
