"""Exercise real launch registration, process completion and stale evidence refusal."""
import json
import hashlib
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from harness_lock import common_dir, git_environment
from harness_receipts import complete_review_launch, corpus_digest, identity, register_review_launch, validate, sha256


def approval(root, candidate, base=None, result=None, working_directory=None):
    """Run a controlled independent reviewer process through production registration."""
    if base is None:
        base = subprocess.check_output(["git", "-C", str(root), "merge-base", "main", candidate],
                                       env=git_environment(), text=True).strip()
    working_directory = working_directory or root
    result = result or dict(verdict="APPROVE", complete=True, findings=[],
                            consequence=dict(tier="low", rationale="fixture deterministic change"), evidence=[])
    command = [sys.executable, "-c", "import sys; print(sys.argv[1])", json.dumps(result)]
    fixture_parent = common_dir(root) / "harness/fixture-isolation"
    fixture_parent.mkdir(parents=True, exist_ok=True)
    fixture_directory = Path(tempfile.mkdtemp(prefix="launch-", dir=fixture_parent))
    inventory = fixture_directory / "inventory.json"
    config = fixture_directory / "config.json"
    inventory.write_text(json.dumps({"runtime": "controlled executable fixture", "external_tools": []}))
    config.write_text(json.dumps({"command": command[:3], "mode": "controlled fixture, no native-runtime proof"}))
    proofs = [{"kind": kind, "path": str(path), "sha256": sha256(path)} for kind, path in
              (("effective_config", config), ("tool_inventory", inventory))]
    launch = register_review_launch(root, candidate=candidate, base=base,
             author_session_id="fixture-author", reviewer_session_id="fixture-reviewer", runtime="codex", role="reviewer",
             command=command, isolation=dict(kind="read-only", history=False, verified=True,
                                            external_tools_disabled=True, working_directory=str(working_directory), evidence=proofs))
    completed = subprocess.run(command, cwd=working_directory, capture_output=True, text=True, env=git_environment())
    response = common_dir(root) / "harness" / "reviews" / launch / "fixture-response.json"
    response.write_text(completed.stdout)
    return complete_review_launch(root, launch, returncode=completed.returncode, response_path=response)


