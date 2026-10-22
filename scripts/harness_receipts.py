"""Bind integration evidence to registered independent review launches.

These records provide cooperative provenance. A malicious process with the same
OS account can rewrite both records and hashes; this is not a signing service.
"""
import argparse
import hashlib
import json
import re
import secrets
import subprocess
from pathlib import Path

from harness_lock import common_dir, git_environment, repository_lock


def sha256(path: Path) -> str:
    """Hash a retained artifact."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(root: Path, revision: str) -> str:
    """Resolve an explicit Git object identity without inherited overrides."""
    done = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", revision],
                          env=git_environment(), capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write_json(path: Path, value: dict) -> None:
    """Publish a complete local evidence record atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def corpus_digest(root: Path, ids: list[str]) -> str:
    """Bind sorted mutant identities to their exact patch bytes."""
    digest = hashlib.sha256()
    for name in sorted(ids):
        if not isinstance(name, str) or not re.fullmatch(r"M\d+", name):
            raise ValueError("invalid mutant identity")
        digest.update(name.encode() + b"\0" + (root / "mutants/patches" / f"{name}.patch").read_bytes())
    return digest.hexdigest()


def register_review_launch(root: Path, *, candidate: str, base: str, author_session_id: str,
                           reviewer_session_id: str, runtime: str, role: str,
                           command: list[str], isolation: dict) -> str:
    """Register the parent-owned provenance before a fresh reviewer starts."""
    if not author_session_id or not reviewer_session_id or author_session_id == reviewer_session_id:
        raise ValueError("independent author and reviewer sessions are required")
    if runtime not in ("codex", "claude") or role not in ("reviewer", "verifier"):
        raise ValueError("unsupported reviewer runtime or role")
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("review command is required")
    if (isolation.get("history") is not False or isolation.get("verified") is not True
            or isolation.get("external_tools_disabled") is not True
            or isolation.get("kind") not in ("read-only", "sandbox-read-only")
            or not isolation.get("working_directory")):
        raise ValueError("verified no-history sandbox with external tools disabled is required")
    working_directory = Path(isolation["working_directory"])
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError("review working directory must be an existing absolute directory")
    isolation = {**isolation, "working_directory": str(working_directory.resolve(strict=True))}
    proof = isolation.get("evidence", [])
    if not isinstance(proof, list) or not {"effective_config", "tool_inventory"}.issubset(
            {item.get("kind") for item in proof if isinstance(item, dict)}):
        raise ValueError("retained effective configuration and native tool inventory are required")
    for item in proof:
        if not isinstance(item, dict) or sha256(Path(item["path"])) != item.get("sha256"):
            raise ValueError("isolation evidence digest does not match")
    candidate, base = identity(root, candidate + "^{commit}"), identity(root, base + "^{commit}")
    done = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", base, candidate],
                          env=git_environment(), capture_output=True)
    if done.returncode:
        raise ValueError("review baseline must be an ancestor of candidate")
    launch_id = secrets.token_hex(16)
    record = dict(schema_version=1, launch_id=launch_id, candidate_sha=candidate,
                  candidate_tree=identity(root, candidate + "^{tree}"), base_sha=base,
                  author_session_id=author_session_id, reviewer_session_id=reviewer_session_id,
                  runtime=runtime, role=role, command=command, isolation=isolation)
    with repository_lock(root, "reviews"):
        write_json(common_dir(root) / "harness/reviews" / launch_id / "launch.json", record)
    return launch_id


