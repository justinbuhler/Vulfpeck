#!/usr/bin/env python3
"""Manage the scope lease that .claude/hooks/scope_guard.py enforces for one branch.

    python3 scripts/scope.py set --glob 'scripts/**' --max-files 6 --max-lines 400 --task "..." [--base main]
    python3 scripts/scope.py show
    python3 scripts/scope.py clear
    python3 scripts/scope.py check      # Jev intent re-check of the diff against the task; advisory

The lease lives at <git common dir>/harness/scope/<branch>.json: local, never tracked.
`set` replaces the lease, so widening it is one explicit call. `check` changes nothing.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402


def lease_file(top):
    """Return (lease path, branch) for the checked-out branch of top, or exit 2 when detached."""
    branch = g.git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        sys.exit("scope: HEAD is detached; a lease belongs to a branch")
    return g.state_file("scope", top, branch), branch


def read(path):
    """Return the lease at path, or exit 1 when there is none or it is unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"scope: no readable lease at {path} ({exc})")


def resolve_base(top, base):
    """Return the merge base of HEAD and the base ref, or exit 2."""
    refs = [base] if base else ["origin/main", "main"]
    for ref in refs:
        sha = g.git(top, "merge-base", "HEAD", ref)
        if sha:
            return sha
    sys.exit(f"scope: cannot find a merge base with {' or '.join(refs)}; pass --base <ref>")


def summary(top, base):
    """Return a short text of the branch diff against base for Jev."""
    stat = g.git(top, "diff", "--stat", base) or ""
    untracked = g.git(top, "ls-files", "--others", "--exclude-standard") or ""
    return stat + ("\nnew files:\n" + untracked if untracked else "")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="write or replace this branch's lease")
    s.add_argument("--glob", action="append", required=True, help="repository-relative glob; repeat")
    s.add_argument("--max-files", type=int, required=True)
    s.add_argument("--max-lines", type=int, required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--base", default="", help="ref whose merge base with HEAD anchors the diff (default origin/main, main)")
    for name in ("show", "clear", "check"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    top = g.git(Path.cwd(), "rev-parse", "--show-toplevel")
    if not top:
        sys.exit("scope: not inside a git repository")
    path, branch = lease_file(top)
    if args.cmd == "set":
        if args.max_files < 1 or args.max_lines < 1:
            sys.exit("scope: --max-files and --max-lines must be at least 1")
        lease = {"v": 1, "globs": args.glob, "max_files": args.max_files, "max_lines": args.max_lines,
                 "task": args.task, "base": resolve_base(top, args.base)}
        g.save_state(path, lease)
        g.record("scope", "set", f"{args.max_files}f/{args.max_lines}l", branch)
        print(f"lease set for {branch}: {json.dumps(lease)}")
    elif args.cmd == "show":
        print(json.dumps(read(path), indent=2))
    elif args.cmd == "clear":
        existed = path.is_file()
        path.unlink(missing_ok=True)
        g.record("scope", "clear", "", branch)
        print(f"lease for {branch} {'cleared' if existed else 'was not set'}")
    else:
        lease = read(path)
        import jev

        out = jev.intent_alignment(lease["task"], summary(top, lease["base"]))
        if out["source"] != "jev":
            print(f"scope check: Jev unavailable ({out['reason']}); compare the diff with the task by hand:\n"
                  f"task: {lease['task']}")
            return 0
        print(f"scope check: drift={out['drift']}")
        for issue in out["issues"]:
            print(f"  - {issue}")
        return 1 if out["drift"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
