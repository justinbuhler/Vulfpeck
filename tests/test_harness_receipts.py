"""Exercise real launch registration, process completion and stale evidence refusal."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from harness_lock import common_dir, git_environment
from harness_receipts import complete_review_launch, register_review_launch, validate, sha256


def approval(root, candidate, base=None, result=None):
    """Run a controlled independent reviewer process through production registration."""
    if base is None:
        base = subprocess.check_output(["git", "-C", str(root), "merge-base", "main", candidate],
                                       env=git_environment(), text=True).strip()
    result = result or dict(verdict="APPROVE", complete=True, findings=[],
                            consequence=dict(tier="low", rationale="fixture deterministic change"), evidence=[])
    command = [sys.executable, "-c", "import sys; print(sys.argv[1])", json.dumps(result)]
    fixture_directory = common_dir(root) / "harness/fixture-isolation"
    fixture_directory.mkdir(parents=True, exist_ok=True)
    inventory = fixture_directory / "inventory.json"
    config = fixture_directory / "config.json"
    inventory.write_text(json.dumps({"runtime": "controlled executable fixture", "external_tools": []}))
    config.write_text(json.dumps({"command": command[:3], "mode": "controlled fixture, no native-runtime proof"}))
    proofs = [{"kind": kind, "path": str(path), "sha256": sha256(path)} for kind, path in
              (("effective_config", config), ("tool_inventory", inventory))]
    launch = register_review_launch(root, candidate=candidate, base=base,
             author_session_id="fixture-author", reviewer_session_id="fixture-reviewer", runtime="codex", role="reviewer",
             command=command, isolation=dict(kind="read-only", history=False, verified=True,
                                            external_tools_disabled=True, working_directory=str(root), evidence=proofs))
    completed = subprocess.run(command, capture_output=True, text=True, env=git_environment())
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

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], env=git_environment(), text=True).strip()
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", "--allow-empty")
        candidate = git("rev-parse", "HEAD")
        receipt = approval(root, candidate)
        check("real registered reviewer process authorizes exact candidate", validate(root, candidate, receipt)["result"]["verdict"] == "APPROVE")
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
