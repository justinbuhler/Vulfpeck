#!/usr/bin/env python3
"""Apply each mutant, run the whole suite against it, and record whether it dies.

The suite is standalone scripts, not a framework: each prints `PASS <name>` or
`FAIL <name>` per assertion and exits non-zero if any failed. So a kill is
recorded at assertion granularity -- the ledger names the specs that went red,
not just the files -- because a mutant killed only by an import crash has no
named spec behind it and that is a gap, not a pass.

`uv run` costs ~3.5s of environment resolution per invocation, once per test
script. The interpreter is resolved once and reused, which is the only reason a
corpus of dozens of mutants is runnable in minutes rather than hours.

    python3 mutants/runner.py                    # baseline, then every patch
    python3 mutants/runner.py --only M003 M027   # named mutants only
    python3 mutants/runner.py --round 2          # tag the ledger rows
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO
from harness_inputs import copy_inputs
from harness_config import load
from harness_lock import repository_lock, git_environment
from harness_receipts import corpus_digest, identity, write_json

ROOT = REPO
LEDGER = ROOT / "mutants" / "mutation_ledger.jsonl"
# Two test idioms live in this tree and they report failure differently. Most
# files are standalone scripts printing "  FAIL <name>"; two use unittest, which
# prints "FAIL: <test> (<dotted path>)" and "ERROR: <test> (...)". Reading only
# the first idiom makes every unittest kill invisible: a mutant is recorded as a
# survivor while the test that guards it fails by name. Neither pattern matches the "FAILED (...)"
# summary line, which is a count rather than a spec.
FAIL_LINE = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+-\s.*)?$")
# Anchored on unittest's exact shape - an identifier, then a dotted path in
# parentheses - not merely a line starting "ERROR:". The loose version matches
# a passing negative test that prints an expected error, whose message embeds a
# fresh temp directory each run, so it reads as a new failing spec for every
# mutant and scores mutants as killed that were not.
UNITTEST_FAIL_LINE = re.compile(r"^(?:FAIL|ERROR):\s+([A-Za-z_]\w*)\s+\(([\w.]+)\)\s*$")
SUMMARY_LINE = re.compile(r"^\s*(\d+) passed, (\d+) failed$", re.M)


def git(*args, check=True):
    """Run one git command in the repository root and return its stdout."""
    done = subprocess.run(("git", *args), cwd=ROOT, capture_output=True, text=True, env=git_environment())
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def tree_digest():
    """Return one digest over the content of every tracked file.

    A per-file revert that leaves a stray byte anywhere else in the tree would
    make the next mutant's result a lie, so the check covers the whole tracked
    tree rather than only the file the patch names.

    The ledger is the one exclusion, and only because it is this runner's own
    output. It is appended to between mutants, so once it became a tracked file
    every row written moved the digest and the integrity check failed on the
    mutant after the first. The instrument's log is not part of the specimen.
    """
    accumulator = hashlib.sha256()
    for name in sorted(git("ls-files").splitlines()):
        path = ROOT / name
        accumulator.update(name.encode())
        accumulator.update(path.read_bytes() if path.is_file() else b"<absent>")
    return accumulator.hexdigest()


def dirty():
    """Return the modified tracked files, ignoring this runner's own ledger."""
    return [line for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
            if line.strip()]


def interpreter():
    """Return the interpreter that runs the suite, resolved once."""
    venv = ROOT / ".venv" / "bin" / "python3"
    if venv.exists():
        return [str(venv)]
    return ["uv", "run", "--no-sync", "python3"]


def suite_files():
    """Return the test scripts run.sh walks, in the same order."""
    return sorted([*(ROOT / "tests").glob("test_*.py"),
                   *ROOT.glob("*/scripts/tests/test_*.py")])


def failing_specs(output):
    """Return the named specs that failed, in either test idiom this tree uses."""
    named = set()
    for line in output.splitlines():
        for pattern in (FAIL_LINE, UNITTEST_FAIL_LINE):
            match = pattern.match(line)
            if match:
                named.add(" ".join(group.strip() for group in match.groups() if group))
                break
    return named


