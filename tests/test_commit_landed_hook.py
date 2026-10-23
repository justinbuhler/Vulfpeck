"""verify_commit_landed.py: the commit that reports success without landing.

Two failures on 2026-08-26 reported a commit that was never created - an
index.lock collision from a peer worktree, and an exit code masked by a later
command in the same invocation. A git post-commit hook cannot see either, since
it runs only when a commit succeeds. The hook this file protects records HEAD and
the HEAD reflog length before the call (PreToolUse) and reads them again after it
(PostToolUse). The checks below drive the real script against real temporary
repositories, so the oracle is git, not the hook's own idea of itself, and never
the text a commit printed.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "verify_commit_landed.py"

# git exports GIT_DIR and GIT_INDEX_FILE to the hooks it runs, and an inherited
# GIT_DIR overrides `-C` - so every git call below would silently address the
# repository being committed instead of the temporary one. This file passed
# standalone and failed only under the pre-commit gate until ENV existed.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

# These tests run real hooks, which record decisions (scripts/harness_events.py).
# Point the record at a scratch file so fixtures never enter the real one.
os.environ["HARNESS_EVENTS_LOG"] = str(Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail and not cond else ""))


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, env=ENV)
    return done.stdout.strip()


def fire(repo, event_name, command, payload_cwd=None, stdout="", session="s1"):
    """Run the hook once as Claude Code runs it; return (reason or '', stderr)."""
    event = {"hook_event_name": event_name, "session_id": session, "tool_name": "Bash",
             "tool_input": {"command": command}, **({"cwd": payload_cwd} if payload_cwd else {})}
    if event_name == "PostToolUse":
        event["tool_response"] = {"stdout": stdout, "stderr": "", "interrupted": False}
    done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                          capture_output=True, text=True, cwd=repo, env=ENV)
    return (json.loads(done.stdout)["reason"] if done.stdout.strip() else ""), done.stderr


def blocked(repo, command, stdout="", commit_in=None, payload_cwd=None):
    """PreToolUse, then (when commit_in is given) a real commit there, then PostToolUse.

    Returns the block reason, or '' when the hook stays silent.
    """
    fire(repo, "PreToolUse", command, payload_cwd)
    if commit_in:
        git(commit_in, "commit", "-q", "--allow-empty", "-m", "made during the call")
    return fire(repo, "PostToolUse", command, payload_cwd, stdout)[0]


# ── a real repository, so HEAD is a fact the hook cannot manufacture ──────────
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, env=ENV)
    (Path(tmp) / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", tmp, "add", "a.txt"], check=True, env=ENV)
    subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--no-verify", "-m", "first"], check=True, env=ENV)
    head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True, env=ENV).stdout.strip()

    # The commit that landed, with git's own line, with `-q` (no line), and as a wrapper
    # condenses it: the text is never read, so none of these can false-block.
    check("a landed commit passes silently",
          blocked(tmp, "git commit -m first", f"[main {head[:7]}] first\n 1 file changed", commit_in=tmp) == "")
    check("a landed `git commit -q` that printed nothing passes silently",
          blocked(tmp, "git commit -q -m first", "", commit_in=tmp) == "")
    check("a landed commit whose output a wrapper condensed passes silently",
          blocked(tmp, "rtk git commit -m first", "ok 1 file changed", commit_in=tmp) == "")
    check("a landed amend, which rewrites HEAD, passes silently",
          blocked(tmp, "git commit --amend -m two", "", commit_in=tmp) == "")
    git(tmp, "reset", "-q", "--hard", "HEAD")

    # The masked exit code. `; echo` makes the shell report 0 whatever git did,
    # and the transcript carries a success word with no commit behind it.
    masked = blocked(tmp, 'git commit -m two; echo "committed"', "committed\n")
    check("an exit code masked by a trailing echo is caught", masked != "")
    check("the masked case says no commit was created", "no commit was created" in masked)

    # The index.lock collision. git fails, the tool call still looks fine.
    lock_file = Path(git(tmp, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
    fire(tmp, "PreToolUse", "git add -A && git commit -m two")
    lock_file.write_text("")
    lock = fire(tmp, "PostToolUse", "git add -A && git commit -m two",
                stdout="fatal: Unable to create '/repo/.git/index.lock': File exists.")[0]
    lock_file.unlink()
    check("an index.lock collision is caught", lock != "")
    check("the block quotes HEAD as it really is", git(tmp, "rev-parse", "--short", "HEAD") in lock)
    check("the block says the lock is still present", "index.lock is still present" in lock)

    # Output cannot vouch for a commit: a printed `[branch sha]` line with HEAD unmoved blocks.
    check("a printed commit line with HEAD unmoved is caught",
          blocked(tmp, "git commit --amend -m two", "[main deadbee] two") != "")

    # Commands that only mention the words must not cost a false block, or the
    # control becomes noise and stops being read.
    check("a command merely quoting the words is ignored",
          blocked(tmp, 'rg "git commit" ENGINEERING_LOG.md', "12: git commit --amend") == "")
    check("a dry run is ignored",
          blocked(tmp, "git commit --dry-run", "nothing to commit") == "")
    check("a commit into another repository is not judged against this HEAD",
          blocked(tmp, "git -C /elsewhere commit -m x", "") == "")

    # The hook's own cwd is not where the command ran: a real commit made in a
    # scratch repo via `cd` or `-C` is judged against that repo's HEAD.
    with tempfile.TemporaryDirectory() as other:
        subprocess.run(["git", "init", "-q", "-b", "main", other], check=True, env=ENV)
        (Path(other) / "b.txt").write_text("two\n")
        subprocess.run(["git", "-C", other, "add", "b.txt"], check=True, env=ENV)
        git(other, "commit", "-q", "--no-verify", "-m", "other")
        check("a commit made via cd into another repository is judged there",
              blocked(tmp, f"cd {other} && git commit -m other", commit_in=other) == "")
        check("a commit made via git -C into another repository is judged there",
              blocked(tmp, f"git -C {other} commit -m other", commit_in=other) == "")
        check("a commit with -c options is judged in the payload cwd",
              blocked(tmp, "git -c user.name=t commit -m other", payload_cwd=other, commit_in=other) == "")
        check("a masked commit in another repository is still caught",
              blocked(tmp, f"cd {other} && git commit -m x; echo done", "done") != "")
        check("a commit landing in this repository does not vouch for one in another",
              blocked(tmp, f"cd {other} && git commit -m x", commit_in=tmp) != "")
    # A separator inside a quoted message must not switch the check off.
    check("a commit message containing ; is still verified",
          blocked(tmp, 'git commit -m "fix: a; b"', "") != "")
    check("a commit message containing | is still verified",
          blocked(tmp, "git commit -m 'a | b'", "") != "")
    check("git help commit is not a commit",
          blocked(tmp, "git help commit", "GIT-COMMIT(1) manual") == "")
    check("git log --grep commit is not a commit",
          blocked(tmp, "git log --grep commit", "") == "")

    # Two commit calls in a session do not share a baseline, and a baseline is used once.
    fire(tmp, "PreToolUse", "git commit -m a", session="s-a")
    git(tmp, "commit", "-q", "--allow-empty", "-m", "a")
    fire(tmp, "PreToolUse", "git commit -m b", session="s-b")
    check("a second call's baseline is not the first's: its own unmoved HEAD blocks",
          fire(tmp, "PostToolUse", "git commit -m b", session="s-b")[0] != "")
    check("the first call, whose HEAD moved, passes", fire(tmp, "PostToolUse", "git commit -m a", session="s-a")[0] == "")
    reason, err = fire(tmp, "PostToolUse", "git commit -m a", session="s-a")
    check("with no recorded baseline the commit is reported unchecked, not blocked",
          reason == "" and "NOT checked" in err, f"{reason!r} {err!r}")

    # The first commit of a repository has no HEAD before it.
    with tempfile.TemporaryDirectory() as fresh:
        subprocess.run(["git", "init", "-q", "-b", "main", fresh], check=True, env=ENV)
        check("the first commit of a repository passes", blocked(fresh, "git commit -m x", commit_in=fresh) == "")
    with tempfile.TemporaryDirectory() as fresh:
        subprocess.run(["git", "init", "-q", "-b", "main", fresh], check=True, env=ENV)
        check("a failed first commit is caught", blocked(fresh, "git commit -m x") != "")

    # An abandoned baseline (the PostToolUse never ran) is pruned the next time one is written.
    commits = Path(git(tmp, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "commits"
    old = commits / "abandoned.json"
    old.write_text("{}")
    os.utime(old, (0, 0))
    fire(tmp, "PreToolUse", "git commit -m z", session="s-z")
    check("a baseline older than a day is removed", not old.exists())

    # A control that cannot read its input must not read as "nothing wrong".
    quiet = subprocess.run([sys.executable, str(HOOK)], input="not json",
                           capture_output=True, text=True, cwd=tmp, env=ENV)
    check("unreadable input says so rather than passing silently",
          "NOT checked" in quiet.stderr and quiet.stdout.strip() == "")

    # The verifier must not be redirectable either. Passing the hostile variable
    # straight through proves the hook scrubs its own environment rather than
    # relying on this file having scrubbed first. Under an inherited GIT_DIR the
    # HEAD read returns empty, which the hook treats as nothing to verify - so the
    # control switches itself off exactly when git is most confusing.
    def hostile_run(event_name):
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"hook_event_name": event_name, "session_id": "hostile", "tool_name": "Bash",
                              "tool_input": {"command": "git commit -m x; echo done"},
                              "tool_response": {"stdout": "done"}}),
            capture_output=True, text=True, cwd=tmp,
            env={**ENV, "GIT_DIR": "/nonexistent/hostile.git"}).stdout.strip()

    hostile_run("PreToolUse")
    check("the hook reads the real HEAD under a hostile GIT_DIR", hostile_run("PostToolUse") != "")

    # The defect the pre-commit gate caught: run under a hostile GIT_DIR, which
    # is what this file sees whenever git itself invokes the suite from a hook.
    os.environ["GIT_DIR"] = "/nonexistent/hostile.git"
    try:
        hostile = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True,
                                 text=True, env=ENV)
        check("an inherited GIT_DIR cannot redirect this file's git calls",
              hostile.returncode == 0 and hostile.stdout.strip() == git(tmp, "rev-parse", "HEAD"))
    finally:
        del os.environ["GIT_DIR"]
    check("ENV carries no GIT_ variable through to a subprocess",
          not [k for k in ENV if k.startswith("GIT_")])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
