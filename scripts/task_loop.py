#!/usr/bin/env python3
"""The task loop: issue -> state -> dispatch -> verify -> land -> publish -> reset -> next.

    python3 scripts/task_loop.py intake [--dry-run]          # record open `ready-for-agent` issues as queued
    python3 scripts/task_loop.py next [--dry-run]            # show the issue the loop would take
    python3 scripts/task_loop.py run-one [--dry-run]         # take it through to a merged pull request
    python3 scripts/task_loop.py run [--dry-run]             # run-one until the queue is empty or a stop rule fires
    python3 scripts/task_loop.py status [--json]
    python3 scripts/task_loop.py reset --issue N --reason "..."   # owner: put a blocked or failed task back in the queue
    python3 scripts/task_loop.py file-followup --title T --body B # file an issue with the queue label

The queue is GitHub issues labelled `[loop] label`. Per issue, a state file under
`<git common dir>/harness/tasks/<issue>.json` moves forward only:

    queued -> running -> verified -> landed -> published
                     \\-> blocked          (a worker that needs context, or no gates for its runtime)
                     \\-> failed           (any other stop; also from verified and landed)

Work happens in a worktree from `agent_harness.py provision`; the worker commits nothing, the
runner queues its changed files and the harness lander commits them on the task branch (ORCH-03,
ORCH-12: origin/main moves only by merging a pull request, so `land.sh` is not used here). Verification is
`plan_tests.py --run`, whose tier Jev may raise and never lower. The merge happens only when every
check of the pull request passed; there is no `--auto`, and no `--dangerously-*` flag is ever passed.

Every external command goes through `sh()`. A blocked or failed issue is never picked again until
the owner runs `reset`. The `run` loop stops after 2 consecutive tasks end failed or blocked, or when
one issue has failed twice.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
import harness_doctor  # noqa: E402
import harness_events  # noqa: E402
import jev  # noqa: E402
from paths import REPO  # noqa: E402

sys.path.insert(0, str(REPO / ".claude" / "harness"))
import agent_harness  # noqa: E402

TRANSITIONS = {
    "queued": {"running"},
    "running": {"verified", "blocked", "failed"},
    "verified": {"landed", "failed"},
    "landed": {"published", "failed"},
    "published": set(),
    "blocked": set(),
    "failed": set(),
}
WORKER_STATUSES = ("DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED")
STATUS_LINE = re.compile(r"^\W*STATUS:\s*(" + "|".join(WORKER_STATUSES) + r")\b(.*)$")
WORKER_TIMEOUT = 3600
VERIFY_TIMEOUT = 3600
LAND_TIMEOUT = 1800
CHECKS_TIMEOUT = 3600
STOP_AFTER_BAD = 2  # consecutive tasks ending failed or blocked
STOP_AFTER_FAILS = 2  # failures of one issue
RESET_SESSION = "task-loop"


class IllegalTransition(ValueError):
    """A task state moved somewhere TRANSITIONS does not allow."""


class Halt(Exception):
    """A stage ended the task: `state` is blocked or failed."""

    def __init__(self, state, reason):
        super().__init__(reason)
        self.state, self.reason = state, reason


def sh(argv, cwd=None, timeout=None):
    """Run one external command and return its CompletedProcess; the only exit from this script.

    A timeout or a missing program becomes a non-zero result rather than an exception, so the
    stage that called it reports the failure. A `--dangerously-*` argument raises: no runtime
    is ever started with its safety checks off.
    """
    if any(str(a).startswith("--dangerously") for a in argv):
        raise ValueError(
            f"refusing a --dangerously-* flag: {shlex.join(map(str, argv))}"
        )
    try:
        return subprocess.run(
            [str(a) for a in argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=agent_harness.clean_env(),
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def tail(done, n=300):
    """The end of a command's output, for a reason string."""
    return (done.stderr.strip() or done.stdout.strip())[-n:]


# --- state --------------------------------------------------------------------


