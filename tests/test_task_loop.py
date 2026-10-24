"""scripts/task_loop.py: the loop's choices, state rules and stop rules, against fake gh, workers and push.

Each scenario builds a throwaway git repo with a bare `origin`, and puts shims for `gh`, the worker
CLIs and `git push` first on PATH; `git worktree`, the harness provisioner and lander, `loop_reset.py`
and the commit gate run for real. Every expectation is a literal written here (Rule 11).
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import harness_config  # noqa: E402
import task_loop  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


REAL_GIT = shutil.which("git")
TMP = ROOT / "tmp" / "test-task-loop"
shutil.rmtree(TMP, ignore_errors=True)
SHIMS = TMP / "shims"
SHIMS.mkdir(parents=True)

SHIM_SOURCES = {
    "gh": """#!/bin/sh
echo "gh $*" >> "$SHIM_LOG"
case "$1 $2" in
  "issue list") cat "$FAKE_ISSUES" ;;
  "pr create") echo "https://github.com/o/r/pull/42" ;;
  "pr checks") case "$*" in
      *--json*) echo '[{"name":"tests","bucket":"fail"},{"name":"lint","bucket":"pass"}]' ;;
      *) exit "${FAKE_CHECKS_RC:-0}" ;;
    esac ;;
esac
exit 0
""",
    "worker": """#!/bin/sh
echo "$(basename "$0") $1 cwd=$(pwd)" >> "$SHIM_LOG"
for last; do :; done
printf '%s' "$last" > "$SHIM_DIR/brief.txt"
echo work > feature.txt
printf 'STATUS: %s because\\n' "${FAKE_WORKER_STATUS:-DONE}"
exit "${FAKE_WORKER_RC:-0}"
""",
    "git": """#!/bin/sh
for a; do
  if [ "$a" = push ]; then echo "git $*" >> "$SHIM_LOG"; exit 0; fi