def run_one(command, path, timeout):
    """Return one test script's outcome as (relative path, record)."""
    name = path.relative_to(ROOT).as_posix()
    # HARNESS_EVENTS_LOG: tests run real hooks; keep mutant runs out of the real
    # decision record (scripts/harness_events.py).
    environment = dict(git_environment(), PYTHONPATH=f"{ROOT}:{ROOT / 'scripts'}",
                       HARNESS_EVENTS_LOG=os.devnull)
    with subprocess.Popen([*command, str(path)], cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
            code = child.returncode
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            out, err = child.communicate()
            code, err = -1, f"TIMEOUT after {timeout}s\n{err}"
    summary = SUMMARY_LINE.search(out)
    return name, {
        "returncode": code,
        # Both streams, because unittest writes its report to stderr while the
        # standalone scripts print theirs to stdout. Parsing stdout alone found
        # neither the format nor the stream, and hid the same four kills twice.
        "failing": sorted(failing_specs(f"{out}\n{err}")),
        "passed": int(summary.group(1)) if summary else 0,
        "counted": summary is not None,
        "stderr": err,
        "stdout": out,
    }


def discard_bytecode(target):
    """Drop the cached bytecode for one source file.

    A mutation that leaves the file the same number of bytes -- "number" for
    "string", two branches reordered -- is otherwise invisible. CPython validates
    a cached .pyc on source mtime-in-whole-seconds plus size, `git apply` rewrites
    the file inside the same second, so the interpreter imports the PREVIOUS
    module while the mutated source sits on disk. Reproduced before it was fixed.

    Only the mutated module's cache is dropped. Redirecting the whole tree with
    PYTHONPYCACHEPREFIX also works, but it changes the environment the suite
    runs in, and a test that asserts on an in-tree __pycache__ goes red under it.
    An instrument must not alter what it measures.
    """
    source = ROOT / target
    for stale in (source.parent / "__pycache__").glob(f"{source.stem}.*.pyc"):
        stale.unlink()


def run_suite(command, paths, timeout, workers):
    """Return every test script's outcome, run concurrently."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, command, path, timeout) for path in paths]
        for future in concurrent.futures.as_completed(futures):
            name, record = future.result()
            results[name] = record
    return results


def compare(baseline, current):
    """Return (killed, newly failing spec names, crash-only files, stderr tail)."""
    specs, crashes, stderr = [], [], []
    for name, before in baseline.items():
        after = current.get(name)
        if after is None:
            continue
        new_specs = sorted(set(after["failing"]) - set(before["failing"]))
        specs.extend(f"{name}::{spec}" for spec in new_specs)
        broke = before["returncode"] == 0 and after["returncode"] != 0
        if broke and not new_specs:
            crashes.append(name)
        if broke or new_specs:
            stderr.append(f"--- {name}\n{after['stderr'].strip()}")
    tail = "\n".join(stderr)[-2000:]
    return bool(specs or crashes), specs, crashes, tail


def mutants(only):
    """Return the corpus patches, filtered to the requested mutant IDs."""
    paths = sorted((ROOT / "mutants" / "patches").glob("M*.patch"))
    if only:
        wanted = set(only)
        paths = [p for p in paths if p.stem in wanted]
    return paths


def metadata(patch):
    """Return the `# key: value` header a generated patch carries."""
    fields = {}
    for line in patch.read_text(encoding="utf-8").splitlines():
        if not line.startswith("# "):
            break
        key, _, value = line[2:].partition(": ")
        fields[key] = value
    return fields



def report(ledger_path, wanted_round=None):
    """Print the mutant-to-killing-spec table for one round of the ledger.

    A mutant killed only because a script crashed has no named spec behind it.
    That is an unmapped kill: the corpus proves the suite notices, but nothing
    in the suite says what was violated, so it is reported separately rather
    than counted as coverage.
    """
    rows = [json.loads(line) for line in
            pathlib.Path(ledger_path).read_text().splitlines() if line.strip()]
    rounds = sorted({row["round"] for row in rows})
    chosen = wanted_round if wanted_round is not None else rounds[-1]
    mutants_seen = [row for row in rows
                    if row.get("record") != "baseline" and row["round"] == chosen]
    baseline = next((row for row in rows
                     if row.get("record") == "baseline" and row["round"] == chosen), {})

    print(f"# Mutation round {chosen}\n")
    print(f"- corpus: {len(mutants_seen)} mutants")
    print(f"- suite: {baseline.get('scripts')} scripts, {baseline.get('checks')} checks")
    print(f"- red at baseline (absent source data): {len(baseline.get('red_at_baseline', []))}\n")

    print("| Mutant | Target | Operator | Status | Killing spec |")
    print("|---|---|---|---|---|")
    unmapped, survivors = [], []
    for row in mutants_seen:
        target = (row.get("target") or "").split("/")[0]
        specs = row.get("failing_spec_names") or []
        crashes = row.get("crash_only_scripts") or []
        if not row.get("killed"):
            status, cell = "SURVIVED", "-"
            survivors.append(row)
        elif specs:
            status = "killed"
            cell = "<br>".join(spec.split("::", 1)[-1] for spec in specs[:3])
            if len(specs) > 3:
                cell += f"<br>(+{len(specs) - 3} more)"
        else:
            status = "killed (unmapped)"
            cell = f"crash only: {', '.join(crashes[:2])}"
            unmapped.append(row)
        print(f"| {row['mutant_id']} | {target} | {row.get('operator')} | {status} | {cell} |")

    killed = sum(1 for row in mutants_seen if row.get("killed"))
    mapped = killed - len(unmapped)
    print(f"\ncaught {killed}/{len(mutants_seen)}"
          f" ({100 * killed / max(len(mutants_seen), 1):.0f}%),"
          f" of which {mapped} map to a named spec")
    if unmapped:
        print(f"\nunmapped kills ({len(unmapped)}): "
              + ", ".join(row["mutant_id"] for row in unmapped))
    if survivors:
        print(f"\nsurvivors ({len(survivors)}):")
        for row in survivors:
            print(f"  {row['mutant_id']}  {row.get('target')}  {row.get('behaviour')}")
    return 0



def execute(args, receipt):
    """Measure the isolated specimen with mandatory stable green baselines."""
    command, paths = [sys.executable], suite_files()
    if not paths:
        raise ValueError("no test scripts in candidate")
    receipt["suite_inventory_digest"] = hashlib.sha256("\n".join(
        f"{p.relative_to(ROOT)}:{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in paths).encode()).hexdigest()
    receipt["command"] = command
    receipt["baselines"] = [run_suite(command, paths, args.timeout, args.workers) for _ in range(2)]
    if any(row["returncode"] != 0 for run in receipt["baselines"] for row in run.values()):
        raise ValueError("baseline is not green; mutation evidence refused")
    receipt["green_baseline"] = True
    baseline = receipt["baselines"][0]
    baseline_digest = tree_digest()
    failures = False
    ledger = pathlib.Path(args.ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for patch in mutants(args.only):
            fields = metadata(patch)
            applied = subprocess.run(["git", "apply", str(patch)], cwd=ROOT,
                                     capture_output=True, text=True, env=git_environment())
            row = {"mutant_id": patch.stem, "round": args.round, "target": fields.get("target"),
                   "failing_spec_names": [], "killed": False, "status": "PATCH_ERROR", "stderr": applied.stderr}
            if applied.returncode == 0:
                try:
                    if fields.get("target"):
                        discard_bytecode(fields["target"])
                    current = run_suite(command, paths, args.timeout, args.workers)
                    killed, specs, crashes, tail = compare(baseline, current)
                    row.update(killed=killed, failing_spec_names=specs, crash_only_scripts=crashes,
                               stderr_tail=tail, results=current,
                               status="TIMEOUT" if any(r["returncode"] == -1 for r in current.values()) else
                                      "KILLED" if killed and specs else "UNMAPPED" if killed else "SURVIVED")
                finally:
                    git("reset", "--hard", receipt["candidate_sha"])
                    git("clean", "-fdx")
                    copy_inputs(args.source_root, ROOT, load(ROOT)["land"]["copy_untracked"])
                    if fields.get("target"):
                        discard_bytecode(fields["target"])
                    if tree_digest() != baseline_digest:
                        raise ValueError("specimen changed outside the applied mutation")
            receipt["mutants"][patch.stem] = row
            failures |= row["status"] != "KILLED"
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            print(f"{patch.stem} {row['status']}")
    receipt["all_requested_completed"] = set(receipt["mutants"]) == set(receipt["requested_mutant_ids"])
    return int(failures or not receipt["all_requested_completed"])


def source_identity(root):
    """Capture original source bytes, staged state and every ref."""
    global ROOT
    saved = ROOT
    try:
        ROOT = root
        return {"tree": tree_digest(), "head": identity(root, "HEAD"),
                "index": git("ls-files", "--stage", "-z"),
                "refs": git("for-each-ref", "--format=%(refname) %(objectname)")}
    finally:
        ROOT = saved


def main():
    """Run candidate mutations in an independent clone under the common mutex."""
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--receipt", type=pathlib.Path)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--ledger")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--require-green", action="store_true", help="compatibility: green baseline is always required")
    args = parser.parse_args()
    ROOT = pathlib.Path(subprocess.check_output(["git", "-C", str(args.root), "rev-parse", "--show-toplevel"],
                                               env=git_environment(), text=True).strip()).resolve()
    source = ROOT
    args.source_root = source
    if args.report:
        return report(args.ledger or str(LEDGER), args.round if "--round" in sys.argv else None)
    output = (args.receipt or source / "tmp/json/mutation-receipt.json").resolve()
    args.ledger = str(pathlib.Path(args.ledger or source / "tmp/json/mutation-ledger.jsonl").resolve())
    value = dict(schema_version=1, kind="mutation", mode="full", exit_code=1, green_baseline=False,
                 all_requested_completed=False, baselines=[], mutants={})
    with repository_lock(source, "verification"):
        value["source_before"] = source_identity(source)
        try:
            candidate = identity(source, args.candidate + "^{commit}")
            if candidate != identity(source, "HEAD") or dirty():
                raise ValueError("mutation requires the clean committed candidate at HEAD")
            available = {p.stem for p in mutants(None)}
            requested = sorted(set(args.only) if args.only is not None else available)
            if not requested or not set(requested).issubset(available):
                raise ValueError("no mutants matched or unknown requested mutant ids")
            value.update(candidate_sha=candidate, candidate_tree=identity(source, candidate + "^{tree}"),
                         requested_mutant_ids=requested, corpus_digest=corpus_digest(source, requested),
                         source_before=source_identity(source))
            scratch = source / "tmp/git"
            scratch.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="mutation-", dir=scratch) as directory:
                specimen = pathlib.Path(directory) / "specimen"
                subprocess.run(["git", "clone", "--no-hardlinks", "--quiet", str(source), str(specimen)],
                               check=True, env=git_environment(), capture_output=True)
                subprocess.run(["git", "-C", str(specimen), "checkout", "--quiet", "--detach", candidate],
                               check=True, env=git_environment(), capture_output=True)
                value["input_manifest"] = copy_inputs(source, specimen, load(specimen)["land"]["copy_untracked"])
                ROOT = specimen
                args.only = requested
                value["exit_code"] = execute(args, value)
        except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            value.update(exit_code=1, error=str(exc))
            print(f"mutation: {exc}", file=sys.stderr)
        finally:
            ROOT = source
            value["source_after"] = source_identity(source)
            if value.get("source_before") != value["source_after"]:
                value.update(exit_code=1, error="source identity changed during mutation verification")
            write_json(output, value)
    return value["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