def _review_directory(root: Path, launch_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", launch_id):
        raise ValueError("invalid registered launch id")
    return common_dir(root) / "harness/reviews" / launch_id


def _check_result(result: dict) -> None:
    if not isinstance(result, dict) or result.get("verdict") not in ("APPROVE", "REQUEST_CHANGES"):
        raise ValueError("explicit reviewer verdict required")
    if result.get("complete") is not True or not isinstance(result.get("findings"), list):
        raise ValueError("complete review and explicit findings required")
    for finding in result["findings"]:
        if (not isinstance(finding, dict) or finding.get("severity") not in ("low", "medium", "high", "critical", "blocking")
                or finding.get("disposition") not in ("open", "resolved", "advisory")
                or not isinstance(finding.get("title"), str) or not finding["title"]):
            raise ValueError("review findings require title, severity and disposition")
    consequence = result.get("consequence")
    if (not isinstance(consequence, dict) or consequence.get("tier") not in ("low", "medium", "high", "critical")
            or not isinstance(consequence.get("rationale"), str) or not consequence["rationale"]):
        raise ValueError("independent consequence and rationale required")
    if not isinstance(consequence.get("required_mutant_ids", []), list):
        raise ValueError("required mutant identities must be a list")
    if not isinstance(result.get("evidence", []), list):
        raise ValueError("evidence must be a list")


def complete_review_launch(root: Path, launch_id: str, *, returncode: int, response_path: Path) -> Path:
    """Retain a terminated launch's raw response and explicit result.

    The trusted local launcher calls this after waiting for its child. No
    generic JSON-import command can create an authorizing receipt.
    """
    directory = _review_directory(root, launch_id)
    with repository_lock(root, "reviews"):
        if (directory / "completion.json").exists():
            raise ValueError("review launch already completed")
        launch = json.loads((directory / "launch.json").read_text())
        raw = response_path.read_bytes()
        write_json(directory / "completion.json", {"returncode": returncode,
                   "response_sha256": hashlib.sha256(raw).hexdigest(), "launch_sha256": sha256(directory / "launch.json")})
        (directory / "response.json").write_bytes(raw)
        if returncode != 0:
            raise ValueError("review process did not succeed")
        result = json.loads(raw)
        _check_result(result)
        receipt = {"schema_version": 1, "kind": "review", "launch": launch, "result": result}
        write_json(directory / "receipt.json", receipt)
        return directory / "receipt.json"


def validate(root: Path, candidate: str, receipt: Path, base: str | None = None) -> dict:
    """Require registered approval and consequence-specific candidate evidence."""
    candidate = identity(root, candidate + "^{commit}")
    data = json.loads(receipt.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "review":
        raise ValueError("unknown review receipt schema")
    launch = data["launch"]
    directory = _review_directory(root, launch["launch_id"])
    if receipt.resolve() != (directory / "receipt.json").resolve():
        raise ValueError("receipt is not from a registered launch")
    registered = json.loads((directory / "launch.json").read_text())
    completion = json.loads((directory / "completion.json").read_text())
    if (registered != launch or completion["returncode"] != 0
            or completion["launch_sha256"] != sha256(directory / "launch.json")
            or completion["response_sha256"] != sha256(directory / "response.json")
            or data["result"] != json.loads((directory / "response.json").read_text())):
        raise ValueError("registered review evidence was changed")
    for item in launch["isolation"]["evidence"]:
        if sha256(Path(item["path"])) != item["sha256"]:
            raise ValueError("review isolation proof changed")
    if (launch["candidate_sha"] != candidate or launch["candidate_tree"] != identity(root, candidate + "^{tree}")
            or (base is not None and launch["base_sha"] != identity(root, base + "^{commit}"))):
        raise ValueError("review candidate, tree or baseline does not match")
    result = data["result"]
    _check_result(result)
    if result["verdict"] != "APPROVE" or any(
            not isinstance(finding, dict) or finding.get("severity") in ("critical", "high", "blocking")
            and finding.get("disposition") != "resolved" for finding in result["findings"]):
        raise ValueError("review did not approve or has unresolved blockers")
    evidence = result.get("evidence", [])
    working_directory = Path(launch["isolation"]["working_directory"])
    if (not working_directory.is_absolute() or not working_directory.is_dir()
            or working_directory.resolve(strict=True) != working_directory):
        raise ValueError("registered review working directory is no longer stable and absolute")

    def evidence_path(item):
        path = Path(item["path"])
        return path if path.is_absolute() else working_directory / path

    for item in evidence:
        if not isinstance(item, dict) or sha256(evidence_path(item)) != item["sha256"]:
            raise ValueError("review evidence digest does not match")
    tier = result["consequence"]["tier"]
    if tier in ("high", "critical"):
        required = result["consequence"].get("required_mutant_ids", [])
        if not required or len(set(required)) != len(required):
            raise ValueError("high consequence requires an explicit mutation selection")
        mutations = [json.loads(evidence_path(item).read_text()) for item in evidence if item.get("kind") == "mutation"]
        if not any(_mutation_valid(root, candidate, mutation, required) for mutation in mutations):
            raise ValueError("complete full-suite candidate mutation evidence is required")
    if tier == "critical" and not {"independent_derivation", "full_population"}.issubset(
            {item.get("kind") for item in evidence}):
        raise ValueError("critical consequence requires independent derivation and population evidence")
    if tier == "critical":
        for item in evidence:
            if item.get("kind") in ("independent_derivation", "full_population"):
                proof = json.loads(evidence_path(item).read_text())
                if (proof.get("candidate_sha") != candidate or proof.get("complete") is not True
                        or not proof.get("producer_session_id")
                        or proof["producer_session_id"] == launch["author_session_id"]):
                    raise ValueError("critical evidence lacks candidate-bound independent provenance")
    return data


def _mutation_valid(root: Path, candidate: str, value: dict, required: list[str]) -> bool:
    selected = value.get("requested_mutant_ids", [])
    suites = sorted([*(root / "tests").glob("test_*.py"), *root.glob("*/scripts/tests/test_*.py")])
    inventory = hashlib.sha256("\n".join(
        f"{path.relative_to(root)}:{sha256(path)}" for path in suites).encode()).hexdigest()
    names = {path.relative_to(root).as_posix() for path in suites}
    return (value.get("schema_version") == 1 and value.get("kind") == "mutation"
            and value.get("candidate_sha") == candidate
            and value.get("candidate_tree") == identity(root, candidate + "^{tree}")
            and value.get("corpus_digest") == corpus_digest(root, selected)
            and set(required).issubset(selected) and value.get("mode") == "full"
            and value.get("exit_code") == 0 and value.get("all_requested_completed") is True
            and value.get("suite_inventory_digest") == inventory and bool(names)
            and value.get("green_baseline") is True and len(value.get("baselines", [])) == 2
            and all(set(run) == names for run in value["baselines"])
            and all(all(row.get("returncode") == 0 for row in run.values()) for run in value["baselines"])
            and set(value.get("mutants", {})) == set(selected)
            and all(row.get("status") == "KILLED" and row.get("failing_spec_names")
                    for row in value["mutants"].values())
            and value.get("source_before") == value.get("source_after"))


def main() -> int:
    """Validate registered evidence offline, without performing integration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "record-land"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--status")
    parser.add_argument("--origin")
    parser.add_argument("--merged")
    parser.add_argument("--gate-log", type=Path)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    try:
        if args.operation == "record-land":
            import time
            value = dict(schema_version=1, status=args.status, candidate_sha=args.candidate,
                         main_before=args.base, origin_sha=args.origin, merged_sha=args.merged,
                         reason=args.reason, main_after=identity(args.root, "refs/heads/main"),
                         review_receipt=str(args.receipt) if args.receipt else None,
                         review_sha256=sha256(args.receipt) if args.receipt and args.receipt.is_file() else None,
                         gate_log=str(args.gate_log) if args.gate_log else None,
                         gate_log_sha256=sha256(args.gate_log) if args.gate_log and args.gate_log.is_file() else None)
            if args.merged:
                value["merged_tree"] = identity(args.root, args.merged + "^{tree}")
            write_json(common_dir(args.root) / "harness/lands" / f"{time.time_ns()}.json", value)
            return 0
        if args.receipt is None:
            parser.error("validate requires --receipt")
        data = validate(args.root, args.candidate, args.receipt, args.base)
        print(json.dumps({"review_base_sha": data["launch"]["base_sha"], "launch_id": data["launch"]["launch_id"]}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"receipts: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
