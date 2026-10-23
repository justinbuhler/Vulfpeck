"""Exercise launcher provenance and refusal with executable protocol fixtures.

These processes test the production caller and real Git/receipt APIs, not native
client isolation. LEAD retains the separate actual-client proof obligation.
"""
import contextlib
import io
import hashlib
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
import fnmatch, hashlib, json, os, subprocess, sys, time, tomllib
from pathlib import Path
ROOT, CASE, FEATURES = SETTINGS
SUPPLIED = []
if "--version" in sys.argv:
    print("codex-cli 0.163.1" if CASE == "future-version" else "codex-cli 0.163.0")
    raise SystemExit(0)
def merge(base, overlay):
    result = dict(base)
    for key,value in overlay.items():
        result[key] = merge(result[key],value) if isinstance(result.get(key),dict) and isinstance(value,dict) else value
    return result
cli = {}
for index,arg in enumerate(sys.argv):
    if arg == "-c":
        path,raw = sys.argv[index+1].split("=",1)
        current = cli
        parts = path.split(".")
        for part in parts[:-1]: current = current.setdefault(part,{})
        current[parts[-1]] = tomllib.loads("value="+raw)["value"]
shell_policy = merge({"inherit":"all","set":{"INHERITED_API_TOKEN":"DO_NOT_RETAIN"},"include_only":["*"]},
                     cli.get("shell_environment_policy",{}))
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
    evidence = SUPPLIED
    if CASE == "relative-source":
        evidence = [{"kind":"source", "path":"tracked.txt", "sha256":hashlib.sha256(Path("tracked.txt").read_bytes()).hexdigest()}]
    return {"verdict":"REQUEST_CHANGES" if CASE == "request-changes" else "APPROVE",
            "complete":CASE != "incomplete", "findings":[],
            "consequence":{"tier":"critical" if CASE == "supplied-critical" else "high" if CASE == "high" or CASE.startswith("supplied-") else "low",
                "rationale":"fixture independent assessment", "required_mutant_ids":["M001"] if CASE == "high" or CASE.startswith("supplied-") else []},
            "evidence":evidence}
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
        if CASE == "snapshot-v1": flags["shell_snapshot"] = True
        if CASE == "snapshot-v2": flags["shell_snapshot_v2"] = True
        path_value = shell_policy["set"]["PATH"]
        assert path_value == os.pathsep.join(p for p in ("/opt/homebrew/bin","/usr/local/bin","/usr/bin","/bin","/usr/sbin","/sbin") if Path(p).is_dir())
        policy = merge({},shell_policy)
        policy["set"] = dict(policy["set"])
        if CASE == "shell-path": policy["set"]["PATH"] = "/wrong-path"
        if CASE == "shell-secret": policy["include_only"] = ["PATH","INHERITED_API_TOKEN"]
        if CASE == "shell-filter": policy["include_only"] = ["*"]
        if CASE == "shell-alias": policy["set"]["Path"] = "DO_NOT_RETAIN"
        reply(request, {"config":{"features":flags, "web_search":"disabled",
              "allow_login_shell":True if CASE == "login-shell" else cli.get("allow_login_shell",True),
              "shell_environment_policy":policy,
              "mcp_servers":{"fixture.with.dots":{"enabled":not disabled, "env":{"DO_NOT_RETAIN":"secret"}}}},
              "layers":[]})
    elif method == "thread/start":
        params = request["params"]
        assert "cwd" not in params and params["ephemeral"] is True
        assert "environments" not in params
        assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
        assert params["dynamicTools"] == [] and params["config"]["model_reasoning_effort"] == "high"
        reply(request, {"thread":{"id":"fixture-native-session", "ephemeral":True,
                      "environments":[] if CASE == "no-environment" else [{"environmentId":"remote" if CASE == "remote-environment" else "local",
                      "cwd":os.getcwd(), "runtimeWorkspaceRoots":[os.getcwd()] if CASE != "wrong-workspace" else ["/"]}],
                      "turns":[{"id":"old"}] if CASE == "history" else []},
               "cwd":os.getcwd(), "sandbox":{"type":"workspaceWrite" if CASE == "writable" else "readOnly",
                                             "networkAccess":False},
               "approvalPolicy":"never", "model":params["model"], "reasoningEffort":"high",
               "instructionSources":["public-policy-file"]})
    elif method == "mcpServerStatus/list":
        # Page two models the implicit server absent from config/read.
        second = request["params"].get("cursor") == "page-two"
        reply(request, {"data":[{"name":"implicit", "tools":{"write":{}} if CASE == "implicit" and second else {},
                       "runtimeStatus": CASE.removeprefix("connection-") if CASE.startswith("connection-") and second else "disabled",
                       "resources":[{}] if CASE == "resources" and second else [],
                       "resourceTemplates":[{}] if CASE == "templates" and second else []}],
                        "nextCursor":None if second else "page-two"})
    elif method == "app/installed":
        reply(request, {"apps":[{"id":"fixture-app", "enabled":False, "callable":CASE == "callable",
                                 "auth":{"DO_NOT_RETAIN":"secret"}}]})
    elif method == "turn/start":
        registered()
        assert cli["allow_login_shell"] is False
        assert cli["features"]["shell_snapshot"] is False and cli["features"]["shell_snapshot_v2"] is False
        assert shell_policy["inherit"] == "none"
        effective = {key:value for key,value in shell_policy["set"].items()
                     if any(fnmatch.fnmatchcase(key.lower(),pattern.lower()) for pattern in shell_policy["include_only"])}
        assert set(effective) == {"PATH"} and "INHERITED_API_TOKEN" not in effective
        child = subprocess.check_output([sys.executable,"-c","import json,os; print(json.dumps({'keys':list(os.environ),'PATH':os.environ.get('PATH')}))"],
                                        env=effective,text=True)
        child = json.loads(child)
        assert "INHERITED_API_TOKEN" not in child["keys"] and child["PATH"] == effective["PATH"]
        subprocess.run(["git","--version"],env=effective,check=True,capture_output=True)
        prompt = request["params"]["input"][0]["text"]
        SUPPLIED = json.loads(prompt.split("Supplied candidate-bound evidence:\n",1)[1].split("\nDiff:\n",1)[0])
        for proof in SUPPLIED:
            path = Path(proof["path"])
            assert path.is_absolute() and path.is_relative_to(Path.cwd())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == proof["sha256"]
            assert json.loads(path.read_text())["candidate_sha"] in prompt
        if CASE == "supplied-tamper-original":
            (Path(ROOT)/"tmp/evidence-inputs/mutation.json").write_text("changed original")
        if CASE == "supplied-tamper-snapshot":
            Path(SUPPLIED[0]["path"]).write_text("changed snapshot")
        reply(request, {"turn":{"id":"turn-1", "status":"inProgress"}})
        if CASE == "timeout":
            time.sleep(10)
        if CASE == "hook":
            emit({"method":"hook/started", "params":{}})
        if CASE == "external":
            emit({"method":"item/started", "params":{"item":{"type":"mcpToolCall", "id":"forbidden-1",
                  "tool":"fixture-write", "name":"fixture-name", "server":"fixture-server",
                  "arguments":{"DO_NOT_RETAIN":"secret"}, "command":"DO_NOT_RETAIN"}}})
        if CASE in ("intrinsic", "intrinsic-target", "intrinsic-cursor", "intrinsic-configured", "intrinsic-fetch", "intrinsic-unknown", "future-version"):
            for tool in ("list_mcp_resources", "list_mcp_resource_templates"):
                item = {"type":"mcpToolCall", "id":"discovery-1", "server":"codex", "tool":tool, "arguments":{}}
                if CASE == "intrinsic-target": item["arguments"] = {"server":"disabled"}
                if CASE == "intrinsic-cursor": item["arguments"] = {"cursor":"nonempty"}
                if CASE == "intrinsic-configured": item["server"] = "configured"
                if CASE == "intrinsic-fetch": item["tool"] = "read_mcp_resource"
                if CASE == "intrinsic-unknown": item["tool"] = "list_mcp_resources_unknown"
                emit({"method":"item/started", "params":{"item":item}})
                emit({"method":"item/completed", "params":{"item":item}})
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

        def run(case, runtime="codex", succeeds=False, timeout=3, cli=False):
            root = directory / f"{runtime}-{case}"
            root.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                       "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
                       text=True, env=git_environment()).strip()

            git("init", "-q", "-b", "main")
            (root / "tracked.txt").write_text("base")
            (root / ".gitignore").write_text("tmp/\n")
            needs_evidence = case.startswith(("supplied-", "input-"))
            if needs_evidence:
                (root / "tests").mkdir()
                (root / "tests/test_fixture.py").write_text('print("fixture")\n')
                (root / "mutants/patches").mkdir(parents=True)
                (root / "mutants/patches/M001.patch").write_text("controlled mutation patch fixture\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (root / "tracked.txt").write_text("candidate")
            git("add", "tracked.txt")
            git("commit", "-qm", "candidate")
            candidate = git("rev-parse", "HEAD")
            inputs = []
            if needs_evidence:
                input_directory = root / "tmp/evidence-inputs"
                input_directory.mkdir(parents=True)
                suite_name = "tests/test_fixture.py"
                mutation = dict(schema_version=1, kind="mutation", candidate_sha=candidate,
                    candidate_tree=git("rev-parse", "HEAD^{tree}"), requested_mutant_ids=["M001"], mode="full", exit_code=0,
                    all_requested_completed=True, green_baseline=True,
                    corpus_digest=hashlib.sha256(b"M001\0"+(root/"mutants/patches/M001.patch").read_bytes()).hexdigest(),
                    suite_inventory_digest=hashlib.sha256(f"{suite_name}:{review.sha256(root/suite_name)}".encode()).hexdigest(),
                    baselines=[{suite_name:dict(returncode=0)} for _ in range(2)],
                    mutants={"M001":dict(status="KILLED", failing_spec_names=[suite_name])},
                    source_before="fixture same", source_after="fixture same")
                if case == "input-wrong-candidate": mutation["candidate_sha"] = base
                if case == "input-wrong-tree": mutation["candidate_tree"] = git("rev-parse", base+"^{tree}")
                if case == "input-unknown-kind": mutation["kind"] = "review"
                path = input_directory / "mutation.json"
                path.write_text(json.dumps(mutation))
                inputs.append(path)
                if case == "supplied-critical":
                    for kind in ("independent_derivation", "full_population"):
                        path = input_directory / f"{kind}.json"
                        path.write_text(json.dumps(dict(kind=kind, candidate_sha=candidate, complete=True,
                                                       producer_session_id="independent-fixture")))
                        inputs.append(path)
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
                        if cli:
                            command = [sys.executable, str(ROOT/"scripts/native_review.py"), "--root", str(root),
                                       "--candidate", candidate, "--base", base, "--author-session-id", "fixture-author",
                                       "--runtime", runtime, "--timeout", str(timeout)]
                            command += [arg for path in inputs for arg in ("--evidence", str(path))]
                            completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
                            if completed.returncode:
                                error = completed.stderr
                            else:
                                receipt = Path(completed.stdout.splitlines()[-1])
                        else:
                            receipt = review.launch(root, candidate, base, "fixture-author", runtime, timeout, inputs)
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
                if case == "intrinsic":
                    inventory = json.loads((artifacts / "tool_inventory.json").read_text())
                    check("empty discovery allowance retains version/source proof", inventory["intrinsic_discovery"]["enabled"] is True
                          and inventory["intrinsic_discovery"]["runtime_version"] == "codex-cli 0.163.0"
                          and inventory["intrinsic_discovery"]["active_connections"] == 0
                          and inventory["intrinsic_discovery"]["sources"])
                if runtime == "codex" and case == "approve":
                    policy = json.loads((artifacts/"effective_config.json").read_text())["shell_environment_policy"]
                    check("inherited credential entry is excluded after merged set before child execution",
                          "INHERITED_API_TOKEN" in policy["set_keys"] and policy["effective_set_keys"] == ["PATH"]
                          and policy["include_only"] == ["PATH"])
            elif case in ("implicit", "callable", "history", "writable", "apps-feature", "no-inventory", "claude-write-tool",
                          "no-environment", "remote-environment", "wrong-workspace", "connection-starting",
                          "connection-ready", "connection-active", "connection-unknown", "resources", "templates",
                          "shell-path", "shell-secret", "shell-filter", "shell-alias", "snapshot-v1", "snapshot-v2", "login-shell",
                          "input-wrong-candidate", "input-wrong-tree", "input-unknown-kind"):
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
            if case == "shell-secret":
                check("extra shell environment values are never retained", "DO_NOT_RETAIN" not in "".join(
                      path.read_text() for path in (root/"tmp/json").rglob("*.json*")))
            if case.startswith("supplied-tamper-"):
                check(f"{case} fails before completion", error is not None and "supplied evidence changed" in error)
            return error

        run("approve", succeeds=True)
        run("intrinsic", succeeds=True)
        run("relative-source", succeeds=True)
        run("supplied-high", succeeds=True)
        run("supplied-critical", succeeds=True, cli=True)
        run("approve", runtime="claude", succeeds=True)
        for case in ("implicit", "callable", "history", "writable", "apps-feature", "no-environment", "remote-environment",
                     "wrong-workspace", "hook", "late-hook", "external", "write",
                     "malformed", "incomplete", "request-changes", "high", "nonzero", "timeout", "forced",
                     "connection-starting", "connection-ready", "connection-active", "connection-unknown", "resources", "templates",
                     "intrinsic-target", "intrinsic-cursor", "intrinsic-configured", "intrinsic-fetch", "intrinsic-unknown", "future-version",
                     "shell-path", "shell-secret", "shell-filter", "shell-alias", "snapshot-v1", "snapshot-v2", "login-shell",
                     "input-wrong-candidate", "input-wrong-tree", "input-unknown-kind",
                     "supplied-tamper-original", "supplied-tamper-snapshot"):
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
    print(f"{len(passed)} passed, {len(failed)} failed")
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
