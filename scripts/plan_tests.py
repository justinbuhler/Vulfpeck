#!/usr/bin/env python3
"""Turn a change's consequence tier into the exact checks to run.

AGENTS.md "Proportional testing": the tier picks what an agent runs before it
reports. The land gate is unchanged and stays the full suite (scripts/gate.sh).

    Low       lint, plus the harness.toml [[mapped_tests]] test for each changed file
    Medium    those, plus tests/ files that name a changed module (rg); a path that
              no mapped or naming test covers (any non-.py file, say) replaces them
              with the full suite
    High      lint and the full suite (./scripts/run.sh --tests), plus the mutants
              in mutants/patches whose patch touches a changed file
    Critical  High, plus a note: independent re-derivation is required

    python3 scripts/plan_tests.py                     # origin/main..HEAD (plus uncommitted work)
    python3 scripts/plan_tests.py --base integration/phases-0-1
    python3 scripts/plan_tests.py --tier High --run   # run, stop at the first failure

Without --tier the tier comes from scripts/jev.py `consequence`: a code floor
from the harness.toml [tiers] globs, raised (never lowered) by Jev when it can be
asked. A referral is printed, and the plan uses the provisional tier. A --tier below
the floor is refused unless --below-floor is given, and the override is recorded.
The change is everything since the merge base: commits, staged, unstaged and
untracked work, with deleted paths and rename sources counted. JSON goes to stdout
and a human summary to stderr. The plan names the tier it ran and what it skipped.
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
    """What changed since `base`: committed, staged, unstaged and untracked work.

    Compares the working tree with the merge base of `base` and HEAD, so a plan
    made before committing sees the work. Deleted paths and the source side of a
    rename count, because they set the floor too.

    Returns:
        (dict): `paths` (every old and new path), `files` (those that exist now),
        `removed` (deleted paths and rename sources), `stats` ("path +a -d" lines),
        `symbols` (names from hunk headers, never changed lines), `subjects`
        (commit subjects of base..HEAD).
    """
    mb = git(root, "merge-base", base, "HEAD").strip()
    paths, removed = set(), set()
    tokens = [t for t in git(root, "diff", "-M", "--name-status", "-z", mb).split("\0") if t]
    i = 0
    while i < len(tokens):
        if tokens[i][0] in "RC":
            old, new = tokens[i + 1], tokens[i + 2]
            paths |= {old, new}
            removed.add(old)
            i += 3
        else:
            paths.add(tokens[i + 1])
            if tokens[i][0] == "D":
                removed.add(tokens[i + 1])
            i += 2
    paths |= {t for t in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if t}
    stats, tokens, i = [], git(root, "diff", "-M", "--numstat", "-z", mb).split("\0"), 0
    while i < len(tokens) and tokens[i]:
        added, deleted, name = tokens[i].split("\t")
        if name:
            label, i = name, i + 1
        else:
            label, i = f"{tokens[i + 1]} => {tokens[i + 2]}", i + 3
        stats.append(f"{label} +{added} -{deleted}")
    # Names only: the text after "@@" in a hunk header, never the changed lines.
    headers = [
        line.split("@@")[-1]
        for line in git(root, "diff", "-M", "-U0", mb).splitlines()
        if line.startswith("@@")
    ]
    return {
        "paths": sorted(paths),
        "files": sorted(p for p in paths if (Path(root) / p).is_file()),
        "removed": sorted(removed),
        "stats": stats,
        "symbols": sorted({name for h in headers for name in SYMBOL.findall(h)}),
        "subjects": git(root, "log", "--format=%s", f"{base}..HEAD").splitlines(),
    }


def referencing_tests(root, path):
    """Test files under a `tests/` directory that name the module `path`, by `rg -w` on its stem."""
    if not path.endswith(".py"):
        return []
    done = subprocess.run(
        ["rg", "-l", "-F", "-w", "-g", "**/tests/test_*.py", "--", Path(path).stem, "."],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if done.returncode > 1:
        raise SystemExit(f"rg failed: {done.stderr.strip()}")
    return sorted(Path(line).as_posix() for line in done.stdout.split())


def touching_mutants(root, paths):
    """IDs of mutants/patches/M###.patch whose patch touches a changed path, old or new."""
    changed = set(paths)
    ids = []
    for patch in sorted((Path(root) / "mutants" / "patches").glob("M*.patch")):
        text = patch.read_text(encoding="utf-8")
        if changed & set(re.findall(r"^--- a/(\S+)", text, re.M)):
            ids.append(patch.stem)
    return ids


