"""The harness decision log records allows and denies, chains, and never decides.

Expectations are literals (CLAUDE.md Rule 11). Every write goes to a scratch
HARNESS_EVENTS_LOG; nothing here touches the real record in .git/harness/.
"""
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import harness_events  # noqa: E402

HOOKS = ROOT / ".claude" / "hooks"
SCRATCH = Path(tempfile.mkdtemp(prefix="harness-events-test."))
os.environ["HARNESS_EVENTS_LOG"] = str(SCRATCH / "unused.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


def fresh(name):
    path = SCRATCH / f"{name}.jsonl"
    os.environ["HARNESS_EVENTS_LOG"] = str(path)
    return path


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- chain ---------------------------------------------------------------------
log = fresh("chain")
for i in range(3):
    harness_events.record("model_router", "allow", "tier-match", f"Explore/haiku#{i}",
                          {"session_id": "s1", "agent_id": "a1", "agent_type": "reviewer-code"})
got = rows(log)
check("three records are written", len(got) == 3)
check("the first record chains from genesis", got[0]["prev"] == "0" * 64)
check("each record chains from the line before it",
      got[1]["prev"] == harness_events.line_hash(log.read_text().splitlines()[0]))
check("identity fields come from the hook payload",
      (got[0]["session"], got[0]["agent_id"], got[0]["agent_type"]) == ("s1", "a1", "reviewer-code"))
check("an intact chain verifies", harness_events.verify(log)[1] is None)

lines = log.read_text().splitlines()
log.write_text("\n".join([lines[0], lines[1].replace("#1", "#9"), lines[2]]) + "\n")
check("an edited record breaks the chain at the line after it", harness_events.verify(log)[1] == 3)
log.write_text("\n".join([lines[0], lines[2]]) + "\n")
check("a deleted record breaks the chain", harness_events.verify(log)[1] == 2)

# --- what is recorded is bounded ----------------------------------------------
log = fresh("bounded")
harness_events.record("h", "deny", "r" * 500, "s" * 500)
row = rows(log)[0]
check("reason and subject are truncated", len(row["reason"]) == 80 and len(row["subject"]) == 160)
check("a record carries exactly the declared fields",
      set(row) == {"v", "ts", "hook", "decision", "reason", "subject", "session",
                   "agent_id", "agent_type", "prev"}, str(sorted(row)))


# --- concurrent writers keep one chain -----------------------------------------
def writer(args):
    path, n = args
    os.environ["HARNESS_EVENTS_LOG"] = path
    for i in range(n):
        harness_events.record("concurrent", "allow", "", f"w{i}")


log = fresh("concurrent")
with multiprocessing.get_context("fork").Pool(8) as pool:
    pool.map(writer, [(str(log), 25)] * 8)
check("200 concurrent appends are all present", len(rows(log)) == 200, str(len(rows(log))))
check("concurrent appends still form one intact chain", harness_events.verify(log)[1] is None,
      str(harness_events.verify(log)))

# --- record() never raises, never prints to stdout -------------------------------
blocker = SCRATCH / "a-file"
blocker.write_text("x")
os.environ["HARNESS_EVENTS_LOG"] = str(blocker / "sub" / "events.jsonl")
probe = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, 'scripts'); import harness_events as h; "
                        "print('result', h.record('h', 'allow'))"],
                       cwd=ROOT, capture_output=True, text=True, env=dict(os.environ))
check("an unwritable log returns False without raising", probe.stdout.strip() == "result False",
      probe.stdout + probe.stderr)
check("an unwritable log is reported on stderr", "not recorded" in probe.stderr, probe.stderr)


# --- hooks record allow and deny, and logging cannot change a decision ------------
def hook(script, payload, log_path):
    env = {**os.environ, "HARNESS_EVENTS_LOG": str(log_path)}
    done = subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=ROOT)
    return done.returncode, done.stdout


def agent(kind, model):
    tool_input = {"description": "x", "prompt": "y", "subagent_type": kind}
    if model:
        tool_input["model"] = model
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input,
            "session_id": "s2", "agent_type": ""}


log = fresh("hooks")
hook("model_router.py", agent("Explore", "opus"), log)
hook("model_router.py", agent("Explore", "haiku"), log)
hook("model_router.py", agent("general-purpose", "opus"), log)
hook("require_agent_model.py", agent("Explore", None), log)
hook("require_agent_model.py", agent("Explore", "haiku"), log)
seen = [(r["hook"], r["decision"], r["reason"], r["subject"]) for r in rows(log)]
check("hooks record every Agent decision, allow and deny", seen == [
    ("model_router", "deny", "tier-mismatch", "Explore/opus"),
    ("model_router", "allow", "tier-match", "Explore/haiku"),
    ("model_router", "allow", "unlisted", "general-purpose/opus"),
    ("require_agent_model", "deny", "model-missing", "Explore/-"),
    ("require_agent_model", "allow", "model-stated", "Explore/haiku"),
], str(seen))
check("a non-Agent tool call is not recorded",
      hook("model_router.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                               "tool_input": {"command": "ls"}}, fresh("other"))[0] == 0
      and rows(SCRATCH / "other.jsonl") == [])

unwritable = blocker / "nope" / "events.jsonl"
for script, payload in (("model_router.py", agent("Explore", "opus")),
                        ("require_agent_model.py", agent("Explore", None))):
    rc, out = hook(script, payload, unwritable)
    check(f"{script} still denies when the log cannot be written",
          rc == 0 and json.loads(out or "{}").get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
          f"rc={rc} {out}")

# --- verify_commit_landed records each commit it checks -------------------------
with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", "-b", "work"], cwd=repo, env=genv, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, env=genv, check=True)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, env=genv,
                          capture_output=True, text=True).stdout.strip()
    log = fresh("commits")
    for response in ("nothing to commit", f"[work {head}] base"):
        subprocess.run([sys.executable, str(HOOKS / "verify_commit_landed.py")],
                       input=json.dumps({"tool_input": {"command": "git commit -m x"},
                                         "tool_response": {"stdout": response}}),
                       capture_output=True, text=True, cwd=repo, env={**genv, "HARNESS_EVENTS_LOG": str(log)})
    subprocess.run([sys.executable, str(HOOKS / "verify_commit_landed.py")],
                   input=json.dumps({"tool_input": {"command": "ls"}, "tool_response": {}}),
                   capture_output=True, text=True, cwd=repo, env={**genv, "HARNESS_EVENTS_LOG": str(log)})
    seen = [(r["decision"], r["reason"], r["subject"]) for r in rows(log)]
    check("verify_commit_landed records a blocked and a verified commit, and nothing else",
          seen == [("block", "not-landed", head), ("pass", "landed", head)], str(seen))

# --- the record CLI used by land.sh exits 0 whatever happens -----------------------
done = subprocess.run([sys.executable, str(ROOT / "scripts" / "harness_events.py"), "record",
                       "--hook", "land", "--decision", "landed", "--subject", "b@1 -> 2"],
                      capture_output=True, text=True, env={**os.environ, "HARNESS_EVENTS_LOG": str(unwritable)})
check("record CLI exits 0 even when it cannot write", done.returncode == 0, done.stderr)
check("record CLI prints nothing to stdout", done.stdout == "", done.stdout)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
