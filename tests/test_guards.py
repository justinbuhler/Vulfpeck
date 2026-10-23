"""readonly_guard and queue_guard: mutation by read-only agents is detected, and
task-worktree agents cannot commit directly.

Every case runs the real hook as a subprocess against a throwaway repository in a
temp directory. Expectations are literals (CLAUDE.md Rule 11).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
SCRATCH = Path(tempfile.mkdtemp(prefix="harness-guards."))
os.environ["HARNESS_EVENTS_LOG"] = str(SCRATCH / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV
    )
    if done.returncode:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout.strip()


def repo():
    d = Path(tempfile.mkdtemp(prefix="guard-repo.", dir=SCRATCH))
    git(d, "init", "-q", "-b", "main")
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        git(d, "config", k, v)
    (d / "a.txt").write_text("a\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    return d


def hook(script, payload):
    done = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    try:
        out = json.loads(done.stdout) if done.stdout.strip() else {}
    except ValueError:
        out = {"unparsed": done.stdout}
    return done.returncode, out


def events():
    path = Path(os.environ["HARNESS_EVENTS_LOG"])
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def sub(event, d, agent_type="reviewer-code", agent_id="a1", active=False):
    return hook(
        "readonly_guard.py",
        {
            "hook_event_name": event,
            "agent_type": agent_type,
            "agent_id": agent_id,
            "cwd": str(d),
            "stop_hook_active": active,
        },
    )


def snap(d, agent_id="a1"):
    return (
        Path(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "harness"
        / "snapshots"
        / f"{agent_id}.json"
    )


# --- readonly_guard ----------------------------------------------------------
d = repo()
rc, out = sub("SubagentStart", d)
check("a read-only agent's start takes a snapshot", rc == 0 and snap(d).is_file())
rc, out = sub("SubagentStop", d)
check(
    "an unchanged checkout lets the read-only agent stop",
    rc == 0 and out == {},
    str(out),
)
check("a clean stop removes the snapshot", not snap(d).exists())
check(
    "a clean stop is recorded as pass",
    events()[-1]["decision"] == "pass",
    str(events()[-1:]),
)

sub("SubagentStart", d)
(d / "a.txt").write_text("changed by a reviewer\n")
rc, out = sub("SubagentStop", d)
check(
    "a modified tracked file blocks the stop", out.get("decision") == "block", str(out)
)
check(
    "the block names the changed path",
    "a.txt" in out.get("reason", ""),
    out.get("reason", ""),
)
check("the block is recorded", events()[-1]["decision"] == "block")
rc, out = sub("SubagentStop", d, active=True)
check(
    "a second stop is let through, not looped",
    "decision" not in out and "still differs" in out.get("systemMessage", ""),
    str(out),
)
check(
    "the override is recorded and the snapshot kept as evidence",
    events()[-1]["decision"] == "block-overridden" and snap(d).is_file(),
)
git(d, "checkout", "--", "a.txt")
snap(d).unlink()

for name, mutate, expect in (
    ("an untracked file", lambda r: (r / "new.txt").write_text("x\n"), "new.txt"),
    (
        "a commit on the checked-out branch",
        lambda r: git(r, "commit", "-q", "--allow-empty", "-m", "sneaky"),
        "HEAD",
    ),
    (
        "a moved local main",
        lambda r: git(
            r,
            "update-ref",
            "refs/heads/main",
            git(
                r,
                "commit-tree",
                "-m",
                "x",
                "-p",
                "main",
                git(r, "rev-parse", "HEAD^{tree}"),
            ),
        ),
        "local main",
    ),
):
    r = repo()
    git(r, "checkout", "-q", "-b", "feature")  # so moving main does not move HEAD
    sub("SubagentStart", r, agent_id="m-" + name.replace(" ", "-"))
    mutate(r)
    rc, out = sub("SubagentStop", r, agent_id="m-" + name.replace(" ", "-"))
    check(
        f"{name} blocks the stop and is named",
        out.get("decision") == "block" and expect in out.get("reason", ""),
        str(out),
    )

r = repo()
git(r, "branch", "feat/cand")
sub("SubagentStart", r, agent_id="refs")
git(
    r,
    "update-ref",
    "refs/heads/feat/cand",
    git(r, "commit-tree", "-m", "x", "-p", "HEAD", git(r, "rev-parse", "HEAD^{tree}")),
)
rc, out = sub("SubagentStop", r, agent_id="refs")
check(
    "moving a branch that is checked out nowhere blocks the stop and is named",
    out.get("decision") == "block" and "refs/heads/feat/cand" in out.get("reason", ""),
    str(out),
)
r = repo()
sub("SubagentStart", r, agent_id="conf")
git(r, "config", "core.hooksPath", "/dev/null")
rc, out = sub("SubagentStop", r, agent_id="conf")
check(
    "changing the local git config blocks the stop",
    out.get("decision") == "block"
    and "local git config changed" in out.get("reason", ""),
    str(out),
)
r = repo()
sub("SubagentStart", r, agent_id="tag")
git(r, "tag", "v-sneaky")
rc, out = sub("SubagentStop", r, agent_id="tag")
check(
    "creating a tag blocks the stop",
    "refs/tags/v-sneaky" in out.get("reason", ""),
    str(out),
)

for name, mutate, expect in (
    (
        "an edited info/exclude",
        lambda r: (r / ".git" / "info" / "exclude").write_text("*.secret\n"),
        "git dir changed",
    ),
    (
        "a new file in .git/hooks",
        lambda r: (r / ".git" / "hooks" / "pre-commit").write_text("exit 0\n"),
        "git dir changed",
    ),
    (
        "a new worktree",
        lambda r: git(
            r, "worktree", "add", "-q", "--detach", str(r.parent / (r.name + "-x"))
        ),
        "git dir changed",
    ),
):
    r = repo()
    sub("SubagentStart", r, agent_id="gd")
    mutate(r)
    rc, out = sub("SubagentStop", r, agent_id="gd")
    check(
        f"{name} blocks the stop",
        out.get("decision") == "block" and expect in out.get("reason", ""),
        str(out),
    )

log = Path(os.environ["HARNESS_EVENTS_LOG"])
r = repo()
sub("SubagentStart", r, agent_id="nolog")
log.write_text("not json\n{broken\n")
rc, out = sub("SubagentStop", r, agent_id="nolog")
check("a corrupt decision log does not block a clean stop", out == {}, str(out))
r = repo()
sub("SubagentStart", r, agent_id="nolog2")
log.unlink()
rc, out = sub("SubagentStop", r, agent_id="nolog2")
check("a missing decision log does not block a clean stop", out == {}, str(out))
r = repo()
sub("SubagentStart", r, agent_id="nolog3")
log.unlink()
(r / "a.txt").write_text("changed\n")
rc, out = sub("SubagentStop", r, agent_id="nolog3")
check(
    "a changed checkout still blocks the stop with no decision log",
    out.get("decision") == "block" and "working tree: a.txt" in out.get("reason", ""),
    str(out),
)
r = repo()
sub("SubagentStart", r, agent_id="twice")
for _ in range(2):
    sub("SubagentStart", repo(), agent_id="other-" + str(_))
rc, out = sub("SubagentStop", r, agent_id="twice")
check("several snapshot rows in the log do not block a clean stop", out == {}, str(out))

r = repo()
for _ in range(2):
    sub("SubagentStart", r, agent_id="resumed")
    rc, out = sub("SubagentStop", r, agent_id="resumed")
check(
    "an agent resumed after a clean stop is checked again, not refused as tampering",
    out == {},
    str(out),
)

r = repo()
sub("SubagentStart", r, agent_id="optout")
(r / "harness.toml").write_text('[roles]\nread_only = ["nobody"]\n')
rc, out = sub("SubagentStop", r, agent_id="optout")
check(
    "an agent that edits the roles mid-run is still checked against its start snapshot",
    out.get("decision") == "block" and "harness.toml" in out.get("reason", ""),
    str(out),
)

r = repo()
sub("SubagentStart", r, agent_type="reviewer-code", agent_id="erased")
(r / "harness.toml").write_text('[roles]\nread_only = ["nobody"]\n')
snap(r, "erased").unlink()
rc, out = sub("SubagentStop", r, agent_type="reviewer-code", agent_id="erased")
check(
    "a default read-only type that erased its snapshot and its role is still reported unverified",
    "NOT verified" in out.get("systemMessage", ""),
    str(out),
)

r = repo()
writer = r.parent / (r.name + "-writer")
git(r, "worktree", "add", "-q", "-b", "task", str(writer))
sub("SubagentStart", r, agent_id="w1")
(writer / "w.txt").write_text("writer's work\n")
git(writer, "add", "w.txt")
git(writer, "commit", "-qm", "writer commit")
rc, out = sub("SubagentStop", r, agent_id="w1")
check(
    "a writer committing in its own worktree does not block the stop",
    "decision" not in out,
    str(out),
)
check(
    "but its branch's advance is reported, not silent",
    "branches checked out in other worktrees advanced: refs/heads/task"
    in out.get("systemMessage", ""),
    str(out),
)
sub("SubagentStart", r, agent_id="rewind")
git(writer, "update-ref", "refs/heads/task", git(r, "rev-parse", "main"))
rc, out = sub("SubagentStop", r, agent_id="rewind")
check(
    "moving another worktree's branch anywhere but forward blocks the stop",
    out.get("decision") == "block" and "refs/heads/task" in out.get("reason", ""),
    str(out),
)

r = repo()
rc, out = sub("SubagentStart", r, agent_type="python-general-engineer")
check("a writer agent type takes no snapshot", not snap(r).exists())
rc, out = sub("SubagentStop", r, agent_type="reviewer-code", agent_id="never-started")
check(
    "a read-only stop with no snapshot is reported as unverified",
    "NOT verified" in out.get("systemMessage", "")
    and events()[-1]["decision"] == "unverified",
    str(out),
)

r = repo()
(r / "harness.toml").write_text('[roles]\nread_only = ["custom-*"]\n')
git(r, "add", "harness.toml")
git(r, "commit", "-qm", "roles")
sub("SubagentStart", r, agent_type="custom-auditor", agent_id="c1")
check("harness.toml roles add a read-only type", snap(r, "c1").is_file())
sub("SubagentStart", r, agent_type="reviewer-code", agent_id="c2")
check("harness.toml roles replace the default list", not snap(r, "c2").exists())
(r / "harness.toml").write_text("[roles\n")
sub("SubagentStart", r, agent_type="reviewer-code", agent_id="c3")
check(
    "an unreadable harness.toml falls back to the default read-only list",
    snap(r, "c3").is_file(),
)

# --- queue_guard ---------------------------------------------------------------
q = repo()
task_wt = q.parent / (q.name + "-task")
git(q, "worktree", "add", "-q", "-b", "agent/t", str(task_wt))
tasks = (
    Path(git(q, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    / "agent-harness"
    / "tasks"
)
tasks.mkdir(parents=True)
(tasks / "t.json").write_text(json.dumps({"task": "t", "worktree": str(task_wt)}))
(task_wt / "sub").mkdir()


def bash(command, cwd):
    return hook(
        "queue_guard.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
        },
    )


def is_deny(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


for command in (
    "git commit -m x",
    "git add a && git commit -qm x",
    "FOO=1 git commit-tree abc",
    "git -c user.name=x commit -m y",
    "git update-ref refs/heads/agent/t HEAD",
):
    rc, out = bash(command, task_wt)
    check(
        f"in a task worktree, `{command}` is denied", rc == 0 and is_deny(out), str(out)
    )
rc, out = bash(f"git -C {task_wt} commit -m x", q)
check(
    "`git -C <task worktree> commit` from elsewhere is denied", is_deny(out), str(out)
)
rc, out = bash(f"git -C {q.parent} -C {task_wt.name} commit -m x", q)
check("chained `-C` arguments compose the way git applies them", is_deny(out), str(out))
rc, out = bash(f"git -C {q} commit -m x", task_wt)
check(
    "a commit run from inside a task worktree is denied whatever `-C` says",
    is_deny(out),
    str(out),
)
rc, out = bash(f"git --git-dir={task_wt}/.git --work-tree={task_wt} commit -m x", q)
check(
    "`--git-dir`/`--work-tree` naming a task worktree is denied", is_deny(out), str(out)
)
rc, out = bash("/usr/bin/git commit -m x", task_wt)
check("git invoked by full path is recognised", is_deny(out), str(out))
rc, out = bash("git commit -m x", task_wt / "sub")
check("a subdirectory of a task worktree counts as inside it", is_deny(out))
check(
    "the denial names the enqueue path",
    "enqueue" in out.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""),
)
check(
    "a denial is recorded",
    events()[-1]["hook"] == "queue_guard" and events()[-1]["decision"] == "deny",
)
for command in (
    "git status",
    "git commit --dry-run -m x",
    "echo git commit",
    "git log --grep commit",
):
    rc, out = bash(command, task_wt)
    check(f"in a task worktree, `{command}` passes", rc == 0 and out == {}, str(out))
rc, out = bash("git commit -m x", q)
check("a commit in the main checkout is not this hook's business", out == {}, str(out))
other = repo()
rc, out = bash("git commit -m x", other)
check("a repository with no task manifests passes", out == {}, str(out))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