def main():
    """Check accepted, stale, self-authored and incomplete review evidence."""
    passed, failed = [], []

    def check(name, condition):
        (passed if condition else failed).append(name)
        print(f"{'PASS' if condition else 'FAIL'} {name}")

    def refused(operation):
        try:
            operation()
            return False
        except (ValueError, OSError, KeyError):
            return True

    scratch = Path(__file__).resolve().parents[1] / "tmp/txt"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        root = Path(directory).resolve()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], env=git_environment(), text=True).strip()
        git("init", "-q", "-b", "main")
        (root / "tests").mkdir()
        (root / "tests/test_fixture.py").write_text('print("fixture")\n')
        (root / "mutants/patches").mkdir(parents=True)
        (root / "mutants/patches/M001.patch").write_text("controlled mutation patch fixture\n")
        git("add", "tests", "mutants")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        candidate = git("rev-parse", "HEAD")
        receipt = approval(root, candidate)
        check("real registered reviewer process authorizes exact candidate", validate(root, candidate, receipt)["result"]["verdict"] == "APPROVE")
        with ThreadPoolExecutor(max_workers=8) as workers:
            receipts = list(workers.map(lambda _: approval(root, candidate), range(16)))
        launches = [json.loads((path.parent / "launch.json").read_text()) for path in receipts]
        evidence_paths = [item["path"] for launch in launches for item in launch["isolation"]["evidence"]]
        check("concurrent reviewers retain distinct immutable isolation artifacts", len(set(evidence_paths)) == len(evidence_paths))
        check("concurrent registered reviews all retain valid hashes",
              all(validate(root, candidate, path)["result"]["verdict"] == "APPROVE" for path in receipts))
        clone = root / "tmp/git/review-clone"
        clone.parent.mkdir(parents=True)
        git("clone", "--quiet", "--no-hardlinks", str(root), str(clone))
        source = clone / "sample.py"
        source.write_text("value = 1\n")
        result = dict(verdict="APPROVE", complete=True, findings=[],
                      consequence=dict(tier="low", rationale="controlled relative source proof"),
                      evidence=[dict(kind="source", path="sample.py", sha256=sha256(source))])
        relative = approval(root, candidate, result=result, working_directory=clone)
        absolute_result = json.loads(json.dumps(result))
        absolute_result["evidence"][0]["path"] = str(source)
        absolute = approval(root, candidate, result=absolute_result, working_directory=clone)
        raw_before = (relative.parent / "response.json").read_bytes()
        suite_name = "tests/test_fixture.py"
        mutation = dict(schema_version=1, kind="mutation", candidate_sha=candidate,
                        candidate_tree=identity(root, candidate+"^{tree}"), corpus_digest=corpus_digest(root, ["M001"]),
                        requested_mutant_ids=["M001"], mode="full", exit_code=0, all_requested_completed=True,
                        suite_inventory_digest=hashlib.sha256(f"{suite_name}:{sha256(root/suite_name)}".encode()).hexdigest(),
                        green_baseline=True, baselines=[{suite_name:dict(returncode=0)} for _ in range(2)],
                        mutants={"M001":dict(status="KILLED", failing_spec_names=[suite_name])},
                        source_before={"fixture":"same"}, source_after={"fixture":"same"})
        (clone / "mutation.json").write_text(json.dumps(mutation))
        high_result = dict(verdict="APPROVE", complete=True, findings=[],
                           consequence=dict(tier="high", rationale="controlled mutation evidence", required_mutant_ids=["M001"]),
                           evidence=[dict(kind="mutation", path="mutation.json", sha256=sha256(clone/"mutation.json"))])
        relative_high = approval(root, candidate, result=high_result, working_directory=clone)
        critical_result = json.loads(json.dumps(high_result))
        critical_result["consequence"]["tier"] = "critical"
        for kind in ("independent_derivation", "full_population"):
            proof = clone / f"{kind}.json"
            proof.write_text(json.dumps(dict(kind=kind, candidate_sha=candidate, complete=True,
                                            producer_session_id="independent-fixture")))
            critical_result["evidence"].append(dict(kind=kind, path=proof.name, sha256=sha256(proof)))
        relative_critical = approval(root, candidate, result=critical_result, working_directory=clone)
        old_cwd = Path.cwd()
        unrelated = root / "unrelated"
        unrelated.mkdir()
        (unrelated / "sample.py").write_text("wrong cwd evidence\n")
        try:
            os.chdir(unrelated)
            check("relative source evidence uses registered clone from other cwd", not refused(lambda: validate(root, candidate, relative)))
            check("absolute source evidence remains valid from other cwd", not refused(lambda: validate(root, candidate, absolute)))
            check("relative mutation JSON uses registered clone from other cwd", not refused(lambda: validate(root, candidate, relative_high)))
            check("relative critical JSON uses registered clone from other cwd", not refused(lambda: validate(root, candidate, relative_critical)))
            check("relative evidence preserves exact raw reviewer bytes", (relative.parent/"response.json").read_bytes() == raw_before)
            source.write_text("tampered clone source\n")
            check("relative clone source tampering is refused", refused(lambda: validate(root, candidate, relative)))
        finally:
            os.chdir(old_cwd)
        check("relative registration cwd is refused", refused(lambda: approval(root, candidate, working_directory=Path("."))))
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved", "--allow-empty")
        check("stale approval is refused", refused(lambda: validate(root, git("rev-parse", "HEAD"), receipt)))
        raw = root / "self-approved.json"
        raw.write_text(receipt.read_text())
        check("caller-copied approval is not registered authority", refused(lambda: validate(root, candidate, raw)))
        high = approval(root, candidate, base=candidate, result=dict(verdict="APPROVE", complete=True, findings=[],
                      consequence=dict(tier="high", rationale="changes decisions", required_mutant_ids=["M001"])))
        check("high consequence without mutation proof is refused", refused(lambda: validate(root, candidate, high)))
        check("same-session launch is refused", refused(lambda: register_review_launch(root, candidate=candidate,
              base=candidate, author_session_id="same", reviewer_session_id="same", runtime="codex", role="reviewer",
              command=["fixture"], isolation={})))
        check("unrevoked external tools cannot authorize review", refused(lambda: register_review_launch(root,
              candidate=candidate, base=candidate, author_session_id="a", reviewer_session_id="r", runtime="codex", role="reviewer",
              command=["fixture"], isolation=dict(kind="read-only", history=False, verified=True, working_directory=str(root)))))
        response = receipt.parent / "response.json"
        response.write_text("{}")
        check("changed raw response invalidates approval", refused(lambda: validate(root, candidate, receipt)))
    print(f"{len(passed)} passed, {len(failed)} failed")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
