"""Exercise launcher provenance and refusal with executable protocol fixtures.

These processes test the production caller and real Git/receipt APIs, not native
client isolation. LEAD retains the separate actual-client proof obligation.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_review as review
from harness_lock import git_environment
from harness_receipts import validate

FIXTURE = r'''
import json, os, sys, time
from pathlib import Path
ROOT, CASE, FEATURES = SETTINGS
if "--version" in sys.argv:
    print("controlled protocol fixture, not native-client proof")
    raise SystemExit(0)
def emit(value):
    print(json.dumps(value), flush=True)
def reply(request, result):
    emit({"id":request["id"], "result":result})
def registered():
    launches = list((Path(ROOT)/".git/harness/reviews").glob("*/launch.json"))
    assert len(launches) == 1, "inference preceded actual registration"
    launch = json.loads(launches[0].read_text())
    assert launch["command"][0] == sys.argv[0]
    assert "SECRET_TOKEN" not in os.environ and "BASH_ENV" not in os.environ
    assert "GIT_DIR" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ
def result():
    return {"verdict":"REQUEST_CHANGES" if CASE == "request-changes" else "APPROVE",
            "complete":CASE != "incomplete", "findings":[],
            "consequence":{"tier":"high" if CASE == "high" else "low",
                "rationale":"fixture change", "required_mutant_ids":["M1"] if CASE == "high" else []},
            "evidence":[]}
def done():
    for line in sys.stdin:
        pass
    if CASE == "forced":
        time.sleep(10)
    raise SystemExit(9 if CASE == "nonzero" else 0)
if Path(sys.argv[0]).name == "claude":
    session = sys.argv[sys.argv.index("--session-id")+1]
    assert "--safe-mode" in sys.argv and "--restricted" in sys.argv
    assert "--strict-mcp-config" in sys.argv and "--no-session-persistence" in sys.argv
    assert sys.argv[sys.argv.index("--tools")+1] == "Read,Glob,Grep"
    assert json.loads(sys.argv[sys.argv.index("--mcp-config")+1]) == {"mcpServers":{}}
    request = json.loads(sys.stdin.readline())
    assert request["request"]["subtype"] == "initialize"
    if CASE == "no-inventory":
        emit({"type":"control_response", "response":{"subtype":"success", "account":"omit"}})
        done()
    emit({"type":"system", "subtype":"init", "session_id":session, "cwd":os.getcwd(),
          "tools":["Read","Glob","Grep"] + (["Bash"] if CASE == "claude-write-tool" else []),
          "mcp_servers":[], "plugins":[], "skills":[], "permissionMode":"dontAsk", "memory_paths":[]})
    request = json.loads(sys.stdin.readline())
    assert request["type"] == "user"
    registered()
    if CASE == "claude-tool-use":
        emit({"type":"assistant", "message":{"content":[{"type":"tool_use", "name":"Bash"}]}})
    if CASE == "claude-hook":
        emit({"type":"system", "subtype":"hook_started"})
    emit({"type":"result", "subtype":"success", "is_error":False, "structured_output":result()})
    done()
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        reply(request, {"userAgent":"controlled-fixture"})
    elif method == "config/read":
        disabled = any(arg.startswith("mcp_servers=") for arg in sys.argv)
        flags = {name:False for name in FEATURES}
        if CASE == "apps-feature":
            flags["apps"] = True
        reply(request, {"config":{"features":flags, "web_search":"disabled",
              "shell_environment_policy":{"inherit":"none"},
              "mcp_servers":{"fixture.with.dots":{"enabled":not disabled, "env":{"DO_NOT_RETAIN":"secret"}}}},
              "layers":[]})
    elif method == "thread/start":
        params = request["params"]
        assert "cwd" not in params and params["ephemeral"] is True
        assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
        assert params["dynamicTools"] == [] and params["config"]["model_reasoning_effort"] == "high"
        reply(request, {"thread":{"id":"fixture-native-session", "ephemeral":True,
                      "turns":[{"id":"old"}] if CASE == "history" else []},
               "cwd":os.getcwd(), "sandbox":{"type":"workspaceWrite" if CASE == "writable" else "readOnly",
                                             "networkAccess":False},
               "approvalPolicy":"never", "model":params["model"], "reasoningEffort":"high",
               "instructionSources":["public-policy-file"]})
    elif method == "mcpServerStatus/list":
        # Page two models the implicit server absent from config/read.
        second = request["params"].get("cursor") == "page-two"
        reply(request, {"data":[{"name":"implicit", "tools":{"write":{}} if CASE == "implicit" and second else {}}],
                        "nextCursor":None if second else "page-two"})
    elif method == "app/installed":
        reply(request, {"apps":[{"id":"fixture-app", "enabled":False, "callable":CASE == "callable",
                                 "auth":{"DO_NOT_RETAIN":"secret"}}]})
    elif method == "turn/start":
        registered()
        reply(request, {"turn":{"id":"turn-1", "status":"inProgress"}})
        if CASE == "timeout":
            time.sleep(10)
        if CASE == "hook":
            emit({"method":"hook/started", "params":{}})
        if CASE == "external":
            emit({"method":"item/started", "params":{"item":{"type":"mcpToolCall", "id":"forbidden-1",
                  "tool":"fixture-write", "name":"fixture-name", "server":"fixture-server",
                  "arguments":{"DO_NOT_RETAIN":"secret"}, "command":"DO_NOT_RETAIN"}}})
        if CASE == "write":
            Path("tracked.txt").write_text("unexpected write")
        emit({"method":"item/completed", "params":{"item":{"type":"agentMessage",
                  "text":"malformed" if CASE == "malformed" else json.dumps(result())}}})
        emit({"method":"turn/completed", "params":{"turn":{"id":"turn-1", "status":"completed"}}})
        if CASE == "late-hook":
            emit({"method":"hook/started", "params":{}})
        done()
    else:
        raise AssertionError(method)
'''


def main():
    """Run bounded caller checks; leave actual native inference to LEAD."""
    passed, failed = [], []

    def check(name, condition):
        (passed if condition else failed).append(name)
        print(f"{'PASS' if condition else 'FAIL'} {name}")

    scratch = ROOT / "tmp/txt"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="native-review-test-") as temporary:
        directory = Path(temporary)

        def run(case, runtime="codex", succeeds=False, timeout=3):
            root = directory / f"{runtime}-{case}"
            root.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                       "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
                       text=True, env=git_environment()).strip()

            git("init", "-q", "-b", "main")
            (root / "tracked.txt").write_text("base")
            (root / ".gitignore").write_text("tmp/\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (root / "tracked.txt").write_text("candidate")
            git("add", "tracked.txt")
            git("commit", "-qm", "candidate")
            candidate = git("rev-parse", "HEAD")
            binaries = root / "tmp/bin"
            binaries.mkdir(parents=True)
            executable = binaries / runtime
            executable.write_text(f"#!{sys.executable}\n" + FIXTURE.replace("SETTINGS", repr((str(root), case, review.FEATURES_OFF))))
            executable.chmod(0o755)
            error, receipt = None, None
            with patch.dict(os.environ, {"PATH":str(binaries)+os.pathsep+os.environ["PATH"],
                    "SECRET_TOKEN":"fixture-only", "BASH_ENV":"fixture-only", "ANTHROPIC_API_KEY":"fixture-only"}):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        receipt = review.launch(root, candidate, base, "fixture-author", runtime, timeout)
                except (ValueError, OSError, EOFError, subprocess.SubprocessError, KeyError, TypeError) as exc:
                    error = str(exc)
            check(f"{runtime} {case} {'authorizes' if succeeds else 'refuses'}", (receipt is not None) == succeeds and (error is None) == succeeds)
            if succeeds:
                record = validate(root, candidate, receipt, base)
                check(f"{runtime} process completion is bound", record["launch"]["candidate_sha"] == candidate and
                      json.loads((receipt.parent / "completion.json").read_text())["returncode"] == 0)
                launch = json.loads((receipt.parent / "launch.json").read_text())
                check(f"{runtime} independent fresh native identity", launch["reviewer_session_id"] != "fixture-author" and
                      launch["isolation"]["history"] is False)
                artifacts = next((root / "tmp/json").glob("review-*"))
                check(f"{runtime} configuration excludes secret-bearing fields", "DO_NOT_RETAIN" not in
                      "".join(path.read_text() for path in artifacts.rglob("*.json*")))
            elif case in ("implicit", "callable", "history", "writable", "apps-feature", "no-inventory", "claude-write-tool"):
                check(f"{runtime} {case} refuses before registration", not list((root / ".git/harness/reviews").glob("*/launch.json")))
            if not succeeds:
                check(f"{runtime} {case} leaves no accepted receipt", not any(
                      validate_if_present(root, candidate, path, base) for path in (root / ".git/harness/reviews").glob("*/receipt.json")))
            if case == "external":
                events = [json.loads(line) for path in (root / "tmp/json").glob("review-*/native-events.jsonl")
                          for line in path.read_text().splitlines()]
                rejected = [event["rejected_event"] for event in events if "rejected_event" in event]
                check("forbidden event retains diagnostic metadata", len(rejected) == 1 and
                      rejected[0]["method"] == "item/started" and rejected[0]["item_type"] == "mcpToolCall" and
                      rejected[0]["item_id"] == "forbidden-1" and rejected[0]["item_tool"] == "fixture-write" and
                      rejected[0]["item_name"] == "fixture-name")
                check("forbidden event omits arguments and commands", "DO_NOT_RETAIN" not in json.dumps(events) and
                      "arguments" not in rejected[0] and "command" not in rejected[0])
            return error

        run("approve", succeeds=True)
        run("approve", runtime="claude", succeeds=True)
        for case in ("implicit", "callable", "history", "writable", "apps-feature", "hook", "late-hook", "external", "write",
                     "malformed", "incomplete", "request-changes", "high", "nonzero", "timeout", "forced"):
            run(case, timeout=1 if case == "timeout" else 3)
        reason = run("no-inventory", runtime="claude")
        check("Claude absent native inventory has exact refusal", "no complete pre-inference tool inventory" in reason)
        run("claude-write-tool", runtime="claude")
        run("claude-tool-use", runtime="claude")
        run("claude-hook", runtime="claude")
        check("aggregate TOML preserves dotted keys", review.inline({"literal.key": {"enabled": False}}) ==
              '{"literal.key"={"enabled"=false}}')
        with patch.dict(os.environ, {"SECRET_TOKEN":"fixture-only", "GIT_DIR":"fixture-only", "PYTHONPATH":"fixture-only"}):
            check("native environment omits credential and loader overrides", not
                  {"SECRET_TOKEN", "GIT_DIR", "PYTHONPATH"}.intersection(review.environment()))
        denied = subprocess.run([sys.executable, str(ROOT / "scripts/native_review.py"), "--root", str(directory),
               "--candidate", "unused", "--base", "unused", "--author-session-id", "author", "--runtime", "codex",
               "--mode", "write"], capture_output=True, text=True, env=git_environment())
        check("WRITE mode refuses before Git or native launch", denied.returncode == 1 and "WRITE mode unsupported" in denied.stderr)
    print(json.dumps({"passed":passed, "failed":failed}))
    return bool(failed)


def validate_if_present(root, candidate, receipt, base):
    """Check the production validator instead of equating receipt presence with authority."""
    try:
        validate(root, candidate, receipt, base)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
