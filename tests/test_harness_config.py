"""harness.toml drives the gate, the suite, the mapped-test hook and pre-commit.

Every expectation here is a literal written in this file (CLAUDE.md Rule 11),
never read back from scripts/harness_config.py's DEFAULTS. Behavioural checks run
the real scripts inside throwaway git repositories in a temp directory; nothing
touches this checkout.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO, SCRIPTS  # noqa: E402
import harness_config  # noqa: E402

# These tests run real hooks, which record decisions (scripts/harness_events.py).
# Point the record at a scratch file so fixtures never enter the real one.
os.environ["HARNESS_EVENTS_LOG"] = str(
    Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl"
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


GIT_ENV = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


def env(**extra):
    e = {
        k: v
        for k, v in os.environ.items()
        if k not in GIT_ENV and not k.startswith("HARNESS_")
    }
    e.update(extra)
    return e


def sh(args, cwd, **extra):
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env(**extra),
        timeout=120,
    )


def fixture(
    toml=None, scripts=("harness_config.py", "harness_env.sh", "gate.sh", "run.sh")
):
    """A git repo holding copies of the named harness scripts and a harness.toml."""
    d = Path(tempfile.mkdtemp(prefix="harness-config."))
    sh(["git", "init", "-q", "-b", "main"], d)
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        sh(["git", "config", k, v], d)
    (d / "scripts").mkdir()
    for name in scripts:
        shutil.copy2(SCRIPTS / name, d / "scripts" / name)
    if toml is not None:
        (d / "harness.toml").write_text(toml)
    return d


def config_error(toml):
    d = Path(tempfile.mkdtemp(prefix="harness-config-err."))
    (d / "harness.toml").write_text(toml)
    try:
        harness_config.load(d)
    except harness_config.ConfigError as exc:
        return str(exc)
    return None


# --- defaults: the harness's own Python layout, spelled out here --------------
empty = Path(tempfile.mkdtemp(prefix="harness-config-none."))
d0 = harness_config.load(empty)
check(
    "no harness.toml: setup is uv sync --frozen",
    d0["commands"]["setup"] == "uv sync --frozen --quiet",
)
check("no harness.toml: lint is ruff", d0["commands"]["lint"] == "uvx ruff check .")
check(
    "no harness.toml: tests are the standalone scripts",
    d0["commands"]["test"] == "./scripts/standalone_tests.sh",
)
check(
    "no harness.toml: the run must report checks total",
    d0["tests"]["count_pattern"] == r"(\d+) checks total",
)
check(
    "no harness.toml: nothing is copied into the lander",
    d0["land"]["copy_untracked"] == [],
)
check(
    "no harness.toml: harness.toml itself is a watched control",
    "harness.toml" in d0["hooks"]["watched_files"],
)

check(
    "the shared input copier is watched by default",
    "scripts/harness_inputs.py" in d0["hooks"]["watched_files"],
)
check(
    "the shipped config watches the shared input copier",
    "scripts/harness_inputs.py" in harness_config.load(REPO)["hooks"]["watched_files"],
)

# --- overrides merge; unknown or mistyped keys are refused -------------------
over = Path(tempfile.mkdtemp(prefix="harness-config-over."))
(over / "harness.toml").write_text(
    '[commands]\ntest = "npm test"\n[tests]\ncount_pattern = ""\n'
)
o = harness_config.load(over)
check(
    "an override replaces only the key it names",
    o["commands"]["test"] == "npm test" and o["commands"]["lint"] == "uvx ruff check .",
)
check("count_pattern can be disabled", o["tests"]["count_pattern"] == "")
check(
    "a misspelt key is an error, not ignored",
    "commands.tset" in (config_error('[commands]\ntset = "pytest"\n') or ""),
)
check(
    "an unknown section is an error",
    "[gate]" in (config_error('[gate]\nx = "y"\n') or ""),
)
check(
    "a non-string command is an error",
    "must be a string" in (config_error("[commands]\ntest = 3\n") or ""),
)
check(
    "copy_untracked refuses a path that climbs out",
    "inside the repository"
    in (config_error('[land]\ncopy_untracked = ["../secrets"]\n') or ""),
)
check(
    "copy_untracked refuses an absolute path",
    "inside the repository"
    in (config_error('[land]\ncopy_untracked = ["/etc"]\n') or ""),
)
check(
    "a count_pattern with no capture group is an error",
    "capture group" in (config_error('[tests]\ncount_pattern = "passed"\n') or ""),
)
check(
    "a mapped test rule needs a command",
    "command"
    in (config_error('[[mapped_tests]]\nmatch = "*.ts"\ntests = ["x"]\n') or ""),
)
check(
    "malformed TOML is an error naming the file",
    "harness.toml" in (config_error("[commands\n") or ""),
)
for toml, expect in (
    (
        '[adr]\ndir = ".claude"\nrequire_for = [".claude/hooks/**"]\n',
        "adr.dir '.claude' contains the governed path '.claude/hooks/**'",
    ),
    ('[adr]\ndir = "docs/*"\n', "must be a plain directory, not a glob"),
    ('[adr]\nrequire_for = ["**"]\n', "must start with a fixed directory or file name"),
    (
        '[adr]\nexempt = ["*/hooks/x.py"]\n',
        "adr.exempt: '*/hooks/x.py' must be a literal path, not a glob",
    ),
    ('[adr]\nexempt = [".claude/hooks/**"]\n', "must be a literal path, not a glob"),
    ('[adr]\nrequire_for = [""]\n', "must be a relative path inside the repository"),
    ('[adr]\nexempt = ["/etc/x"]\n', "must be a relative path inside the repository"),
):
    got = config_error(toml) or ""
    check(f"[adr] refuses {toml.splitlines()[1]!r}", expect in got, got)

# --- [jev] and [tiers] ----------------------------------------------------------
check(
    "no harness.toml: Jev is enabled with an 8 second timeout",
    d0["jev"]["enabled"] is True and d0["jev"]["timeout"] == 8.0,
)
check(
    "no harness.toml: only plain docs are Low, ADRs are Medium",
    d0["tiers"]["low"] == ["README.md", "ENGINEERING_LOG.md", "HARNESS.md", "docs/**"]
    and d0["tiers"]["medium"] == ["docs/adr/**"]
    and d0["tiers"]["critical"] == [],
)
check(
    "no harness.toml: rules and machinery are High",
    {
        "harness.toml",
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/**",
        ".agents/**",
        ".codex/**",
        ".gemini/**",
        ".github/**",
        "scripts/paths.py",
        "scripts/harness_config.py",
        "scripts/jev.py",
        "scripts/plan_tests.py",
        "scripts/standalone_tests.sh",
        "scripts/land.sh",
        "scripts/gate.sh",
        "scripts/run.sh",
        "mutants/**",
        "tests/**",
        "scripts/harness_env.sh",
        "scripts/adr.py",
        "docs/agents/**",
        "docs/mutation-testing.md",
    }
    <= set(d0["tiers"]["high"]),
)
mt = Path(tempfile.mkdtemp(prefix="harness-config-mt.")).resolve()
for rel in (
    "scripts/test_helper.py",
    "scripts/plan.py",
    "tests/test_plan.py",
    "tests/test_other.py",
    "pkg/scripts/tests/test_mod.py",
    "pkg/mod.py",
    "pkg/tests/test_mod.py",
):
    (mt / rel).parent.mkdir(parents=True, exist_ok=True)
    (mt / rel).write_text("x\n")
check(
    "default rule: a script named test_*.py outside tests/ is not its own test",
    harness_config.mapped_test(d0, mt, mt / "scripts/test_helper.py") is None,
)
check(
    "default rule: scripts/plan.py maps to tests/test_plan.py",
    harness_config.mapped_test(d0, mt, mt / "scripts/plan.py")[0]
    == mt / "tests/test_plan.py",
)
check(
    "default rule: a file under tests/ is its own test",
    harness_config.mapped_test(d0, mt, mt / "tests/test_other.py")[0]
    == mt / "tests/test_other.py",
)
check(
    "default rule: a file under a folder's scripts/tests/ is its own test",
    harness_config.mapped_test(d0, mt, mt / "pkg/scripts/tests/test_mod.py")[0]
    == mt / "pkg/scripts/tests/test_mod.py",
)
jv = Path(tempfile.mkdtemp(prefix="harness-config-jev."))
(jv / "harness.toml").write_text(
    '[jev]\nenabled = false\ntimeout = 2\n[tiers]\ncritical = ["scripts/x.py"]\n'
)
j = harness_config.load(jv)
check(
    "[jev] and [tiers] overrides are read",
    j["jev"] == {"enabled": False, "timeout": 2}
    and j["tiers"]["critical"] == ["scripts/x.py"]
    and "docs/**" in j["tiers"]["low"],
)
for toml, expect in (
    ('[jev]\nenabled = "yes"\n', "jev.enabled must be true or false"),
    ("[jev]\ntimeout = 0\n", "jev.timeout must be a number of seconds above 0"),
    ("[jev]\ntimeout = true\n", "jev.timeout must be a number of seconds above 0"),
    ("[jev]\nretries = 3\n", "unknown key jev.retries"),
    ('[tiers]\nextra = ["a"]\n', "unknown key tiers.extra"),
    ('[tiers]\nmedium = ["/etc"]\n', "tiers.medium: '/etc' must be a relative path"),
    ('[tiers]\nhigh = "scripts/x.py"\n', "tiers.high must be a list of strings"),
    ('[tiers]\nhigh = ["../x"]\n', "tiers.high: '../x' must be a relative path"),
):
    got = config_error(toml) or ""
    check(f"{toml.splitlines()[1]!r} is refused", expect in got, got)

# --- [loop] max_usd: the worker's spend cap -----------------------------------
check(
    "loop.max_usd defaults to 10.0 dollars",
    d0["loop"].get("max_usd") == 10.0 and isinstance(d0["loop"].get("max_usd"), float),
    repr(d0["loop"].get("max_usd")),
)
check(
    "the repository's harness.toml sets [loop] max_usd = 10.0",
    __import__("tomllib").loads((REPO / "harness.toml").read_text())["loop"].get("max_usd")
    == 10.0,
)
for toml, want in (
    ("[loop]\nmax_usd = 2.5\n", 2.5),
    ("[loop]\nmax_usd = 3\n", 3),
    ("[loop]\nmax_usd = 0\n", 0),
):
    d = Path(tempfile.mkdtemp(prefix="harness-config-usd."))
    (d / "harness.toml").write_text(toml)
    try:
        got = harness_config.load(d)["loop"]["max_usd"]
    except harness_config.ConfigError as exc:
        got = str(exc)
    check(f"{toml.splitlines()[1]!r} is read as {want}", got == want and not isinstance(got, bool), repr(got))
for toml in (
    "[loop]\nmax_usd = -1\n",
    "[loop]\nmax_usd = -0.01\n",
    "[loop]\nmax_usd = true\n",
    '[loop]\nmax_usd = "5"\n',
    "[loop]\nmax_usd = nan\n",
    "[loop]\nmax_usd = inf\n",
):
    got = config_error(toml) or ""
    check(
        f"{toml.splitlines()[1]!r} is refused",
        "loop.max_usd must be a number >= 0 (0 is off)" in got,
        got,
    )
for command in ("claude -p --max-budget-usd 5", "claude --max-budget-usd=5 -p"):
    got = config_error(f'[loop.workers]\nclaude = "{command}"\n') or ""
    check(
        f"loop.workers.claude = {command!r} is refused: [loop] max_usd is the one place",
        "loop.workers.claude" in got and "[loop] max_usd" in got,
        got,
    )

# --- count: last match wins, zero and absence both fail -----------------------
cfg = harness_config.merge({"tests": {"count_pattern": r"(\d+) passed"}})
check(
    "count takes the last match", harness_config.count(cfg, "1 passed\n9 passed\n") == 9
)
check("count is None when nothing matches", harness_config.count(cfg, "ok\n") is None)

# --- --shell output evals to the same values in bash --------------------------
d1 = fixture(
    '[project]\nname = "demo app"\n[land]\ncopy_untracked = ["a b/c", "d"]\n'
    "[commands]\nlint = \"echo 'quoted; $HOME'\"\n"
)
out = sh(
    [
        "bash",
        "-c",
        '. scripts/harness_env.sh && harness_load "$PWD" && '
        'printf "%s|%s|%s|%s\\n" "$HARNESS_NAME" "${#HARNESS_COPY_UNTRACKED[@]}" '
        '"${HARNESS_COPY_UNTRACKED[0]}" "$HARNESS_LINT"',
    ],
    d1,
)
check(
    "bash sees names, arrays and quotes exactly as written",
    out.stdout.strip() == "demo app|2|a b/c|echo 'quoted; $HOME'",
    out.stdout + out.stderr,
)
bad = fixture("[commands]\ntset = 1\n")
out = sh(["bash", "-c", '. scripts/harness_env.sh && harness_load "$PWD"'], bad)
check("an unreadable harness.toml fails harness_load", out.returncode != 0, out.stdout)
check("and says which key", "tset" in out.stderr, out.stderr)


# --- run.sh: the configured command, its exit status, and its count -----------
def run_sh(test, pattern):
    d = fixture(
        f'[commands]\nsetup = ""\ntest = {json.dumps(test)}\n'
        f"[tests]\ncount_pattern = {json.dumps(pattern)}\n"
    )
    return sh(["./scripts/run.sh", "--tests"], d)


r = run_sh("echo '3 tests passed'", r"(\d+) tests passed")
check(
    "run.sh passes a test command that reports checks",
    r.returncode == 0,
    r.stdout + r.stderr,
)
check("run.sh shows the test command's output", "3 tests passed" in r.stdout, r.stdout)
r = run_sh("true", r"(\d+) tests passed")
check("run.sh fails a green run that reported no checks", r.returncode != 0, r.stdout)
r = run_sh("echo '0 tests passed'", r"(\d+) tests passed")
check("run.sh fails a run that reported zero checks", r.returncode != 0, r.stdout)
r = run_sh("true", "")
check(
    "with count_pattern empty, exit status alone decides",
    r.returncode == 0,
    r.stdout + r.stderr,
)
r = run_sh("echo '5 tests passed'; exit 5", r"(\d+) tests passed")
check("run.sh propagates a failing test command", r.returncode != 0, r.stdout)
r = run_sh("", "")
check("an empty test command is refused", r.returncode != 0, r.stdout)


# --- gate.sh: setup, lint, suite in order; a failure stops the rest ------------
def gate(lint_status, args=()):
    d = fixture(
        '[commands]\nsetup = "echo setup >> log"\n'
        f'lint = "echo lint >> log; exit {lint_status}"\n'
        "test = \"echo test >> log; echo '1 checks total'\"\n"
    )
    r = sh(["./scripts/gate.sh", *args], d)
    log = (d / "log").read_text().split() if (d / "log").exists() else []
    return r, log


r, log = gate(0)
check(
    "the gate runs setup, lint and tests once each, in order",
    r.returncode == 0 and log == ["setup", "lint", "test"],
    f"{log} {r.stdout}{r.stderr}",
)
r, log = gate(7)
check(
    "a lint failure fails the gate with lint's status and skips the suite",
    r.returncode == 7 and log == ["setup", "lint"],
    f"{r.returncode} {log}",
)
r, log = gate(0, ["--lint-only"])
check(
    "--lint-only runs lint and nothing else",
    r.returncode == 0 and log == ["lint"],
    str(log),
)

# --- the mapped-test hook follows [[mapped_tests]] ----------------------------
HOOK = REPO / ".claude" / "hooks" / "test_for_edited_module.py"


def invoke_hook(payload, project, *, process_cwd=None, deadline=None, overrides=None):
    hook_env = env(**(overrides or {}))
    hook_env.pop("CLAUDE_PROJECT_DIR", None)
    if project is not None:
        hook_env["CLAUDE_PROJECT_DIR"] = str(project)
    argv = [sys.executable, str(HOOK)]
    if deadline is not None:
        # Exercise a real timeout through the existing constant in a fresh process.
        code = (
            "import importlib.util,sys; "
            "spec=importlib.util.spec_from_file_location('mapped_hook',sys.argv[1]); "
            "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            "assert hasattr(mod,'TIMEOUT'); mod.TIMEOUT=float(sys.argv[2]); "
            "raise SystemExit(mod.main())"
        )
        argv = [sys.executable, "-c", code, str(HOOK), str(deadline)]
    return subprocess.run(
        argv,
        input=json.dumps(payload),
        capture_output=True,
        cwd=process_cwd,
        text=True,
        env=hook_env,
        timeout=60,
    )


def hook(root, edited):
    return invoke_hook(
        {"tool_input": {"file_path": str(root / edited)}, "cwd": str(root)}, root
    )


d3 = fixture(
    '[[mapped_tests]]\nmatch = "*.ts"\nself = "*.test.ts"\n'
    'tests = ["{dir}/{stem}.test.ts"]\ncommand = "bash {test}"\n',
    scripts=("harness_config.py",),
)
(d3 / "src").mkdir()
(d3 / "src" / "app.ts").write_text("export {}\n")
(d3 / "src" / "app.test.ts").write_text("echo ran; exit 0\n")
(d3 / "src" / "lone.ts").write_text("export {}\n")
r = hook(d3, "src/app.ts")
check(
    "a configured rule maps app.ts to app.test.ts and runs it",
    r.returncode == 0 and "app.test.ts" in r.stdout,
    r.stdout + r.stderr,
)
(d3 / "src" / "app.test.ts").write_text("echo broken; exit 1\n")
r = hook(d3, "src/app.ts")
check(
    "a failing mapped test exits 2 with its output",
    r.returncode == 2 and "broken" in r.stderr,
    r.stdout + r.stderr,
)
for label, payload_cwd, file_path in (
    ("subdirectory with an absolute edit", d3 / "src", str(d3 / "src" / "app.ts")),
    ("subdirectory with a relative edit", d3 / "src", "app.ts"),
    ("root with a relative edit", d3, "src/app.ts"),
):
    r = invoke_hook(
        {"cwd": str(payload_cwd), "tool_input": {"file_path": file_path}},
        d3,
        process_cwd=empty,
    )
    check(
        f"{label} runs the mapped test from a distinct process cwd",
        r.returncode == 2 and "broken" in r.stderr and "app.test.ts" in r.stderr,
        r.stdout + r.stderr,
    )
r = hook(d3, "src/app.test.ts")
check(
    "editing a test file runs that test",
    r.returncode == 2 and "broken" in r.stderr,
    r.stderr,
)
check("a file with no mapped test is silent", hook(d3, "src/lone.ts").returncode == 0)
check("a file no rule matches is silent", hook(d3, "README.md").returncode == 0)
payload = {"cwd": str(d3), "tool_input": {"file_path": str(d3 / "src" / "app.ts")}}
for label, item, project in (
    ("foreign project", payload, d1),
    ("missing project", payload, None),
    ("outside Git", {"cwd": str(empty), "tool_input": {"file_path": "app.ts"}}, d3),
    (
        "outside file",
        {"cwd": str(d3), "tool_input": {"file_path": str(d1 / "app.ts")}},
        d3,
    ),
    ("missing edit path", {"cwd": str(d3), "tool_input": {}}, d3),
):
    r = invoke_hook(item, project, process_cwd=empty)
    check(
        f"{label} is a silent mapped-test no-op",
        r.returncode == 0 and r.stdout == r.stderr == "",
        r.stdout + r.stderr,
    )
sh(["git", "add", "-A"], d3)
sh(["git", "commit", "-qm", "mapped fixture"], d3)
linked = d3.parent / (d3.name + "-linked")
created = sh(["git", "worktree", "add", "-q", "-b", "mapped-side", str(linked)], d3)
r = invoke_hook(
    {"cwd": str(linked / "src"), "tool_input": {"file_path": "app.ts"}},
    d3,
    process_cwd=empty,
)
check(
    "a linked worktree remains trusted for a subdirectory edit",
    created.returncode == 0 and r.returncode == 2 and "broken" in r.stderr,
    created.stderr + r.stdout + r.stderr,
)

# A selected test must report a failure when it cannot finish or be executed.
slow = fixture(
    '[[mapped_tests]]\nmatch = "*.ts"\nself = "*.test.ts"\n'
    'tests = ["{dir}/{stem}.test.ts"]\ncommand = "bash {test}"\n',
    scripts=("harness_config.py",),
)
(slow / "app.ts").write_text("export {}\n")
(slow / "app.test.ts").write_text("printf started > started; exec sleep 2\n")
slow_payload = {"cwd": str(slow), "tool_input": {"file_path": str(slow / "app.ts")}}
r = invoke_hook(slow_payload, slow, deadline=0.2)
check(
    "a selected test that starts and times out exits 2 with a diagnostic",
    r.returncode == 2
    and "TimeoutExpired" in r.stderr
    and (slow / "started").read_text() == "started",
    r.stdout + r.stderr,
)
git_only = slow / "git-only"
git_only.mkdir()
(git_only / "git").symlink_to(shutil.which("git"))
r = invoke_hook(slow_payload, slow, overrides={"PATH": str(git_only)})
check(
    "a selected test whose launcher cannot execute exits 2 with a diagnostic",
    r.returncode == 2 and "FileNotFoundError" in r.stderr and "app.test.ts" in r.stderr,
    r.stdout + r.stderr,
)

(d3 / "harness.toml").write_text("[[mapped_tests]]\nmatch = 1\n")
r = hook(d3, "src/app.ts")
check(
    "an unreadable harness.toml is reported, not skipped",
    r.returncode == 2,
    r.stdout + r.stderr,
)

d4 = fixture(None, scripts=("harness_config.py",))
(d4 / "pkg" / "tests").mkdir(parents=True)
(d4 / "pkg" / "mod.py").write_text("x = 1\n")
(d4 / "pkg" / "tests" / "test_mod.py").write_text("raise SystemExit(1)\n")
(d4 / ".venv").mkdir()
(d4 / ".venv" / "mod.py").write_text("x = 1\n")
shutil.copy2(d4 / "pkg" / "tests" / "test_mod.py", d4 / ".venv" / "test_mod.py")
r = hook(d4, "pkg/mod.py")
check(
    "by default pkg/mod.py maps to pkg/tests/test_mod.py",
    r.returncode == 2 and "test_mod.py" in r.stderr,
    r.stdout + r.stderr,
)
check(
    "files under a skipped directory are ignored",
    hook(d4, ".venv/test_mod.py").returncode == 0,
)

# --- pre-commit lints with harness.toml's command -------------------------------
d5 = fixture(
    '[commands]\nlint = "echo config-lint-ran; exit 3"\n',
    scripts=("harness_config.py", "harness_env.sh", "gate.sh"),
)
sh(["git", "config", "core.hooksPath", str(REPO / ".githooks")], d5)
sh(["git", "checkout", "-q", "-b", "work"], d5)
(d5 / "f.txt").write_text("x\n")
sh(["git", "add", "-A"], d5)
r = sh(["git", "commit", "-qm", "x"], d5)
check(
    "pre-commit runs the configured lint and refuses on its failure",
    r.returncode != 0 and "config-lint-ran" in r.stdout + r.stderr,
    r.stdout + r.stderr,
)

# --- the [[hook]] registration table ----------------------------------------------
GOOD = {
    "name": "a",
    "event": "PreToolUse",
    "kind": "gate",
    "timeout": 5,
    "command": "true",
}


def hook_table(*entries):
    """Return (config or None, error text) for a harness.toml holding these [[hook]] entries."""
    try:
        return harness_config.merge({"hook": list(entries)}), ""
    except harness_config.ConfigError as exc:
        return None, str(exc)


cfg, err = hook_table(GOOD)
check(
    "a complete [[hook]] entry loads, with an empty matcher by default",
    cfg is not None and cfg["hook"] == [{**GOOD, "matcher": ""}],
    err,
)
check("no [[hook]] table means no hooks", harness_config.merge({})["hook"] == [])
for label, entry, want in (
    ("an unknown key", {**GOOD, "colour": "red"}, "unknown key"),
    (
        "a missing command",
        {k: v for k, v in GOOD.items() if k != "command"},
        "missing key",
    ),
    ("an unknown event", {**GOOD, "event": "BeforeTool"}, "must be one of"),
    (
        "a kind that is not gate or advisory",
        {**GOOD, "kind": "block"},
        "gate' or 'advisory",
    ),
    ("a zero timeout", {**GOOD, "timeout": 0}, "positive"),
    ("a string timeout", {**GOOD, "timeout": "5"}, "must be a int"),
    ("a boolean timeout", {**GOOD, "timeout": True}, "must be a int"),
):
    cfg, err = hook_table(entry)
    check(f"[[hook]] with {label} is an error", cfg is None and want in err, err)
cfg, err = hook_table(GOOD, GOOD)
check("a repeated [[hook]] name is an error", cfg is None and "used twice" in err, err)
try:
    harness_config.merge({"hook": {"name": "a"}})
    table_error = False
except harness_config.ConfigError:
    table_error = True
check("[hook] as a plain table (not [[hook]]) is an error", table_error)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