done
exec "$REAL_GIT" "$@"
""",
}
for name, text in {"gh": SHIM_SOURCES["gh"], "claude": SHIM_SOURCES["worker"],
                   "codex": SHIM_SOURCES["worker"], "gemini": SHIM_SOURCES["worker"],
                   "git": SHIM_SOURCES["git"]}.items():  # fmt: skip
    (SHIMS / name).write_text(text)
    (SHIMS / name).chmod(0o755)

PLAN_STUB = """import os, sys
open(os.environ["SHIM_LOG"], "a").write("plan_tests " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_VERIFY_RC", "0")))
"""
FAKE_VARS = (
    "FAKE_ISSUES",
    "FAKE_CHECKS_RC",
    "FAKE_WORKER_STATUS",
    "FAKE_WORKER_RC",
    "FAKE_VERIFY_RC",
)
os.environ["PATH"] = f"{SHIMS}{os.pathsep}{os.environ['PATH']}"
os.environ["REAL_GIT"] = REAL_GIT
os.environ["JEV_DISABLE"] = "1"
for leak in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[leak]


def real(*argv, cwd):
    subprocess.run(
        [REAL_GIT, *argv], cwd=cwd, check=True, capture_output=True, text=True
    )


def issue(number, title, *labels, body=""):
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": n} for n in labels],
    }


def scenario(issues, codex_hooks=False, **env):
    """A fresh repo, remote and shim state; returns (repo path, log path, events path)."""
    s = Path(tempfile.mkdtemp(prefix="s.", dir=TMP))
    repo, remote = s / "repo", s / "remote.git"
    repo.mkdir()
    real("init", "-q", "--bare", str(remote), cwd=s)
    real("init", "-q", "-b", "main", cwd=repo)
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@example.com"),
        ("core.hooksPath", "hooks"),
    ):
        real("config", key, value, cwd=repo)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "plan_tests.py").write_text(PLAN_STUB)
    (repo / "hooks").mkdir()
    (repo / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (repo / "hooks" / "pre-commit").chmod(0o755)
    (repo / ".gitignore").write_text("tmp/\n")
    if codex_hooks:
        (repo / ".codex").mkdir()
        (repo / ".codex" / "hooks.json").write_text("{}\n")
    real("add", ".", cwd=repo)
    real("commit", "-q", "-m", "chore: seed", cwd=repo)
    real("remote", "add", "origin", str(remote), cwd=repo)
    real("push", "-q", "-u", "origin", "main", cwd=repo)
    (s / "issues.json").write_text(json.dumps(issues))
    for key in FAKE_VARS:
        os.environ.pop(key, None)
    os.environ.update(FAKE_ISSUES=str(s / "issues.json"), SHIM_LOG=str(s / "shim.log"), SHIM_DIR=str(s),
                      HARNESS_EVENTS_LOG=str(s / "events.jsonl"))  # fmt: skip
    os.environ.update({k: str(v) for k, v in env.items()})
    (s / "shim.log").write_text("")
    return repo, s / "shim.log", s / "events.jsonl"


def cfg_for(repo, **over):
    return {**harness_config.load(repo)["loop"], **over}


def lines(path):
    return path.read_text().splitlines()


def events(path, hook, decision):
    if not path.is_file():
        return []
    rows = [json.loads(x) for x in lines(path)]
    return [r for r in rows if r["hook"] == hook and r["decision"] == decision]


def quiet(func, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args)
    return result, out.getvalue()


# --- choice: priority, Jev tie-break, deterministic fallback -------------------
ORDER = ["priority:high", "priority:medium", "priority:low"]
mixed = [
    {**issue(7, "seven", "priority:low"), "labels": ["priority:low"]},
    {**issue(5, "five"), "labels": []},
    {**issue(9, "nine", "priority:high"), "labels": ["priority:high"]},
    {**issue(3, "three", "priority:medium"), "labels": ["priority:medium"]},
]
picked, how = task_loop.choose(mixed, ORDER)
check(
    "the highest priority label wins whatever the issue number",
    (picked["number"], how) == (9, "priority"),
)
picked, how = task_loop.choose(mixed[:2] + mixed[3:], ORDER)
check("medium beats low and unlabelled", (picked["number"], how) == (3, "priority"))
picked, how = task_loop.choose(mixed[:2], ORDER)
check(
    "an unlabelled issue ranks as low: the tie with priority:low goes to the oldest issue number when Jev is off",
    (picked["number"], how) == (5, "oldest"),
)
check(
    "an issue with two priority labels ranks by the most urgent",
    task_loop.rank({"labels": ["priority:low", "priority:high"]}, ORDER) == 0,
)

sent = {}


def jev_says(answers):
    def fake(questions, state):
        sent["state"], sent["questions"] = state, questions
        return answers, None if answers else "down"

    return fake


real_ask = task_loop.jev._ask
tie = [
    {**issue(5, "five"), "labels": []},
    {**issue(8, "eight", body="SECRET BODY"), "labels": ["x"]},
]
task_loop.jev._ask = jev_says(
    {"first": {"probabilities": {"issue_5": 0.1, "issue_8": 0.9}}}
)
picked, how = task_loop.choose(tie, ORDER)
check(
    "a tie goes to the issue Jev prefers at 0.9", (picked["number"], how) == (8, "jev")
)
check("Jev is sent numbers, titles and labels only, never a body",
      sent["state"] == {"issues": [{"number": 5, "title": "five", "labels": []},
                                   {"number": 8, "title": "eight", "labels": ["x"]}]})  # fmt: skip
task_loop.jev._ask = jev_says(
    {"first": {"probabilities": {"issue_5": 0.4, "issue_8": 0.5}}}
)
check(
    "a Jev answer under 0.6 falls back to the oldest issue",
    task_loop.choose(tie, ORDER) == (tie[0], "oldest"),
)
task_loop.jev._ask = jev_says(None)
check(
    "a Jev error falls back to the oldest issue",
    task_loop.choose(tie, ORDER) == (tie[0], "oldest"),
)
task_loop.jev._ask = real_ask

# --- state transitions ---------------------------------------------------------
repo, log, ev = scenario([])
task_loop.ensure(repo, {"number": 1, "title": "one"})
for frm_to in (
    ("queued", "landed"),
    ("queued", "published"),
    ("queued", "verified"),
    ("queued", "failed"),
):
    try:
        task_loop.move(repo, 1, frm_to[1])
        check(f"{frm_to[0]} -> {frm_to[1]} raises", False)
    except task_loop.IllegalTransition:
        check(f"{frm_to[0]} -> {frm_to[1]} raises", True)
task_loop.move(repo, 1, "running")
task_loop.move(repo, 1, "failed", "boom")
for to in ("queued", "running", "verified", "published"):
    try:
        task_loop.move(repo, 1, to)
        check(f"failed -> {to} raises", False)
    except task_loop.IllegalTransition:
        check(f"failed -> {to} raises", True)
st = task_loop.load(repo, 1)
check(
    "a failure is counted and its reason kept",
    (st["state"], st["failures"], st["reason"]) == ("failed", 1, "boom"),
)
task_loop.owner_reset(repo, 1, "fixed upstream")
st = task_loop.load(repo, 1)
check(
    "the owner reset requeues and keeps the failure count",
    (st["state"], st["failures"]) == ("queued", 1),
)
try:
    task_loop.owner_reset(repo, 1, "again")
    check("a queued task cannot be reset", False)
except task_loop.IllegalTransition:
    check("a queued task cannot be reset", True)
check(
    "a transition is recorded in the decision log",
    len(events(ev, "task_loop", "failed")) == 1,
)

# --- a green task end to end ---------------------------------------------------
repo, log, ev = scenario([issue(11, "Add widget", "priority:high")])
final = task_loop.run_one(
    repo,
    cfg_for(repo),
    {**issue(11, "Add widget", "priority:high"), "labels": ["priority:high"]},
)
text = log.read_text()
check(
    "green task ends published",
    final == "published",
    task_loop.load(repo, 11)["reason"],
)
check(
    "verify ran in the worktree against origin/main",
    "plan_tests --base origin/main --run" in text,
)
wt = repo / "tmp" / "worktrees" / "issue-11"
subject = subprocess.run(
    [REAL_GIT, "-C", str(wt), "log", "-1", "--format=%s"],
    capture_output=True,
    text=True,
).stdout
check(
    "the lander committed the worker's file on the task branch",
    subject.strip() == "feat: Add widget",
)
check("the branch was pushed", "git -C %s push -u origin agent/issue-11" % wt in text)
check(
    "the PR targets the base and links the issue",
    "gh pr create --base main --head agent/issue-11 --title Add widget --body Closes #11 -a @me"
    in text,
)
check(
    "merge is called with --merge and --delete-branch",
    "gh pr merge 42 --merge --delete-branch" in text,
)
check("auto-merge is never requested", "--auto" not in text)
check(
    "checks are watched before the merge",
    text.index("gh pr checks 42 --watch --fail-fast") < text.index("gh pr merge"),
)
check("the PR number is kept in the state", task_loop.load(repo, 11)["pr"] == "42")
brief = (ev.parent / "brief.txt").read_text()
for needle in (
    "issue #11: Add widget",
    "SCOPE:",
    "FORBIDDEN:",
    "DONE WHEN:",
    "STATUS: DONE",
    "commit trailers",
    "spawning agents",
):
    check(f"the brief states {needle!r}", needle in brief)

# --- red CI: failed, PR left open, no merge ------------------------------------
repo, log, ev = scenario([], FAKE_CHECKS_RC=1)
final = task_loop.run_one(repo, cfg_for(repo), {**issue(12, "Red one"), "labels": []})
text = log.read_text()
check("red CI ends failed", final == "failed")
check("red CI never calls merge", "gh pr merge" not in text)
check(
    "red CI comments the failing check name and leaves the PR open",
    "gh pr comment 42 --body Task loop: failing checks: tests. Left open, not merged."
    in text,
)
check(
    "the failure reason names the PR",
    "checks failed on PR #42: tests" in task_loop.load(repo, 12)["reason"],
)

# --- worker statuses -----------------------------------------------------------
for status in ("NEEDS_CONTEXT", "BLOCKED"):
    repo, log, ev = scenario([issue(13, "Needs things")], FAKE_WORKER_STATUS=status)
    final = task_loop.run_one(
        repo, cfg_for(repo), {**issue(13, "Needs things"), "labels": []}
    )
    check(f"worker {status} ends blocked", final == "blocked")
    check(
        f"worker {status} gets no verify, no push, no PR",
        not any(w in log.read_text() for w in ("plan_tests", "push", "pr create")),
    )
    cfg = cfg_for(repo)
    check(
        f"a {status} issue is not picked again while gh still lists it",
        task_loop.next_issue(repo, cfg) == (None, None),
    )
    task_loop.owner_reset(repo, 13, "context added")
    check(
        f"after the owner reset it is picked again",
        task_loop.next_issue(repo, cfg)[0]["number"] == 13,
    )
repo, log, ev = scenario([], FAKE_WORKER_STATUS="DONE_WITH_CONCERNS")
check(
    "DONE_WITH_CONCERNS goes on to publish",
    task_loop.run_one(repo, cfg_for(repo), {**issue(14, "Concerned"), "labels": []})
    == "published",
)
repo, log, ev = scenario([], FAKE_WORKER_RC=1)
check(
    "a worker that exits non-zero fails the task",
    task_loop.run_one(repo, cfg_for(repo), {**issue(15, "Crash"), "labels": []})
    == "failed",
)
repo, log, ev = scenario([], FAKE_VERIFY_RC=1)
check("a failing verification fails the task before anything is committed or pushed",
      task_loop.run_one(repo, cfg_for(repo), {**issue(16, "Unverified"), "labels": []}) == "failed"
      and "push" not in log.read_text())  # fmt: skip
check(
    "a bug label makes a fix commit",
    task_loop.commit_message({"title": "Crash on start", "labels": ["bug"]})
    == "fix: Crash on start",
)
check(
    "a commit subject is cut to 50 characters",
    len(task_loop.commit_message({"title": "x" * 80, "labels": []})) == 50,
)

# --- the codex rule ------------------------------------------------------------
repo, log, ev = scenario([])
final = task_loop.run_one(
    repo, cfg_for(repo, runtime="codex"), {**issue(21, "Codex one"), "labels": []}
)
st = task_loop.load(repo, 21)
check(
    "codex is blocked when main has no .codex/hooks.json",
    final == "blocked" and "is missing" in st["reason"],
    st["reason"],
)
check("no codex worker was started", "codex" not in log.read_text())
repo, log, ev = scenario([], codex_hooks=True)
(repo / ".codex" / "hooks.json").write_text('{"changed": true}\n')
final = task_loop.run_one(
    repo, cfg_for(repo, runtime="codex"), {**issue(22, "Codex two"), "labels": []}
)
check(
    "codex is blocked when main's hooks differ from the worktree's",
    final == "blocked" and "is stale" in task_loop.load(repo, 22)["reason"],
)
repo, log, ev = scenario([], codex_hooks=True)
final = task_loop.run_one(
    repo, cfg_for(repo, runtime="codex"), {**issue(23, "Codex three"), "labels": []}
)
check(
    "codex runs when main's hooks match the worktree's",
    final == "published" and "codex exec" in log.read_text(),
)

# --- no dangerous flag anywhere ------------------------------------------------
repo, log, ev = scenario([])
for runtime in ("claude", "codex", "gemini"):
    argvs = [
        a
        for _, _, a in task_loop.plan(
            repo, cfg_for(repo, runtime=runtime), {**issue(1, "t"), "labels": []}
        )
    ]
    check(
        f"no --dangerously flag in any {runtime} command",
        not any(str(x).startswith("--dangerously") for a in argvs for x in a),
    )
check("the worker command of each runtime is its headless form",
      [task_loop.plan(repo, cfg_for(repo, runtime=r), {**issue(1, "t"), "labels": []})[2][2][:2]
       for r in ("claude", "codex", "gemini")] == [["claude", "-p"], ["codex", "exec"], ["gemini", "-p"]])  # fmt: skip
try:
    task_loop.sh(["claude", "-p", "--dangerously-skip-permissions", "x"])
    check("sh refuses a --dangerously flag", False)
except ValueError:
    check("sh refuses a --dangerously flag", True)
for bad in ('[loop.workers]\nclaude = "claude -p --dangerously-skip-permissions"\n',
            '[loop]\nruntime = "ghost"\n', '[loop]\nbogus = 1\n', '[loop]\npriority = []\n'):  # fmt: skip
    try:
        harness_config.merge(__import__("tomllib").loads(bad))
        check(f"harness.toml refuses {bad.splitlines()[-1]!r}", False)
    except harness_config.ConfigError:
        check(f"harness.toml refuses {bad.splitlines()[-1]!r}", True)

# --- the loop: stop rules and reset between tasks ------------------------------
three = [issue(1, "one"), issue(2, "two"), issue(3, "three")]
repo, log, ev = scenario(three, FAKE_WORKER_STATUS="BLOCKED")
code, out = quiet(task_loop.run_loop, repo, cfg_for(repo))
states = {n: task_loop.load(repo, n) for n in (1, 2, 3)}
check(
    "the loop stops after 2 consecutive blocked tasks",
    code == 1 and "2 consecutive tasks ended failed or blocked" in out,
    out,
)
check("the third issue was never started", states[3] is None)
check(
    "loop_reset ran after each of the two tasks",
    len(events(ev, "loop_guard", "reset")) == 2,
)
check("the runner recorded each reset", len(events(ev, "task_loop", "reset")) == 2)

repo, log, ev = scenario(three[:2])
code, out = quiet(task_loop.run_loop, repo, cfg_for(repo))
check(
    "two green tasks drain the queue and the loop ends 0",
    code == 0 and "queue empty" in out,
    out,
)
check("both were published and reset ran between them",
      [task_loop.load(repo, n)["state"] for n in (1, 2)] == ["published", "published"]
      and len(events(ev, "loop_guard", "reset")) == 2)  # fmt: skip

repo, log, ev = scenario([issue(1, "one")], FAKE_VERIFY_RC=1)
task_loop.ensure(repo, {"number": 1, "title": "one"})
task_loop.move(repo, 1, "running")
task_loop.move(repo, 1, "failed", "first time")
task_loop.owner_reset(repo, 1, "retry")
code, out = quiet(task_loop.run_loop, repo, cfg_for(repo))
check(
    "the loop stops when one issue has failed twice, though it is only one task in a row",
    code == 1 and "failed 2 times" in out and task_loop.load(repo, 1)["failures"] == 2,
    out,
)

# --- dry runs execute nothing --------------------------------------------------
repo, log, ev = scenario([issue(4, "Dry one", "priority:high")])
before = set(os.listdir(repo / "tmp")) if (repo / "tmp").exists() else set()
for argv in (["run-one", "--dry-run"], ["run", "--dry-run"], ["next", "--dry-run"], ["intake", "--dry-run"],
             ["reset", "--issue", "4", "--reason", "x", "--dry-run"],
             ["file-followup", "--title", "T", "--body", "B", "--dry-run"]):  # fmt: skip
    quiet(task_loop.main, argv, repo)
calls = lines(log)
check(
    "a dry run only reads the issue list",
    all(c.startswith("gh issue list") for c in calls) and calls,
    str(calls),
)
check(
    "a dry run creates no state", list(task_loop.state_dir(repo).glob("*.json")) == []
)
check("a dry run creates no worktree", not (repo / "tmp" / "worktrees").exists())
_, out = quiet(task_loop.main, ["run-one", "--dry-run"], repo)
check("the dry run names the chosen issue", "next: #4 Dry one" in out)
check(
    "the dry run prints the merge command and no --auto",
    "gh pr merge '<pr>' --merge --delete-branch" in out and "--auto" not in out,
)
check(
    "the dry run prints the worker, verify, land and reset stages",
    all(f"  {s}" in out for s in ("worker", "verify", "land", "reset")),
)

# --- intake, status, follow-up -------------------------------------------------
repo, log, ev = scenario([issue(2, "two"), issue(1, "one")])
quiet(task_loop.main, ["intake"], repo)
check(
    "intake queues every labelled issue",
    [task_loop.load(repo, n)["state"] for n in (1, 2)] == ["queued", "queued"],
)
check(
    "intake asked gh for the ready-for-agent label",
    "gh issue list --label ready-for-agent --state open --json number,title,body,labels --limit 100"
    in log.read_text(),
)
_, out = quiet(task_loop.main, ["status", "--json"], repo)
check("status lists both tasks", [s["issue"] for s in json.loads(out)] == [1, 2])
quiet(
    task_loop.main, ["file-followup", "--title", "Follow up", "--body", "Details"], repo
)
check(
    "a follow-up is a gh issue create with the queue label",
    "gh issue create --title Follow up --body Details --label ready-for-agent"
    in log.read_text(),
)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