def plan(root, tier, paths, config):
    """Build the plan for one tier.

    Args:
        root (Path): repository root.
        tier (str): Low, Medium, High or Critical.
        paths (list[str]): every changed path, old and new (deleted ones included).
        config (dict): a loaded harness.toml.

    Returns:
        (dict): tier, commands (command and why), skipped, notes.
    """
    root = Path(root)
    commands, skipped, notes = [], [], []

    def add(command, why):
        if command and command not in [c["command"] for c in commands]:
            commands.append({"command": command, "why": why})

    def mapped(rel, why):
        hit = harness_config.mapped_test(config, root, root / rel)
        if hit and hit[0].is_file():  # a deleted test is not a check to run
            add(hit[1], f"{why}: {rel}")
            return True
        return False

    lint = config["commands"]["lint"]
    add(lint, "lint")
    if tier == "Low":
        for rel in paths:
            mapped(rel, "mapped test")
        skipped += [
            "test files that name a changed module (Medium)",
            "full suite (High)",
            "mutation (High)",
        ]
    elif tier == "Medium":
        uncovered = []
        for rel in paths:
            covered = mapped(rel, "mapped test")
            for test in referencing_tests(root, rel):
                covered = mapped(test, f"test naming {Path(rel).stem}") or covered
            if not covered:
                uncovered.append(rel)
        if uncovered:
            commands[:] = [{"command": lint, "why": "lint"}]
            add(FULL_SUITE, f"no mapped or naming test covers {', '.join(uncovered)}")
            skipped.append("per-file tests (the full suite replaces them)")
        skipped += ["full suite (High)", "mutation (High)"] if not uncovered else ["mutation (High)"]
    else:
        add(FULL_SUITE, "full suite")
        ids = touching_mutants(root, paths)
        if ids:
            add(
                f"python3 mutants/runner.py --only {' '.join(ids)}",
                "mutants touching changed paths",
            )
        else:
            skipped.append("mutation: no mutant patch touches a changed path")
            notes.append(
                "High with no mutant on the touched paths: adversarial review owns that gap"
            )
        skipped.append("mapped and naming test scans (the full suite covers them)")
        if tier == "Critical":
            notes.append(
                "Critical: independent re-derivation (.claude/agents/dual-derive.md) and "
                "full-population evidence are required before reporting"
            )
    notes.append(f"the land gate is unchanged: {lint} then {FULL_SUITE}")
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


def main(argv=None, root=REPO):
    parser = argparse.ArgumentParser(
        description="Plan the checks for a change's consequence tier."
    )
    parser.add_argument("--base", default="origin/main", help="plan for BASE..HEAD plus uncommitted work")
    parser.add_argument(
        "--tier",
        type=str.capitalize,
        choices=jev.TIERS,
        help="skip the consequence call; refused below the floor",
    )
    parser.add_argument(
        "--below-floor",
        action="store_true",
        help="allow --tier below the floor; recorded in the output",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="execute the commands, stop at the first failure",
    )
    args = parser.parse_args(argv)
    config = harness_config.load(root)
    changed = change(root, args.base)
    floor, floor_paths = jev.floor_tier(changed["paths"], config["tiers"])
    verdict = None
    if args.tier:
        if jev.TIERS.index(args.tier) < jev.TIERS.index(floor) and not args.below_floor:
            print(
                f"refused: --tier {args.tier} is below the {floor} floor set by "
                f"{', '.join(floor_paths) or 'an empty change'}; pass --below-floor to override",
                file=sys.stderr,
            )
            return 2
        tier, source = args.tier, "given"
    else:
        verdict = jev.consequence(
            changed["paths"],
            "; ".join(changed["subjects"]),
            stats=changed["stats"],
            symbols=changed["symbols"],
            tiers=config["tiers"],
        )
        tier, source = verdict["tier"], f"{verdict['source']}:{verdict['status']}"
    result = plan(root, tier, changed["paths"], config)
    result.update(
        base=args.base,
        files=changed["paths"],
        floor=floor,
        floor_paths=floor_paths,
        below_floor_override=jev.TIERS.index(tier) < jev.TIERS.index(floor),
        tier_source=source,
        consequence=verdict,
    )
    if result["below_floor_override"]:
        result["notes"].append(f"OVERRIDE: tier {tier} is below the {floor} floor (--below-floor)")
    if verdict and verdict["status"] == "referred":
        result["notes"].append(
            f"REFERRAL to the owner: {verdict['reason']}; planned at the provisional {tier}"
        )
    print(json.dumps(result, indent=2))
    print(summary(result), file=sys.stderr)
    return run(root, result["commands"]) if args.run else 0


if __name__ == "__main__":
    sys.exit(main())
