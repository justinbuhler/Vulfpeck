#!/usr/bin/env python3
"""Turn a change's consequence tier into the exact checks to run.

AGENTS.md "Proportional testing": the tier picks what an agent runs before it
reports. The land gate is unchanged and stays the full suite (scripts/gate.sh).

    Low       lint, plus the harness.toml [[mapped_tests]] test for each changed file
    Medium    those, plus test files that name a changed module (rg)
    High      lint and the full suite (./scripts/run.sh --tests), plus the mutants
              in mutants/patches whose patch touches a changed file
    Critical  High, plus a note: independent re-derivation is required

    python3 scripts/test_plan.py                     # origin/main...HEAD
    python3 scripts/test_plan.py --base integration/phases-0-1
    python3 scripts/test_plan.py --tier High --run   # run, stop at the first failure

Without --tier the tier comes from scripts/jev.py `consequence`: a code floor
from the harness.toml [tiers] globs, raised (never lowered) by Jev when it can be
asked. A referral is printed, and the plan uses the provisional tier. JSON goes to
stdout and a human summary to stderr. The plan names the tier it ran and what it
skipped.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
import jev  # noqa: E402
from paths import REPO  # noqa: E402

FULL_SUITE = "./scripts/run.sh --tests"
SYMBOL = re.compile(r"\b(?:def|class)\s+(\w+)")


def git(root, *args):
    """Stdout of one git command in `root`; a failure is an error, not an empty answer."""
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def change(root, base):
    """The range `base...HEAD` as (live paths, numstat lines, symbol names, commit subjects)."""
    span = f"{base}...HEAD"
    files = git(root, "diff", "--name-only", "--diff-filter=d", span).split()
    stats = [
        f"{p} +{a} -{d}"
        for a, d, p in (
            line.split("\t")
            for line in git(root, "diff", "--numstat", span).splitlines()
        )
    ]
    # Names only: the text after "@@" in a hunk header, never the changed lines.
    headers = [
        line.split("@@")[-1]
        for line in git(root, "diff", "-U0", span).splitlines()
        if line.startswith("@@")
    ]
    symbols = sorted({name for h in headers for name in SYMBOL.findall(h)})
    subjects = git(root, "log", "--format=%s", span).splitlines()
    return files, stats, symbols, subjects


def affected_tests(root, files):
    """Test files that name a changed Python module, by `rg -w` on the module's stem."""
    found = set()
    for stem in sorted({Path(f).stem for f in files if f.endswith(".py")}):
        done = subprocess.run(
            ["rg", "-l", "-w", "-g", "test_*.py", "--", stem, "."],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if done.returncode > 1:
            raise SystemExit(f"rg failed: {done.stderr.strip()}")
        found.update(Path(line).as_posix() for line in done.stdout.split())
    return sorted(found)


def touching_mutants(root, files):
    """IDs of mutants/patches/M###.patch whose patch touches a changed file."""
    changed = set(files)
    ids = []
    for patch in sorted((Path(root) / "mutants" / "patches").glob("M*.patch")):
        text = patch.read_text(encoding="utf-8")
        if changed & set(re.findall(r"^--- a/(\S+)", text, re.M)):
            ids.append(patch.stem)
    return ids


def plan(root, tier, files, config):
    """Build the plan for one tier.

    Args:
        root (Path): repository root.
        tier (str): Low, Medium, High or Critical.
        files (list[str]): changed paths that still exist.
        config (dict): a loaded harness.toml.

    Returns:
        (dict): tier, commands (command and why), skipped, notes.
    """
    root = Path(root)
    commands, skipped, notes = [], [], []

    def add(command, why):
        if command and command not in [c["command"] for c in commands]:
            commands.append({"command": command, "why": why})

    def mapped(paths, why):
        for rel in paths:
            hit = harness_config.mapped_test(config, root, root / rel)
            if hit:
                add(hit[1], f"{why}: {rel}")

    if tier in ("Low", "Medium"):
        add(config["commands"]["lint"], "lint")
        mapped(files, "mapped test")
        if tier == "Medium":
            mapped(affected_tests(root, files), "test naming a changed module")
        else:
            skipped.append("test files that name a changed module (Medium)")
        skipped += ["full suite (High)", "mutation (High)"]
    else:
        add(config["commands"]["lint"], "lint")
        add(FULL_SUITE, "full suite")
        ids = touching_mutants(root, files)
        if ids:
            add(
                f"python3 mutants/runner.py --only {' '.join(ids)}",
                "mutants touching changed files",
            )
        else:
            skipped.append("mutation: no mutant patch touches a changed file")
            notes.append(
                "High with no mutant on the touched files: adversarial review owns that gap"
            )
        skipped.append("mapped and affected test scans (the full suite covers them)")
        if tier == "Critical":
            notes.append(
                "Critical: independent re-derivation (.claude/agents/dual-derive.md) and "
                "full-population evidence are required before reporting"
            )
    notes.append(
        f"the land gate is unchanged: {config['commands']['lint']} then {FULL_SUITE}"
    )
    return {"tier": tier, "commands": commands, "skipped": skipped, "notes": notes}


def run(root, commands):
    """Run each command through bash in `root`; stop at the first failure and return its status."""
    for item in commands:
        print(f"\n== {item['command']}  ({item['why']})", flush=True)
        status = subprocess.run(["bash", "-c", item["command"]], cwd=root).returncode
        if status:
            print(f"FAILED ({status}): {item['command']}", file=sys.stderr)
            return status
    return 0


def summary(result):
    """The human view of a plan."""
    lines = [
        f"tier: {result['tier']} ({result['tier_source']})  base: {result['base']}  "
        f"files: {len(result['files'])}"
    ]
    lines += [f"  run: {c['command']}   # {c['why']}" for c in result["commands"]]
    lines += [f"  skipped: {s}" for s in result["skipped"]]
    lines += [f"  note: {n}" for n in result["notes"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plan the checks for a change's consequence tier."
    )
    parser.add_argument("--base", default="origin/main", help="plan for BASE...HEAD")
    parser.add_argument(
        "--tier",
        type=str.capitalize,
        choices=jev.TIERS,
        help="skip the consequence call",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="execute the commands, stop at the first failure",
    )
    args = parser.parse_args(argv)
    root = REPO
    config = harness_config.load(root)
    files, stats, symbols, subjects = change(root, args.base)
    verdict = None
    if args.tier:
        tier, source = args.tier, "given"
    else:
        verdict = jev.consequence(
            files,
            "; ".join(subjects),
            stats=stats,
            symbols=symbols,
            tiers=config["tiers"],
        )
        tier, source = verdict["tier"], f"{verdict['source']}:{verdict['status']}"
    result = plan(root, tier, files, config)
    result.update(base=args.base, files=files, tier_source=source, consequence=verdict)
    if verdict and verdict["status"] == "referred":
        result["notes"].append(
            f"REFERRAL to the owner: {verdict['reason']}; planned at the provisional {tier}"
        )
    print(json.dumps(result, indent=2))
    print(summary(result), file=sys.stderr)
    return run(root, result["commands"]) if args.run else 0


if __name__ == "__main__":
    sys.exit(main())
