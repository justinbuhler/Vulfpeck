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

ROOT = pathlib.Path(__file__).resolve().parents[1]
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
    done = subprocess.run(("git", *args), cwd=ROOT, capture_output=True, text=True)
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
        if name == LEDGER.relative_to(ROOT).as_posix():
            continue
        path = ROOT / name
        accumulator.update(name.encode())
        accumulator.update(path.read_bytes() if path.is_file() else b"<absent>")
    return accumulator.hexdigest()


def dirty():
    """Return the modified tracked files, ignoring this runner's own ledger."""
    return [
        line
        for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
        if line.strip() and not line.endswith(LEDGER.name)
    ]


def interpreter():
    """Return the interpreter that runs the suite, resolved once."""
    venv = ROOT / ".venv" / "bin" / "python3"
    if venv.exists():
        return [str(venv)]
    return ["uv", "run", "--no-sync", "python3"]


def suite_files():
    """Return the test scripts run.sh walks, in the same order."""
    return sorted(
        [*(ROOT / "tests").glob("test_*.py"), *ROOT.glob("*/scripts/tests/test_*.py")]
    )


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
    environment = dict(
        os.environ,
        PYTHONPATH=f"{ROOT}:{ROOT / 'scripts'}",
        HARNESS_EVENTS_LOG=os.devnull,
    )
    try:
        done = subprocess.run(
            [*command, str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
        code, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as expired:
        code = -1
        out = (
            (expired.stdout or b"").decode("utf-8", "replace")
            if isinstance(expired.stdout, bytes)
            else (expired.stdout or "")
        )
        err = f"TIMEOUT after {timeout}s"
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
    rows = [
        json.loads(line)
        for line in pathlib.Path(ledger_path).read_text().splitlines()
        if line.strip()
    ]
    rounds = sorted({row["round"] for row in rows})
    chosen = wanted_round if wanted_round is not None else rounds[-1]
    mutants_seen = [
        row
        for row in rows
        if row.get("record") != "baseline" and row["round"] == chosen
    ]
    baseline = next(
        (
            row
            for row in rows
            if row.get("record") == "baseline" and row["round"] == chosen
        ),
        {},
    )

    print(f"# Mutation round {chosen}\n")
    print(f"- corpus: {len(mutants_seen)} mutants")
    print(
        f"- suite: {baseline.get('scripts')} scripts, {baseline.get('checks')} checks"
    )
    print(
        f"- red at baseline (absent source data): {len(baseline.get('red_at_baseline', []))}\n"
    )

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
        print(
            f"| {row['mutant_id']} | {target} | {row.get('operator')} | {status} | {cell} |"
        )

    killed = sum(1 for row in mutants_seen if row.get("killed"))
    mapped = killed - len(unmapped)
    print(
        f"\ncaught {killed}/{len(mutants_seen)}"
        f" ({100 * killed / max(len(mutants_seen), 1):.0f}%),"
        f" of which {mapped} map to a named spec"
    )
    if unmapped:
        print(
            f"\nunmapped kills ({len(unmapped)}): "
            + ", ".join(row["mutant_id"] for row in unmapped)
        )
    if survivors:
        print(f"\nsurvivors ({len(survivors)}):")
        for row in survivors:
            print(f"  {row['mutant_id']}  {row.get('target')}  {row.get('behaviour')}")
    return 0


def main():
    for received in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(
            received,
            lambda number, frame: (_ for _ in ()).throw(
                KeyboardInterrupt(f"signal {number}")
            ),
        )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=12)
    # Generous on purpose: the lander's end-to-end tests take minutes under
    # parallel load, and a script that times out at baseline has no killing power.
    parser.add_argument(
        "--timeout", type=int, default=600, help="per test script, seconds"
    )
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--ledger", default=str(LEDGER))
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the mutant-to-spec table for a recorded round",
    )
    parser.add_argument(
        "--require-green",
        action="store_true",
        help="refuse to run unless every test script passes",
    )
    args = parser.parse_args()

    if args.report:
        return report(args.ledger, args.round if "--round" in sys.argv else None)

    if dirty():
        raise SystemExit("tracked files are modified; refusing to run on a dirty tree")

    # An empty corpus would run the baseline, kill nothing, and report a round.
    # A run that tested no mutant is not a result, so it is refused up front.
    if not mutants(args.only):
        raise SystemExit("no mutants matched in mutants/patches/; nothing to run")

    command = interpreter()
    paths = suite_files()
    print(f"interpreter: {' '.join(command)}\nsuite: {len(paths)} scripts")

    started = time.monotonic()
    baseline_digest = tree_digest()
    baseline = run_suite(command, paths, args.timeout, args.workers)
    red = sorted(name for name, r in baseline.items() if r["returncode"] != 0)
    checks = sum(r["passed"] for r in baseline.values())
    print(
        f"baseline: {checks} checks, {len(red)} failing scripts, "
        f"{time.monotonic() - started:.0f}s"
    )
    timed_out = sorted(name for name, r in baseline.items() if r["returncode"] == -1)
    if timed_out:
        # Not a red test: the runner is misconfigured. Every mutant would then
        # look like a survivor or a crash-only kill for these scripts.
        raise SystemExit(
            f"baseline timed out after {args.timeout}s in {timed_out}; "
            "raise --timeout (or lower --workers) and run again"
        )
    if red and args.require_green:
        raise SystemExit(f"baseline is not green: {red}")
    if red:
        # A kill is attributed differentially -- green-to-red, or a new named
        # FAIL -- so a script already red here cannot manufacture one. The names
        # are recorded so the ledger says what killing power was absent.
        print(
            f"  {len(red)} scripts red at baseline: {red}; "
            f"kills are attributed differentially"
        )

    ledger = pathlib.Path(args.ledger)
    corpus = mutants(args.only)
    killed_count = 0
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "record": "baseline",
                    "round": args.round,
                    "scripts": len(paths),
                    "checks": checks,
                    "red_at_baseline": red,
                    "tree_digest": baseline_digest,
                    "corpus": len(corpus),
                }
            )
            + "\n"
        )
        for patch in corpus:
            fields = metadata(patch)
            mutant_id = fields.get("mutant_id", patch.stem)
            applied = subprocess.run(
                ("git", "apply", str(patch)), cwd=ROOT, capture_output=True, text=True
            )
            if applied.returncode != 0:
                record = {
                    "mutant_id": mutant_id,
                    "round": args.round,
                    "patch": patch.name,
                    "error": "patch did not apply",
                    "killed": None,
                    "failing_spec_names": [],
                    "stderr_tail": applied.stderr.strip()[-2000:],
                }
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                print(f"  {mutant_id} PATCH-FAILED")
                continue

            discard_bytecode(fields["target"])
            began = time.monotonic()
            try:
                current = run_suite(command, paths, args.timeout, args.workers)
                killed, specs, crashes, tail = compare(baseline, current)
            finally:
                # A runner killed mid-mutant leaves the mutation applied, and every
                # later result is then measured against poisoned source. So the
                # revert belongs to `finally`, not to the happy path, and SIGTERM
                # is turned into an exception for the same reason: the default
                # handler exits without unwinding and would skip this block.
                git("apply", "-R", str(patch), check=False)
                discard_bytecode(fields["target"])
                after_digest = tree_digest()
                if after_digest != baseline_digest:
                    raise SystemExit(
                        f"{mutant_id}: tree not byte-identical after revert "
                        f"({after_digest} != {baseline_digest})"
                    )

            record = {
                "mutant_id": mutant_id,
                "round": args.round,
                "patch": patch.name,
                "target": fields.get("target"),
                "operator": fields.get("operator"),
                "behaviour": fields.get("behaviour"),
                "killed": killed,
                "failing_spec_names": specs,
                "crash_only_scripts": crashes,
                "stderr_tail": tail,
                "seconds": round(time.monotonic() - began, 1),
                "tree_verified": True,
            }
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            killed_count += bool(killed)
            mark = "KILLED " if killed else "SURVIVED"
            detail = (
                specs[0] if specs else (f"crash-only: {crashes[0]}" if crashes else "")
            )
            print(f"  {mutant_id} {mark} {len(specs)} specs  {detail}")

    total = len(corpus)
    print(
        f"\nround {args.round}: {killed_count}/{total} killed "
        f"({100 * killed_count / total:.0f}%), {time.monotonic() - started:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
