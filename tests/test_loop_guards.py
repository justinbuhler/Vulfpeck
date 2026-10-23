"""loop_guard, scope_guard, budget_guard, turn_guard and their scripts (ADR 0017).

Hooks run as real subprocesses against throwaway git repositories; the pure helpers are
imported. Expectations are literals (Rule 11); nothing here reads a count or a phrase back
from the file it checks except the AGENTS.md quote check, which compares two files.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRATCH = Path(tempfile.mkdtemp(prefix="harness-loop."))
EVENTS = SCRATCH / "events.jsonl"
os.environ["HARNESS_EVENTS_LOG"] = str(EVENTS)
os.environ["JEV_DISABLE"] = "1"
os.environ["GUARD_ROLLOUT_WAIT"] = "0.2"

import guard_lib as g  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV)
    if done.returncode:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout.strip()


def repo(files=("a.py", "b.py", "c.py", "docs.md")):
    d = Path(tempfile.mkdtemp(prefix="repo.", dir=SCRATCH))
    git(d, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(d, "config", k, v)
    for name in files:
        (d / name).write_text("x = 1\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-qb", "work")
    return d


def run_hook(name, payload, cwd, project=ROOT):
    done = subprocess.run([sys.executable, str(project / ".claude" / "hooks" / name)], capture_output=True,
                          text=True, input=json.dumps(payload), cwd=cwd,
                          env={**ENV, "CLAUDE_PROJECT_DIR": str(project), "JEV_DISABLE": "1",
                               "HARNESS_EVENTS_LOG": str(EVENTS)})
    out = {}
    if done.stdout.strip():
        try:
            out = json.loads(done.stdout)
        except ValueError:
            out = {"unparsed": done.stdout}
    return done.returncode, out, done.stderr


def reason_of(out):
    inner = out.get("hookSpecificOutput", {})
    return inner.get("permissionDecisionReason") or out.get("reason") or inner.get("additionalContext") or ""


def is_deny(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def is_block(out):
    return out.get("decision") == "block"


# --- the rule quotes are the AGENTS.md rules ------------------------------------------
agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
for label, rule in (("fingerprint", g.RULE_FINGERPRINT), ("no-progress", g.RULE_NO_PROGRESS),
                    ("unchanged", g.RULE_UNCHANGED), ("turn", g.RULE_TURN)):
    check(f"the quoted {label} rule is a line of AGENTS.md", rule in agents, rule)

# --- fingerprint normalisation --------------------------------------------------------
check("rtk prefix, repeated spaces and a trailing tail filter are dropped",
      g.normalise("rtk  pytest   -x   tests/  | tail -5") == "pytest -x tests/")
check("head then tail filters are both dropped", g.normalise("make test | head -n 3 | tail -2") == "make test")
check("a pipe into another command is kept", g.normalise("ls | xargs echo") == "ls | xargs echo")
for variant in ("python3 build.py 2>&1", "python3 build.py 2>&1 | cat", "cd . && python3 build.py", "cd ./ && python3 build.py",
                "FOO=1 python3 build.py", "FOO=2 BAR=x python3 build.py >/dev/null", "python3 build.py | tail -n 5",
                "python3 build.py 2>&1 | tail -3", "python3 build.py | grep x", "python3 build.py | tee out.txt",
                "python3 build.py > out.txt 2>/dev/null", "rtk  python3   build.py"):
    check(f"{variant!r} fingerprints like the bare command", g.fingerprint(variant, "Error: build failed")
          == g.fingerprint("python3 build.py", "Error: build failed"))
for a, b in (("2026-10-23T10:00:00 ERROR build failed", "2026-10-23T10:03:07.123Z ERROR build failed"),
             ("ERROR build failed in 12.5s", "ERROR build failed in 3ms"),
             ("error at 0x7f3a9c", "error at 0xdeadbeef"),
             ("id abc1234def failed", "id 99887766aa failed"),
             ("12:00:01 error: boom", "13:59:59 error: boom")):
    check(f"error lines differing only in volatile parts share a fingerprint: {a!r}",
          g.fingerprint("make", a) == g.fingerprint("make", b))
check("a different error word is still a different fingerprint", g.fingerprint("make", "error: a") != g.fingerprint("make", "error: b"))
check("the env prefix is dropped but the command words stay", g.normalise("FOO=1 BAR=2 make all") == "make all")
for payload_text, want in (("  FAIL  test_a\n  FAIL  test_b\n  PASS  test_c", ["test_a", "test_b"]),
                           ("FAILED tests/x.py::t1 - assert 1\nFAILED tests/x.py::t2\n2 failed, 5 passed", ["tests/x.py::t1", "tests/x.py::t2"]),
                           ("3 failed, 1 passed", ["?", "?", "?"]),
                           ("FAILED (1): ./scripts/run.sh --tests", None),
                           ("everything exploded", None)):
    got = g.failure_items({"hook_event_name": "PostToolUseFailure", "error": "Exit code 1\n" + payload_text.replace("\\n", "\n")})
    check(f"failure_items reads {payload_text[:30]!r}", got == want, str(got))
check("the same failing check fingerprints alike across spacing and filters",
      g.fingerprint("pytest -x", "E boom") == g.fingerprint("rtk pytest  -x | tail -3", "E boom"))
check("a different error line is a different fingerprint",
      g.fingerprint("pytest -x", "E boom") != g.fingerprint("pytest -x", "E crash"))
check("a different command is a different fingerprint",
      g.fingerprint("pytest -x", "E boom") != g.fingerprint("pytest -y", "E boom"))
check("the fingerprint is a sha1 hex digest", len(g.fingerprint("a", "b")) == 40)

# --- exit status and error line, per runtime ------------------------------------------
check("Claude PostToolUseFailure carries the exit status in `error`",
      g.exit_status({"hook_event_name": "PostToolUseFailure", "error": "Exit code 2\nboom"}) == 2)
check("a Claude PostToolUse {stdout, stderr} response is a success even if the text says exit code",
      g.exit_status({"hook_event_name": "PostToolUse",
                     "tool_response": {"stdout": "Exit code: 1\n", "stderr": ""}}) == 0)
check("a numeric exit_code in a response is read",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": {"exit_code": 3, "output": "x"}}) == 3)
check("Gemini's `Exit Code: N` line is read",
      g.exit_status({"hook_event_name": "PostToolUse",
                     "tool_response": {"llmContent": "Output: nope\nExit Code: 1", "stdout": "Output: nope\nExit Code: 1"}}) == 1)
codex_log = SCRATCH / "rollout.jsonl"
codex_log.write_text(json.dumps({"type": "event_msg", "payload": {"item": {"id": "exec-1", "exit_code": 0}}}) + "\n"
                     + json.dumps({"type": "event_msg", "payload": {"item": {"id": "exec-2", "exit_code": 4}}}) + "\n")
check("a Codex text-only response takes the exit code its rollout logged for the tool_use_id",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": "boom\n", "tool_use_id": "exec-2",
                     "transcript_path": str(codex_log)}) == 4)
check("a Codex call the rollout shows exiting 0 is a success",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": "boom\n", "tool_use_id": "exec-1",
                     "transcript_path": str(codex_log)}) == 0)
check("a Codex call the rollout does not hold is an unknown exit, not a success",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": "boom\n", "tool_use_id": "exec-9",
                     "transcript_path": str(codex_log)}) is None)
check("a Codex text response with no rollout at all is unknown too",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": "boom\n"}) is None)
check("a Codex-style text response with an exit line is read",
      g.exit_status({"hook_event_name": "PostToolUse", "tool_response": "boom\nExit code: 7\n"}) == 7)
check("the error line skips the Exit code header",
      g.first_error_line({"hook_event_name": "PostToolUseFailure", "error": "Exit code 1\nls: x: No such file"})
      == "ls: x: No such file")
check("the error line prefers a line that reads as an error",
      g.first_error_line({"hook_event_name": "PostToolUse", "tool_response": {"stdout": "collected 3\nFAILED t.py::a - x", "stderr": ""}})
      == "FAILED t.py::a - x")

# --- which commands are test or lint runs ----------------------------------------------
for cmd, want in (("./scripts/run.sh --tests", True), ("./scripts/gate.sh", True), ("python3 tests/test_x.py", True),
                  ("uv run --no-sync python3 tests/test_x.py", True), ("uvx ruff check .", True),
                  ("./scripts/standalone_tests.sh", True), ("python3 scripts/plan_tests.py --base x --run", True),
                  ("python3 scripts/plan_tests.py --base x", False), ("pytest", True), ("pytest -x tests/a.py", True),
                  ("python -m pytest", True), ("python3 -m pytest -q", True), ("uv run pytest", True),
                  ("cd sub && pytest -q", True), ("cat pytest.ini", False), ("ls .pytest_cache", False), ("ls tests", False), ("git status", False)):
    check(f"is_test_command({cmd!r}) is {want}", g.is_test_command(cmd) is want)

# --- loop_guard: the 3rd sighting blocks, the 2nd does not ------------------------------
def post_fail(cwd, cmd, error, session="s1", exit_code=1):
    return run_hook("loop_guard.py", {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "session_id": session,
                                      "cwd": str(cwd), "tool_input": {"command": cmd},
                                      "error": f"Exit code {exit_code}\n{error}"}, cwd)


def post_ok(cwd, cmd, session="s1"):
    return run_hook("loop_guard.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session,
                                      "cwd": str(cwd), "tool_input": {"command": cmd},
                                      "tool_response": {"stdout": "ok", "stderr": ""}}, cwd)


def pre(cwd, cmd, session="s1"):
    return run_hook("loop_guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": session,
                                      "cwd": str(cwd), "tool_input": {"command": cmd}}, cwd)


def edit(cwd, session="s1"):
    return run_hook("loop_guard.py", {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": session,
                                      "cwd": str(cwd), "tool_input": {"file_path": str(cwd / "a.py")}}, cwd)


d = repo()
rc, out, _ = post_fail(d, "make check", "E: boom")
check("1st sighting of a fingerprint does not block", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = post_fail(d, "make   check | tail -3", "E: boom")
check("2nd sighting (same command modulo spacing and tail, same error) does not block", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = post_fail(d, "rtk make check", "E: boom")
text = reason_of(out)
check("3rd sighting blocks", rc == 0 and is_block(out), str(out))
check("the block quotes the AGENTS.md rule", g.RULE_FINGERPRINT in text, text)
check("the block lists the 3 attempts as one-liners", all(f"  {i}. make" in text for i in (1, 2, 3)) and text.count("-> E: boom") == 3, text)
check("the block names the way out and the session id",
      "write what was tried" in text and "owner or the task runner resets this (session s1)" in text, text)
check("the block no longer carries a copy-paste reset command", "loop_reset.py" not in text, text)
check("without Jev the block carries no Jev line", "Jev" not in text, text)
rc, out, _ = pre(d, "make check")
check("after the trip the failing command is denied at PreToolUse", rc == 0 and is_deny(out) and g.RULE_FINGERPRINT in reason_of(out), str(out))
rc, out, _ = pre(d, "make other")
check("after the trip a different command is allowed", rc == 0 and not is_deny(out), str(out))
rc, out, _ = pre(d, "make check", session="s2")
check("another session is not tripped", rc == 0 and not is_deny(out), str(out))

# --- recovery -----------------------------------------------------------------------------
before = EVENTS.read_text() if EVENTS.is_file() else ""
done = subprocess.run([sys.executable, str(ROOT / "scripts" / "loop_reset.py"), "--session", "s1"], cwd=d,
                      capture_output=True, text=True, env={**ENV, "HARNESS_EVENTS_LOG": str(EVENTS)})
check("loop_reset refuses to run without a reason", done.returncode == 2, done.stderr)
done = subprocess.run([sys.executable, str(ROOT / "scripts" / "loop_reset.py"), "--session", "s1", "--reason", "owner changed the test"],
                      cwd=d, capture_output=True, text=True, env={**ENV, "HARNESS_EVENTS_LOG": str(EVENTS)})
check("loop_reset clears the session's counters", done.returncode == 0 and "cleared: s1.json" in done.stdout, done.stdout + done.stderr)
events = [json.loads(line) for line in EVENTS.read_text().splitlines()]
check("a block and a reset each append an event",
      any(e["hook"] == "loop_guard" and e["decision"] == "block" and e["reason"] == "fingerprint-x3" for e in events)
      and any(e["hook"] == "loop_guard" and e["decision"] == "reset" and e["reason"] == "owner changed the test"
              and e["subject"] == "s1" for e in events), str(events[-3:]))
rc, out, _ = pre(d, "make check")
check("after a reset the command is allowed again", rc == 0 and not is_deny(out), str(out))

# --- unchanged re-run ---------------------------------------------------------------------
d = repo()
post_fail(d, "pytest -x", "E: one")
rc, out, _ = pre(d, "pytest -x")
check("an unchanged re-run of the last failed command is denied", rc == 0 and is_deny(out)
      and "never re-run an unchanged failing command" in reason_of(out).lower(), str(out))
rc, out, _ = pre(d, "pytest  -x")
check("a re-run that differs by a space is not byte-identical, so it is allowed", rc == 0 and not is_deny(out), str(out))
rc, out, _ = pre(d, "rtk pytest -x")
check("the rtk prefix a user hook adds does not hide an unchanged re-run (rtk is stripped)", rc == 0 and is_deny(out), str(out))
edit(d)
rc, out, _ = pre(d, "pytest -x")
check("the same command is allowed once a file was edited", rc == 0 and not is_deny(out), str(out))
post_fail(d, "pytest -x", "E: one")
post_ok(d, "pytest -x")
rc, out, _ = pre(d, "pytest -x")
check("a command that then passed may run again", rc == 0 and not is_deny(out), str(out))

post_fail(d, "pytest -x", "E: two")
post_ok(d, "git stash")
rc, out, _ = pre(d, "pytest -x")
check("an unrelated Bash command in between (git stash) lifts the unchanged-re-run deny", rc == 0 and not is_deny(out), str(out))
post_fail(d, "pytest -x", "E: three")
rc, out, _ = pre(d, "pytest -x")
check("a fresh failure denies the re-run again", rc == 0 and is_deny(out), str(out))
post_ok(d, "uv sync")
post_ok(d, "git checkout -q work")
rc, out, _ = pre(d, "pytest -x")
check("uv sync and checkout in between also lift it", rc == 0 and not is_deny(out), str(out))

# --- a passing run clears that command's fingerprints (mutant: no clearing) ------------------
d = repo()
post_fail(d, "make check", "E: boom")
edit(d)
post_fail(d, "make check", "E: boom")
edit(d)
post_ok(d, "make check")
edit(d)
rc, out, _ = post_fail(d, "make check", "E: boom")
check("a successful run clears that command's fingerprints, so a later failure is only the 1st sighting",
      rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = post_fail(d, "make check", "E: boom")
check("and the count restarts: the 2nd sighting after the pass still does not block", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = post_fail(d, "make check", "E: boom")
check("the 3rd sighting after the pass blocks", rc == 0 and is_block(out), str(out))

# --- no progress: failing tests, not commands ----------------------------------------------
T = "python3 tests/test_a.py"


def red(cwd, n, names, cmd=T, session="s1"):
    """A failing run number n whose first error line differs per run but whose failing tests are `names`."""
    return post_fail(cwd, cmd, "error: run %d\n" % n + "\n".join(f"  FAIL  {name}" for name in names), session=session)


d = repo()
red(d, 1, ["a", "b"])
edit(d)
rc, out, _ = red(d, 2, ["a", "b"])
check("one stalled cycle does not block", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = red(d, 3, ["a", "b"])
check("a second stalled cycle blocks once with the reassess message", rc == 0 and is_block(out)
      and "blocked once" in reason_of(out) and g.RULE_NO_PROGRESS in reason_of(out), str(out))
rc, out, _ = pre(d, "python3 tests/test_other.py")
check("after the first no-progress block test commands are still allowed", rc == 0 and not is_deny(out), str(out))
edit(d)
rc, out, _ = red(d, 4, ["a", "b"])
check("the count restarted after the block: one more stalled cycle does not block", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = red(d, 5, ["a", "b"])
check("a SECOND trip in the session blocks with the second-trip message", rc == 0 and is_block(out) and "second trip" in reason_of(out), str(out))
rc, out, _ = pre(d, "python3 tests/test_other.py")
check("after the second trip every test command is denied", rc == 0 and is_deny(out) and "second trip" in reason_of(out), str(out))
rc, out, _ = pre(d, "pytest -q")
check("after the second trip pytest is denied too", rc == 0 and is_deny(out), str(out))
rc, out, _ = pre(d, "git status")
check("after the second trip other commands still run", rc == 0 and not is_deny(out), str(out))

d = repo()
red(d, 1, ["a", "b", "c"])
edit(d)
red(d, 2, ["b", "c"])
edit(d)
red(d, 3, ["b", "c"])
edit(d)
rc, out, _ = red(d, 4, ["c"])
check("fixing one failing test per cycle is progress and never blocks", rc == 0 and not is_block(out), str(out))
edit(d)
red(d, 5, ["c"])
edit(d)
rc, out, _ = red(d, 6, ["c"])
check("a stall after that progress counts from zero: two stalled cycles block", rc == 0 and is_block(out), str(out))

d = repo()
post_ok(d, T)
edit(d)
rc, out, _ = red(d, 1, ["new_test"])
check("green, edit, red: the first red is not a stall", rc == 0 and not is_block(out), str(out))
edit(d)
rc, out, _ = red(d, 2, ["new_test"])
check("TDD green, edit, red, edit, red does not trip (one stalled cycle only)", rc == 0 and not is_block(out), str(out))
edit(d)
post_ok(d, T)
edit(d)
rc, out, _ = red(d, 3, ["x"])
check("a passing run resets the stalled count", rc == 0 and not is_block(out), str(out))

d = repo()
post_fail(d, T, "E: first")
for i in range(2, 6):
    edit(d)
    rc, out, _ = post_fail(d, T, f"E: different {i}")
check("with no parseable failures a different error each cycle counts as progress", rc == 0 and not is_block(out), str(out))
d = repo()
post_fail(d, T, "E: same")
edit(d)
post_fail(d, T, "E: same")
edit(d)
rc, out, _ = post_fail(d, T, "E: same")
check("with no parseable failures the same error twice over blocks", rc == 0 and is_block(out), str(out))

# --- an unknown exit changes no counter -----------------------------------------------------
def post_codex(cwd, cmd, text, session="s1", call="exec-x"):
    return run_hook("loop_guard.py", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session,
                                      "cwd": str(cwd), "tool_input": {"command": cmd}, "tool_response": text,
                                      "tool_use_id": call}, cwd)


d = repo()
post_fail(d, "make check", "E: boom")
edit(d)
post_fail(d, "make check", "E: boom")
edit(d)
rc, out, err = post_codex(d, "make check", "E: boom\n")
check("an unknown exit adds a one-line notice and is not counted or blocked",
      rc == 0 and not is_block(out) and "exit status is unknown" in reason_of(out), f"{rc} {out} {err}")
check("an exit-unknown event is recorded", any(json.loads(line)["reason"] == "exit-unknown" for line in EVENTS.read_text().splitlines()))
edit(d)
rc, out, _ = post_fail(d, "make check", "E: boom")
check("the unknown run neither counted nor cleared: the next real failure is the 3rd sighting", rc == 0 and is_block(out), str(out))
d = repo()
post_fail(d, "make check", "E: boom")
edit(d)
post_codex(d, "make check", "E: boom\n")
edit(d)
post_codex(d, "make check", "E: boom\n")
edit(d)
rc, out, _ = post_fail(d, "make check", "E: boom")
check("unknown exits do not count as sightings: 1 real + 2 unknown + 1 real is only the 2nd", rc == 0 and not is_block(out), str(out))
d = repo()
post_fail(d, "make check", "E: boom")
post_codex(d, "make check", "fine\n")
rc, out, _ = pre(d, "make check")
check("an unknown exit does not clear last_failed", rc == 0 and is_deny(out), str(out))

# --- the agent cannot reset or edit the loop state ---------------------------------------------
d = repo()
for cmd in ("python3 scripts/loop_reset.py --session s1 --reason go", "uv run python3 scripts/loop_reset.py --session s1 --reason x",
            "rm -f /x/.git/harness/loop/s1.json", "mv .git/harness/loop/s1.json /tmp/", "echo '{}' > .git/harness/loop/s1.json",
            "sed -i s/a/b/ .git/harness/loop/s1.json"):
    rc, out, _ = pre(d, cmd)
    check(f"loop_guard denies {cmd[:50]!r}", rc == 0 and is_deny(out) and "owner or the task runner" in reason_of(out), str(out))
rc, out, _ = pre(d, "ls .git/harness/loop")
check("reading the loop directory is not a mutation", rc == 0 and not is_deny(out), str(out))
for tool in ("Write", "Edit"):
    rc, out, _ = run_hook("loop_guard.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s1", "cwd": str(d),
                                            "tool_input": {"file_path": str(d / ".git" / "harness" / "loop" / "s1.json")}}, d)
    check(f"loop_guard denies a {tool} into the loop state", rc == 0 and is_deny(out), str(out))
rc, out, _ = run_hook("loop_guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Write", "session_id": "s1", "cwd": str(d),
                                        "tool_input": {"file_path": str(d / "a.py")}}, d)
check("an ordinary Write passes loop_guard", rc == 0 and out == {}, str(out))
d = repo()
for i in range(3):
    post_fail(d, "make check", "E: boom")
    edit(d)
state_path = Path(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "loop" / "s1.json"
check("(setup) the state holds a trip", '"tripped": {' in state_path.read_text())
state_path.write_text(state_path.read_text()[:-6])
rc, out, _ = pre(d, "python3 tests/test_a.py")
check("a corrupt state file that held a trip is still tripped: test commands are denied", rc == 0 and is_deny(out)
      and "corrupt while a trip was set" in reason_of(out), str(out))
for payload in ({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "x"},
                {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "x"}, "tool_response": None}):
    rc, out, err = run_hook("loop_guard.py", {"session_id": "s7", "cwd": str(d), **payload}, d)
    check(f"a malformed payload does not crash loop_guard: {str(payload)[:60]}", rc == 0, f"{rc} {err}")

# --- subagents have their own counters --------------------------------------------------------
d = repo()
for i in range(3):
    run_hook("loop_guard.py", {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "session_id": "s1", "agent_id": "a1",
                               "cwd": str(d), "tool_input": {"command": "make check"}, "error": "Exit code 1\nE: boom"}, d)
    run_hook("loop_guard.py", {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "s1", "agent_id": "a1",
                               "cwd": str(d), "tool_input": {"file_path": "a.py"}}, d)
rc, out, _ = run_hook("loop_guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1", "agent_id": "a1",
                                        "cwd": str(d), "tool_input": {"command": "make check"}}, d)
check("a subagent's trip denies its own command", rc == 0 and is_deny(out), str(out))
rc, out, _ = pre(d, "make check")
check("the parent session (same session_id, no agent_id) is not tripped by its subagent", rc == 0 and not is_deny(out), str(out))

# --- corrupt state ------------------------------------------------------------------------
d = repo()
post_fail(d, "x", "E: y")
state = next((Path(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "loop").glob("s1.json"))
state.write_text("{not json")
rc, out, err = pre(d, "anything")
check("a corrupt state file is reset with a one-line notice, not a crash",
      rc == 0 and "was unreadable and was reset" in reason_of(out) and len(reason_of(out).splitlines()) == 1, f"{rc} {out} {err}")
rc, out, _ = post_fail(d, "x", "E: y")
check("the reset state counts from zero and is valid JSON again", rc == 0 and json.loads(state.read_text())["v"] == 1)
state.write_text('{"v": 2}')
rc, out, _ = pre(d, "anything")
check("a state file of another version is reset too", rc == 0 and "reset" in reason_of(out), str(out))

# --- Jev advice is appended, advisory --------------------------------------------------------
spec = importlib.util.spec_from_file_location("loop_guard_under_test", ROOT / ".claude" / "hooks" / "loop_guard.py")
lg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lg)
import jev  # noqa: E402

original = jev.reassess
jev.reassess = lambda ctx: {"source": "jev", "action": "escalate", "reason": "needs the owner", "attempts": ctx["attempts"]}
state = g.fresh_loop()
entry = {"n": 3, "cmd": "make", "attempts": ["make -> E: x"] * 3}
msg = lg.trip_fingerprint(state, "f" * 40, "make", entry, "s9")
check("Jev's recommendation is appended when Jev answered", "recommends escalate: needs the owner" in msg and "cannot unblock" in msg, msg)
jev.reassess = lambda ctx: {"source": "fallback", "action": "change_approach", "reason": None}
check("a fallback Jev answer adds nothing", "Jev" not in lg.trip_fingerprint(g.fresh_loop(), "f" * 40, "make", entry, "s9"))


def boom(ctx):
    raise RuntimeError("down")


jev.reassess = boom
check("a Jev failure adds nothing and does not raise", "Jev" not in lg.trip_fingerprint(g.fresh_loop(), "f" * 40, "make", entry, "s9"))
jev.reassess = original

# --- scope_guard ------------------------------------------------------------------------------
def scope(cwd, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "scope.py"), *args], cwd=cwd, capture_output=True, text=True,
                          env={**ENV, "JEV_DISABLE": "1", "HARNESS_EVENTS_LOG": str(EVENTS)})


def write_event(cwd, name, tool="Write", **extra):
    return run_hook("scope_guard.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(cwd),
                                       "tool_input": {"file_path": name, **extra}}, cwd)


d = repo()
rc, out, _ = write_event(d, "a.py", content="y = 2\n")
check("with no lease an edit is allowed silently", rc == 0 and out == {})
done = scope(d, "set", "--glob", "a.py", "--glob", "b.py", "--max-files", "2", "--max-lines", "10", "--task", "tidy a and b", "--base", "main")
check("scope.py set writes a lease", done.returncode == 0 and "lease set for work" in done.stdout, done.stdout + done.stderr)
lease = json.loads(scope(d, "show").stdout)
check("show prints the lease fields", lease["globs"] == ["a.py", "b.py"] and lease["max_files"] == 2 and lease["max_lines"] == 10
      and lease["task"] == "tidy a and b" and len(lease["base"]) == 40, str(lease))
rc, out, _ = write_event(d, "a.py", content="y = 2\n")
check("an edit inside the globs is allowed", rc == 0 and out == {}, str(out))
rc, out, _ = write_event(d, str(d / "c.py"), content="y = 2\n")
check("an edit outside the globs is denied with the out-of-scope message", rc == 0 and is_deny(out)
      and reason_of(out).startswith("out of scope: c.py; file an issue or report it, do not edit"), str(out))
(d / "a.py").write_text("x = 1\ny = 2\n")
(d / "b.py").write_text("x = 1\ny = 2\n")
git(d, "add", "-A")
rc, out, _ = write_event(d, "b.py", tool="Edit", old_string="y = 2", new_string="y = 3")
check("an edit to a file already in the diff does not add a file", rc == 0 and out == {}, str(out))
done = scope(d, "set", "--glob", "*.py", "--max-files", "2", "--max-lines", "100", "--task", "tidy", "--base", "main")
rc, out, _ = write_event(d, "c.py", content="y = 2\n")
check("an edit that takes the branch past max_files is denied and points at scope.py check", rc == 0 and is_deny(out)
      and "scope exceeded" in reason_of(out) and "python3 scripts/scope.py check" in reason_of(out)
      and "only an explicit" in reason_of(out).lower(), str(out))
done = scope(d, "set", "--glob", "*.py", "--max-files", "5", "--max-lines", "3", "--task", "tidy", "--base", "main")
rc, out, _ = write_event(d, "a.py", content="a\nb\nc\nd\n")
check("an edit that takes the branch past max_lines is denied", rc == 0 and is_deny(out) and "scope exceeded" in reason_of(out), str(out))
done = scope(d, "set", "--glob", "*.py", "--max-files", "5", "--max-lines", "300", "--task", "tidy", "--base", "main")
rc, out, _ = write_event(d, "c.py", content="y = 2\n")
check("widening the lease with scope.py set unblocks it", rc == 0 and out == {}, str(out))
done = scope(d, "check")
check("scope.py check runs the intent re-check and says when Jev is unavailable", done.returncode == 0 and "Jev unavailable" in done.stdout
      and "task: tidy" in done.stdout, done.stdout + done.stderr)
(d / "new.py").write_text("n = 1\n")
done = scope(d, "set", "--glob", "*.py", "--max-files", "3", "--max-lines", "300", "--task", "tidy", "--base", "main")
rc, out, _ = write_event(d, "c.py", content="y = 2\n")
check("an untracked file counts toward max_files (a, b, new, c is 4 > 3)", rc == 0 and is_deny(out), str(out))
rc, out, _ = write_event(Path(tempfile.mkdtemp(dir=SCRATCH)), "x.py", content="x")
check("an edit outside any repository passes", rc == 0 and out == {})
(lease_file := Path(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "scope" / "work.json").write_text("{bad")
rc, out, _ = write_event(d, "a.py", content="x")
check("an unreadable lease denies rather than allows", rc == 0 and is_deny(out) and "unreadable" in reason_of(out), str(out))
done = scope(d, "clear")
rc, out, _ = write_event(d, "c.py", content="y = 2\n")
check("scope.py clear removes the lease and edits pass again", done.returncode == 0 and rc == 0 and out == {}, done.stdout + str(out))
git(d, "checkout", "-q", "--detach")
check("scope.py set refuses on a detached HEAD", scope(d, "set", "--glob", "a", "--max-files", "1", "--max-lines", "1", "--task", "t", "--base", "main").returncode != 0)

# --- budget_guard -------------------------------------------------------------------------------
def project(budget):
    """A copy of the hooks and scripts whose harness.toml carries the given [budget]."""
    p = Path(tempfile.mkdtemp(prefix="project.", dir=SCRATCH))
    shutil.copytree(ROOT / ".claude" / "hooks", p / ".claude" / "hooks")
    shutil.copytree(ROOT / "scripts", p / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (p / "harness.toml").write_text(f"[budget]\n{budget}\n")
    git(p, "init", "-q", "-b", "main")
    return p


def tool_call(p, session="b1", tool="Read", agent=None, tool_input=None):
    extra = {"agent_id": agent} if agent else {}
    return run_hook("budget_guard.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": session,
                                        "cwd": str(p), "tool_input": tool_input or {}, **extra}, p, project=p)


p = project("max_tool_calls = 5\nmax_minutes = 0")
results = [tool_call(p) for _ in range(7)]
check("calls 1 to 3 are silent", all(rc == 0 and out == {} for rc, out, _ in results[:3]), str(results[:3]))
check("call 5 (4 used of 5 = 80%) warns once as context", "80%" in reason_of(results[4][1]) and not is_deny(results[4][1])
      and "write status" in reason_of(results[4][1]), str(results[4]))
p10 = project("max_tool_calls = 10\nmax_minutes = 0")
r10 = [tool_call(p10) for _ in range(10)]
check("the 80% warning appears on call 9 only (call 10 is silent)", "80%" in reason_of(r10[8][1]) and r10[9][1] == {}
      and all(r[1] == {} for r in r10[:8]), str(r10[7:]))
check("call 6 (5 used of 5 = 100%) is denied with the budget message", is_deny(results[5][1])
      and "budget exhausted: write status (done, left, blockers) and stop" in reason_of(results[5][1]), str(results[5]))
check("call 7 stays denied", is_deny(results[6][1]))
check("another session has its own budget", tool_call(p, "b2")[1] == {})
done = subprocess.run([sys.executable, str(ROOT / "scripts" / "loop_reset.py"), "--session", "b1", "--reason", "extend", "--budget"],
                      cwd=p, capture_output=True, text=True, env={**ENV, "HARNESS_EVENTS_LOG": str(EVENTS)})
check("loop_reset --budget starts a fresh budget", done.returncode == 0 and tool_call(p, "b1")[1] == {}, done.stdout + done.stderr)
p = project("max_tool_calls = 0\nmax_minutes = 1")
tool_call(p)
bfile = next((Path(git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "loop").glob("b1.budget.json"))
state = json.loads(bfile.read_text())
state["active"], state["last"] = 50.0, time.time()
bfile.write_text(json.dumps(state))
rc, out, _ = tool_call(p)
check("elapsed minutes count: 50s of a 1-minute budget warns", "(1/1 minutes)" in reason_of(out) and "% of this session's budget" in reason_of(out), str(out))
state["active"], state["last"] = 120.0, time.time()
bfile.write_text(json.dumps(state))
rc, out, _ = tool_call(p)
check("elapsed minutes past the limit deny", is_deny(out), str(out))
p = project("max_tool_calls = 0\nmax_minutes = 20")
tool_call(p)
bfile = next((Path(git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "loop").glob("b1.budget.json"))
state = json.loads(bfile.read_text())
state["last"] = time.time() - 3600
bfile.write_text(json.dumps(state))
rc, out, _ = tool_call(p)
check("a gap of an hour between calls is not counted: still silent", rc == 0 and out == {}, str(out))
check("(the gap added nothing)", json.loads(bfile.read_text())["active"] < 5)
state = json.loads(bfile.read_text())
state["last"] = time.time() - 25 * 60
bfile.write_text(json.dumps(state))
rc, out, _ = tool_call(p)
check("a 25-minute gap is counted: 25 of 20 minutes denies", is_deny(out), str(out))
p = project("max_tool_calls = 1\nmax_minutes = 0")
tool_call(p)
for name, want in (("STATUS.md", True), ("notes/STATUS.md", True), ("status.md", True), ("tmp/x/status-2.md", True),
                   ("src/a.py", False), ("STATUS.txt", False)):
    for tool in ("Write", "Edit"):
        rc, out, _ = tool_call(p, tool=tool, tool_input={"file_path": str(p / name)})
        check(f"at 100% a {tool} of {name} is {'allowed' if want else 'denied'}", rc == 0 and is_deny(out) is not want, str(out))
rc, out, _ = tool_call(p, tool="Bash", tool_input={"command": "echo hi > STATUS.md"})
check("at 100% a Bash call is still denied", is_deny(out), str(out))
p = project('max_tool_calls = 1\nmax_minutes = 0\nstatus_glob = ["notes/*.txt"]')
tool_call(p)
rc, out, _ = tool_call(p, tool="Write", tool_input={"file_path": str(p / "notes" / "done.txt")})
check("a configured status_glob replaces the default", rc == 0 and not is_deny(out), str(out))
rc, out, _ = tool_call(p, tool="Write", tool_input={"file_path": str(p / "STATUS.md")})
check("and the default globs no longer apply once replaced", is_deny(out), str(out))
p = project("max_tool_calls = 2\nmax_minutes = 0")
tool_call(p, "b9")
tool_call(p, "b9")
check("(setup) the parent session is out of budget", is_deny(tool_call(p, "b9")[1]))
check("a subagent in that session has its own budget", tool_call(p, "b9", agent="sub1")[1] == {})
p = project("max_tool_calls = 0\nmax_minutes = 0")
check("0 turns both limits off", all(tool_call(p)[1] == {} for _ in range(5)))
bfile = next((Path(git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "harness" / "loop").glob("b1.budget.json"))
bfile.write_text("garbage")
rc, out, _ = tool_call(p)
check("a corrupt budget file resets with a notice", rc == 0 and "reset" in reason_of(out), str(out))

# --- turn_guard ---------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("turn_guard_under_test", ROOT / ".claude" / "hooks" / "turn_guard.py")
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)
for text, want in (
        ("Fixed the bug.\n\nNext I will run the full suite.", "Next I will"),
        ("Done with part one. I'll now update the docs.", "I'll now"),
        ("Let me now check the other callers.", "Let me now"),
        ("The tests need an update, so I'm going to rewrite them.", "I'm going to"),
        ("I will start with the adapter.", "I will start"),
        ("We'll now move to phase two.", "We'll now"),
        ("Tests are green. Let me run the full suite.", "Let me run"),
        ("Docs are updated. I'll do the adapter tests next.", "I'll do the adapter tests next.")):
    check(f"announcing message blocks: {text!r}", tg.announced(text) == want, str(tg.announced(text)))
for text in (
        "I will not touch the adapter.",
        "Next I will not run the suite; it is out of scope.",
        "This is not going to work, so I stopped.",
        "He said \"next I will run it\" in the notes.",
        "The log line `I'll now retry` is from the old script.",
        "Result:\n\n```\nnext I will run it\n```\n",
        "> next I will run the suite\n\nAll done.",
        "Shall I start with the adapter?",
        "I am going to need a decision: which base?\n\nBlocked: no access to the repo, so I cannot go on. Next I will wait.",
        "Done. Tests pass: 12 checks.",
        "Let me know if you want more.",
        "After you approve the plan, I will start the migration.",
        "If you approve, I'll now run the migration.",
        "Once you confirm the base branch, I will begin the rebase.",
        "I'll wait for your go-ahead before I start.",
        "Nothing is left, so I will now stop here.",
        "I'm going to need the repository path before I can continue.",
        "Plan:\n\n1. I will start with the adapter.\n2. Next I will run the suite.",
        "Waiting for your answer: should I proceed?",
        "Earlier I will have said so; the first paragraph only.\n\nAll finished."):
    check(f"non-announcing message passes: {text[:50]!r}", tg.announced(text) is None, str(tg.announced(text)))


def stop(cwd, message, session="t1", active=False, key="last_assistant_message"):
    return run_hook("turn_guard.py", {"hook_event_name": "Stop", "session_id": session, "cwd": str(cwd),
                                      "stop_hook_active": active, key: message}, cwd)


d = repo()
rc, out, _ = stop(d, "Next I will run the tests.")
check("an announcing final message blocks once with 'do it or name the blocker'", rc == 0 and is_block(out)
      and "do it or name the blocker" in reason_of(out) and g.RULE_TURN in reason_of(out), str(out))
rc, out, _ = stop(d, "Next I will run the tests.")
check("the same message does not block twice", rc == 0 and out == {}, str(out))
rc, out, _ = stop(d, "Then I'll start on the adapter.", session="t2", active=True)
check("a continuation (stop_hook_active) is never blocked", rc == 0 and out == {}, str(out))
rc, out, _ = stop(d, "All done.", session="t3")
check("a finished message stops silently", rc == 0 and out == {})
transcript = d / "t.jsonl"
transcript.write_text(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n" + json.dumps(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll now run it."}]}}) + "\n")
rc, out, _ = run_hook("turn_guard.py", {"hook_event_name": "Stop", "session_id": "t4", "cwd": str(d),
                                        "transcript_path": str(transcript)}, d)
check("with no last_assistant_message the Claude transcript is read", rc == 0 and is_block(out), str(out))
rc, out, _ = run_hook("turn_guard.py", {"hook_event_name": "Stop", "session_id": "t5", "cwd": str(d),
                                        "transcript_path": str(d / "missing.jsonl")}, d)
check("an unreadable transcript is a pass", rc == 0 and out == {})

# --- unreadable input: a gate that cannot read it allows rather than crashes ------------------------
for name in ("loop_guard.py", "scope_guard.py", "budget_guard.py", "turn_guard.py"):
    done = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / name)], input="not json", capture_output=True, text=True,
                          env={**ENV, "CLAUDE_PROJECT_DIR": str(ROOT)})
    check(f"{name} allows unreadable input", done.returncode == 0 and done.stdout == "", done.stdout + done.stderr)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
