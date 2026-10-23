"""verify_commit_landed.py: the commit that reports success without landing.

Two failures on 2026-08-26 reported a commit that was never created - an
index.lock collision from a peer worktree, and an exit code masked by a later
command in the same invocation. A git post-commit hook cannot see either, since
it runs only when a commit succeeds. The PostToolUse hook this file protects
reads HEAD back instead. The checks below drive the real script against a real
temporary repository, so the oracle is git, not the hook's own idea of itself.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "verify_commit_landed.py"
sys.path.insert(0, str(HOOK.parent))
import verify_commit_landed as H  # noqa: E402

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


def blocked(repo, command, stdout, payload_cwd=None):
    """Run the hook as Claude Code runs it; return its reason, or '' when silent."""
    event = {"tool_name": "Bash", "tool_input": {"command": command},
             **({"cwd": payload_cwd} if payload_cwd else {}),
             "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False}}
    done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                          capture_output=True, text=True, cwd=repo, env=ENV)
    return json.loads(done.stdout)["reason"] if done.stdout.strip() else ""


# ── a real repository, so HEAD is a fact the hook cannot manufacture ──────────
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, env=ENV)
    (Path(tmp) / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", tmp, "add", "a.txt"], check=True, env=ENV)
    subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--no-verify", "-m", "first"], check=True, env=ENV)
    head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True, env=ENV).stdout.strip()

    # The commit that landed: git's line names the sha the branch is actually on.
    check("a landed commit passes silently",
          blocked(tmp, "git commit -m first", f"[main {head[:7]}] first\n 1 file changed") == "")

    # The masked exit code. `; echo` makes the shell report 0 whatever git did,
    # and the transcript carries a success word with no commit behind it.
    masked = blocked(tmp, 'git commit -m two; echo "committed"', "committed\n")
    check("an exit code masked by a trailing echo is caught", masked != "")
    check("the masked case names the missing git line", "[branch sha]" in masked)

    # The index.lock collision. git fails, the tool call still looks fine.
    lock = blocked(tmp, "git add -A && git commit -m two",
                   "fatal: Unable to create '/repo/.git/index.lock': File exists.")
    check("an index.lock collision is caught", lock != "")
    check("the block quotes HEAD as it really is", head[:7] in lock)

    # A sha that is not HEAD - the amend that rewrote a peer's commit reads this way.
    check("a reported sha that is not HEAD is caught",
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
        subprocess.run(["git", "-C", other, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--no-verify", "-m", "other"], check=True, env=ENV)
        ohead = subprocess.run(["git", "-C", other, "rev-parse", "HEAD"], capture_output=True,
                               text=True, check=True, env=ENV).stdout.strip()
        line = f"[main {ohead[:7]}] other\n 1 file changed"
        check("a commit made via cd into another repository is judged there",
              blocked(tmp, f"cd {other} && git commit -m other", line) == "")
        check("a commit made via git -C into another repository is judged there",
              blocked(tmp, f"git -C {other} commit -m other", line) == "")
        check("a commit with -c options is judged in the payload cwd",
              blocked(tmp, "git -c user.name=t commit -m other", line, payload_cwd=other) == "")
        check("a masked commit in another repository is still caught",
              blocked(tmp, f"cd {other} && git commit -m x; echo done", "done") != "")
    # A separator inside a quoted message must not switch the check off.
    check("a commit message containing ; is still verified",
          blocked(tmp, 'git commit -m "fix: a; b"', "") != "")
    check("a commit message containing | is still verified",
          blocked(tmp, "git commit -m 'a | b'", "") != "")
    check("git help commit is not a commit",
          blocked(tmp, "git help commit", "GIT-COMMIT(1) manual") == "")
    check("git log --grep commit is not a commit",
          blocked(tmp, "git log --grep commit", "") == "")

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
    check("the hook reads the real HEAD under a hostile GIT_DIR",
          subprocess.run(
              [sys.executable, str(HOOK)],
              input=json.dumps({"tool_name": "Bash",
                                "tool_input": {"command": "git commit -m x; echo done"},
                                "tool_response": {"stdout": "done"}}),
              capture_output=True, text=True, cwd=tmp,
              env={**ENV, "GIT_DIR": "/nonexistent/hostile.git"}).stdout.strip() != "")

    # The defect the pre-commit gate caught: run under a hostile GIT_DIR, which
    # is what this file sees whenever git itself invokes the suite from a hook.
    os.environ["GIT_DIR"] = "/nonexistent/hostile.git"
    try:
        hostile = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True,
                                 text=True, env=ENV)
        check("an inherited GIT_DIR cannot redirect this file's git calls",
              hostile.returncode == 0 and hostile.stdout.strip() == head)
    finally:
        del os.environ["GIT_DIR"]
    check("ENV carries no GIT_ variable through to a subprocess",
          not [k for k in ENV if k.startswith("GIT_")])

# ── the parser, at the shapes git actually prints ─────────────────────────────
check("the root commit line still yields its sha",
      H.LANDED.findall("[main (root-commit) 1a2b3c4] first") == ["1a2b3c4"])
check("two commits in one call are judged on the last",
      H.LANDED.findall("[main aaaaaaa] one\n[main bbbbbbb] two")[-1] == "bbbbbbb")
check("a response of an unexpected shape is still searched",
      "[main aaaaaaa] one" in H.text_of({"a": ["x", {"b": "[main aaaaaaa] one"}]}))
check("the git line is not invented out of prose",
      H.LANDED.findall("no commit was made [see above] for why") == [])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