def state_dir(root):
    """`<git common dir>/harness/tasks`, shared by every worktree."""
    done = sh(
        ["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if done.returncode:
        raise SystemExit(f"task_loop: not a git repository: {root}")
    path = Path(done.stdout.strip()) / "harness" / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(root, number):
    """One task's state dict, or None when the issue was never taken in."""
    path = state_dir(root) / f"{number}.json"
    return json.loads(path.read_text()) if path.is_file() else None


def save(root, state):
    """Write a state file atomically."""
    agent_harness.write_json(state_dir(root) / f"{state['issue']}.json", state)


def stamp():
    """Current UTC instant, ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(root, issue):
    """Create the queued state for an issue not seen before; return the state."""
    state = load(root, issue["number"])
    if state is None:
        state = {
            "issue": issue["number"],
            "title": issue["title"],
            "state": "queued",
            "reason": "",
            "failures": 0,
            "pr": None,
            "history": [{"state": "queued", "ts": stamp()}],
        }
        save(root, state)
    return state


def move(root, number, to, reason=""):
    """Move a task one step forward; any other move raises IllegalTransition."""
    state = load(root, number)
    if state is None:
        raise IllegalTransition(f"issue {number} has no state")
    if to not in TRANSITIONS[state["state"]]:
        raise IllegalTransition(
            f"issue {number}: {state['state']} -> {to} is not allowed"
        )
    state.update(state=to, reason=reason)
    if to == "failed":
        state["failures"] += 1
    state["history"].append({"state": to, "ts": stamp(), "reason": reason[:200]})
    save(root, state)
    harness_events.record("task_loop", to, reason, f"issue-{number}", cwd=root)
    return state


def owner_reset(root, number, reason):
    """The one backward move: the owner puts a blocked, failed or stuck task back in the queue.

    `failures` is kept, so an issue that fails again after a reset stops the loop.
    """
    state = load(root, number)
    if state is None or state["state"] in ("queued", "published"):
        raise IllegalTransition(f"issue {number} is not blocked, failed or running")
    state.update(state="queued", reason="")
    state["history"].append(
        {"state": "queued", "ts": stamp(), "reason": f"owner reset: {reason}"[:200]}
    )
    save(root, state)
    harness_events.record("task_loop", "reset", reason, f"issue-{number}", cwd=root)


# --- intake and choice --------------------------------------------------------


def list_issues(root, cfg):
    """Open issues carrying the queue label, as dicts with plain label names."""
    done = sh(
        ["gh", "issue", "list", "--label", cfg["label"], "--state", "open",
         "--json", "number,title,body,labels", "--limit", "100"],
        cwd=root,
    )  # fmt: skip
    if done.returncode:
        raise SystemExit(f"task_loop: gh issue list failed: {tail(done)}")
    return [
        {**i, "labels": [lab["name"] for lab in i["labels"]]}
        for i in json.loads(done.stdout)
    ]


def rank(issue, order):
    """Position of the issue's most urgent priority label; an unlabelled issue ranks as the last one."""
    hits = [i for i, label in enumerate(order) if label in issue["labels"]]
    return min(hits) if hits else len(order) - 1


def jev_pick(tied):
    """The tied issue Jev prefers at probability >= jev.ACCEPT_HIGH, else None.

    Sends issue numbers, titles and labels only.
    """
    questions = {
        "first": jev._choice(
            "Which of these issues should an agent do first?",
            {f"issue_{i['number']}": i["title"] for i in tied},
        )
    }
    state = {
        "issues": [
            {"number": i["number"], "title": i["title"], "labels": i["labels"]}
            for i in tied
        ]
    }
    answers, _ = jev._ask(questions, state)
    if answers is None:
        return None
    name, p = jev._top(answers["first"])
    if p < jev.ACCEPT_HIGH:
        return None
    return next((i for i in tied if f"issue_{i['number']}" == name), None)


def choose(issues, order):
    """(issue, how): highest priority first; a tie goes to Jev, else the lowest issue number."""
    best = min(rank(i, order) for i in issues)
    tied = sorted(
        (i for i in issues if rank(i, order) == best), key=lambda i: i["number"]
    )
    if len(tied) == 1:
        return tied[0], "priority"
    picked = jev_pick(tied)
    return (picked, "jev") if picked else (tied[0], "oldest")


def next_issue(root, cfg):
    """(issue, how) for the next task, or (None, None) when no queued issue is left."""
    queued = []
    for issue in list_issues(root, cfg):
        state = load(root, issue["number"])
        if state is None or state["state"] == "queued":
            queued.append(issue)
    return choose(queued, cfg["priority"]) if queued else (None, None)


# --- the commands one task runs ----------------------------------------------


def brief(issue, worktree):
    """The worker's structured brief (AGENTS.md "Dispatch and report")."""
    return (
        f"TASK: issue #{issue['number']}: {issue['title']}\n\n{issue['body'] or ''}\n\n"
        f"SCOPE: work only inside {worktree}; change only what the issue requires.\n"
        "FORBIDDEN: git commit, git add, git push, merging, spawning agents or worktrees, "
        "any --dangerously-* flag, commit trailers or attribution, files outside the worktree, "
        "user-level config, running scripts/task_loop.py.\n"
        "DONE WHEN: the issue's change is complete and `python3 scripts/plan_tests.py --run` exits 0.\n"
        "REPORT: finish with one line `STATUS: DONE`, `STATUS: DONE_WITH_CONCERNS <concerns>`, "
        "`STATUS: NEEDS_CONTEXT <what is missing>` or `STATUS: BLOCKED <why>`."
    )


def commit_message(issue):
    """`{type}: title`, at most 50 characters."""
    kind = "fix" if "bug" in issue["labels"] else "feat"
    return f"{kind}: {issue['title']}"[:50].rstrip()


def parse_status(output):
    """(status, note) from the last STATUS line of a worker's output, or (None, '')."""
    for line in reversed(output.splitlines()):
        found = STATUS_LINE.match(line.strip())
        if found:
            return found.group(1), found.group(2).strip()
    return None, ""


def plan(root, cfg, issue, files=("<changed files>",), pr="<pr>"):
    """Every command one task runs, in order, as (stage, cwd, argv)."""
    n = issue["number"]
    task, base = f"issue-{n}", cfg["base"]
    wt = root / "tmp" / "worktrees" / task
    harness = [sys.executable, str(REPO / ".claude" / "harness" / "agent_harness.py")]
    return [
        ("fetch", root, ["git", "-C", root, "fetch", "origin", base]),
        ("provision", root, [*harness, "provision", "--task", task, "--baseline", f"origin/{base}", "--path", wt]),
        ("worker", wt, [*shlex.split(cfg["workers"][cfg["runtime"]]), brief(issue, wt)]),
        ("verify", wt, ["python3", "scripts/plan_tests.py", "--base", f"origin/{base}", "--run"]),
        ("enqueue", wt, [*harness, "enqueue", "--task", task, "-m", commit_message(issue),
                         *[a for f in files for a in ("--file", f)]]),  # fmt: skip
        ("land", root, [*harness, "land", "--once"]),
        ("push", wt, ["git", "-C", wt, "push", "-u", "origin", f"agent/{task}"]),
        ("pr", root, ["gh", "pr", "create", "--base", base, "--head", f"agent/{task}", "--title", issue["title"],
                      "--body", f"Closes #{n}", "-a", "@me"]),  # fmt: skip
        ("checks", root, ["gh", "pr", "checks", pr, "--watch", "--fail-fast"]),
        ("merge", root, ["gh", "pr", "merge", pr, "--merge", "--delete-branch"]),
        ("reset", root, [sys.executable, str(REPO / "scripts" / "loop_reset.py"), "--session", RESET_SESSION,
                         "--budget", "--reason", f"task loop: after #{n}"]),  # fmt: skip
    ]  # fmt: skip


def show(stages):
    """Print commands without running them."""
    for stage, cwd, argv in stages:
        print(f"  {stage:<9} (in {cwd}) {shlex.join(map(str, argv))}")


def stage(argv, cwd, what, timeout=None):
    """Run a command that must succeed; a non-zero exit halts the task as failed."""
    done = sh(argv, cwd, timeout)
    if done.returncode:
        raise Halt("failed", f"{what} exited {done.returncode}: {tail(done)}")
    return done


def changed_files(worktree):
    """Modified, deleted and new (not ignored) paths in the worktree, literal."""
    done = stage(["git", "-C", worktree, "ls-files", "-m", "-d", "-o", "--exclude-standard", "-z"],
                 worktree, "listing changed files")  # fmt: skip
    return sorted(p for p in done.stdout.split("\0") if p)


def run_one(root, cfg, issue):
    """Take one issue to a merged pull request, or to blocked or failed. Returns the final state."""
    n = issue["number"]
    plans = {name: (cwd, argv) for name, cwd, argv in plan(root, cfg, issue)}
    wt = plans["worker"][0]
    ensure(root, issue)
    move(root, n, "running")
    try:
        stage(plans["fetch"][1], root, "git fetch")
        stage(plans["provision"][1], root, "provision")
        if cfg["runtime"] == "codex":
            drift = harness_doctor.codex_main_hooks(wt)
            if drift:
                raise Halt(
                    "blocked",
                    f"codex would run without hook gates: main's .codex/hooks.json is {drift}",
                )
        done = sh(plans["worker"][1], wt, WORKER_TIMEOUT)
        status, note = parse_status(done.stdout)
        if status in ("NEEDS_CONTEXT", "BLOCKED"):
            raise Halt("blocked", f"{status}: {note}")
        if done.returncode or status is None:
            raise Halt(
                "failed",
                f"worker exited {done.returncode} with status {status}: {tail(done)}",
            )
        stage(plans["verify"][1], wt, "plan_tests --run", VERIFY_TIMEOUT)
        move(root, n, "verified", status)
        files = changed_files(wt)
        if not files:
            raise Halt("failed", "the worker changed nothing")
        enqueue = next(
            argv for name, _, argv in plan(root, cfg, issue, files) if name == "enqueue"
        )
        stage(enqueue, wt, "enqueue")
        stage(plans["land"][1], root, "land", LAND_TIMEOUT)
        move(root, n, "landed")
        stage(plans["push"][1], wt, "git push")
        created = stage(plans["pr"][1], root, "gh pr create")
        pr = re.search(r"/pull/(\d+)", created.stdout)
        if not pr:
            raise Halt(
                "failed", f"no pull request number in: {created.stdout.strip()[-200:]}"
            )
        state = load(root, n)
        state["pr"] = pr.group(1)
        save(root, state)
        pr_plan = {
            name: argv for name, _, argv in plan(root, cfg, issue, pr=pr.group(1))
        }
        if sh(pr_plan["checks"], root, CHECKS_TIMEOUT).returncode:
            raise Halt(
                "failed",
                f"checks failed on PR #{pr.group(1)}: {fail_comment(root, pr.group(1))}",
            )
        stage(pr_plan["merge"], root, "gh pr merge")
        move(root, n, "published", f"PR #{pr.group(1)}")
    except Halt as halt:
        move(root, n, halt.state, halt.reason)
    return load(root, n)["state"]


def fail_comment(root, pr):
    """Comment the failing check names on the PR (it stays open); return them."""
    done = sh(["gh", "pr", "checks", pr, "--json", "name,bucket"], cwd=root)
    try:
        names = [c["name"] for c in json.loads(done.stdout) if c["bucket"] == "fail"]
    except (ValueError, KeyError, TypeError):
        names = []
    text = ", ".join(names) or "unknown"
    sh(
        [
            "gh",
            "pr",
            "comment",
            pr,
            "--body",
            f"Task loop: failing checks: {text}. Left open, not merged.",
        ],
        cwd=root,
    )
    return text


def reset_between(root, number, final):
    """Run loop_reset.py as code between tasks; its failure stops the loop."""
    done = sh([sys.executable, str(REPO / "scripts" / "loop_reset.py"), "--session", RESET_SESSION, "--budget",
               "--reason", f"task loop: after #{number} ({final})"], root)  # fmt: skip
    if done.returncode:
        raise SystemExit(f"task_loop: loop_reset failed: {tail(done)}")
    harness_events.record(
        "task_loop", "reset", f"after #{number}", RESET_SESSION, cwd=root
    )


def run_loop(root, cfg):
    """run-one until the queue is empty (0) or a stop rule fires (1)."""
    bad = 0
    while True:
        issue, how = next_issue(root, cfg)
        if issue is None:
            print("queue empty")
            return 0
        print(f"#{issue['number']} {issue['title']} (chosen by {how})")
        final = run_one(root, cfg, issue)
        print(f"  -> {final}")
        reset_between(root, issue["number"], final)
        bad = bad + 1 if final in ("failed", "blocked") else 0
        if bad >= STOP_AFTER_BAD:
            print(f"stop: {bad} consecutive tasks ended failed or blocked")
            return 1
        if load(root, issue["number"])["failures"] >= STOP_AFTER_FAILS:
            print(f"stop: #{issue['number']} has failed {STOP_AFTER_FAILS} times")
            return 1


# --- subcommands --------------------------------------------------------------


def cmd_intake(root, cfg, args):
    """Record open queue-labelled issues as queued."""
    for issue in list_issues(root, cfg):
        known = load(root, issue["number"])
        print(
            f"  #{issue['number']:<5} {known['state'] if known else 'new':<10} {issue['title']}"
        )
        if not args.dry_run:
            ensure(root, issue)
    return 0


def cmd_next(root, cfg, args):
    """Print the issue the loop would take and the commands it would run."""
    issue, how = next_issue(root, cfg)
    if issue is None:
        print("queue empty")
        return 0
    print(
        f"next: #{issue['number']} {issue['title']} (chosen by {how}; labels: {', '.join(issue['labels'])})"
    )
    show(plan(root, cfg, issue))
    return 0


def cmd_run_one(root, cfg, args):
    """Run the next task, or with --dry-run print its commands."""
    if args.dry_run:
        return cmd_next(root, cfg, args)
    issue, how = next_issue(root, cfg)
    if issue is None:
        print("queue empty")
        return 0
    print(f"#{issue['number']} {issue['title']} (chosen by {how})")
    final = run_one(root, cfg, issue)
    print(f"  -> {final}")
    return 0 if final == "published" else 1


def cmd_run(root, cfg, args):
    """Run the loop, or with --dry-run print the first task's commands."""
    return cmd_next(root, cfg, args) if args.dry_run else run_loop(root, cfg)


def cmd_status(root, cfg, args):
    """Print every task's state."""
    states = [
        json.loads(p.read_text())
        for p in sorted(state_dir(root).glob("*.json"), key=lambda p: int(p.stem))
    ]
    if args.json:
        print(json.dumps(states, indent=2))
    for s in [] if args.json else states:
        print(
            f"  #{s['issue']:<5} {s['state']:<10} failures={s['failures']} pr={s['pr'] or '-'} {s['reason'][:80]}"
        )
    return 0


def cmd_reset(root, cfg, args):
    """Owner reset of one task."""
    if args.dry_run:
        print(f"would reset #{args.issue}: {args.reason}")
        return 0
    owner_reset(root, args.issue, args.reason)
    return 0


def cmd_followup(root, cfg, args):
    """File a follow-up issue with the queue label."""
    argv = [
        "gh",
        "issue",
        "create",
        "--title",
        args.title,
        "--body",
        args.body,
        "--label",
        cfg["label"],
    ]
    if args.dry_run:
        show([("issue", root, argv)])
        return 0
    done = sh(argv, cwd=root)
    print(done.stdout.strip() or tail(done))
    return done.returncode


def build_parser():
    """The argument parser for every subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func, mutating in (
        ("intake", cmd_intake, True),
        ("next", cmd_next, False),
        ("run-one", cmd_run_one, True),
        ("run", cmd_run, True),
        ("status", cmd_status, False),
        ("reset", cmd_reset, True),
        ("file-followup", cmd_followup, True),
    ):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if mutating:
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="print the commands, run none of them",
            )
    sub.choices["status"].add_argument("--json", action="store_true")
    sub.choices["next"].add_argument(
        "--dry-run", action="store_true", help="accepted; next never mutates"
    )
    sub.choices["reset"].add_argument("--issue", type=int, required=True)
    sub.choices["reset"].add_argument("--reason", required=True)
    sub.choices["file-followup"].add_argument("--title", required=True)
    sub.choices["file-followup"].add_argument("--body", required=True)
    return parser


def main(argv=None, root=REPO):
    """Dispatch one subcommand and return its exit code."""
    args = build_parser().parse_args(argv)
    cfg = harness_config.load(root)["loop"]
    return args.func(Path(root), cfg, args)


if __name__ == "__main__":
    sys.exit(main())
