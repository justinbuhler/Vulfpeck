#!/usr/bin/env python3
"""Clear one session's loop_guard counters (and, with --budget, its budget_guard counters).

    python3 scripts/loop_reset.py --session <session_id> --reason "why the loop is safe to resume"

This is the owner's deliberate recovery from a loop_guard trip: loop_guard never resets
itself. Each reset is recorded in the harness decision log (scripts/harness_events.py)
with the reason. The session id is printed in the block message.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--budget", action="store_true", help="also start a fresh budget_guard budget")
    args = parser.parse_args(argv)
    cleared = []
    for suffix in ("", ".budget") if args.budget else ("",):
        path = g.state_file("loop", Path.cwd(), args.session + suffix)
        if path is None:
            sys.exit("loop_reset: not inside a git repository")
        if path.is_file():
            path.unlink()
            cleared.append(path.name)
    g.record("loop_guard", "reset", args.reason[:80], args.session, {"session_id": args.session})
    print(f"cleared: {', '.join(cleared) or 'nothing (no state for that session)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
