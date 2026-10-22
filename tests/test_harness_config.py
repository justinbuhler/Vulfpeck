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
os.environ["HARNESS_EVENTS_LOG"] = str(Path(tempfile.mkdtemp(prefix="harness-events.")) / "events.jsonl")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


GIT_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX",
           "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
           "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def env(**extra):
    e = {k: v for k, v in os.environ.items() if k not in GIT_ENV and not k.startswith("HARNESS_")}
    e.update(extra)
    return e


def sh(args, cwd, **extra):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                          env=env(**extra), timeout=120)


def fixture(toml=None, scripts=("harness_config.py", "harness_env.sh", "gate.sh", "run.sh")):
    """A git repo holding copies of the named harness scripts and a harness.toml."""
    d = Path(tempfile.mkdtemp(prefix="harness-config."))
    sh(["git", "init", "-q", "-b", "main"], d)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
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
check("no harness.toml: setup is uv sync --frozen", d0["commands"]["setup"] == "uv sync --frozen --quiet")
check("no harness.toml: lint is ruff", d0["commands"]["lint"] == "uvx ruff check .")
check("no harness.toml: tests are the standalone scripts",
      d0["commands"]["test"] == "./scripts/standalone_tests.sh")
check("no harness.toml: the run must report checks total",
      d0["tests"]["count_pattern"] == r"(\d+) checks total")
check("no harness.toml: nothing is copied into the lander", d0["land"]["copy_untracked"] == [])
check("no harness.toml: harness.toml itself is a watched control",
      "harness.toml" in d0["hooks"]["watched_files"])

# --- overrides merge; unknown or mistyped keys are refused -------------------
over = Path(tempfile.mkdtemp(prefix="harness-config-over."))
(over / "harness.toml").write_text('[commands]\ntest = "npm test"\n[tests]\ncount_pattern = ""\n')
o = harness_config.load(over)
check("an override replaces only the key it names",
      o["commands"]["test"] == "npm test" and o["commands"]["lint"] == "uvx ruff check .")
check("count_pattern can be disabled", o["tests"]["count_pattern"] == "")
check("a misspelt key is an error, not ignored",
      "commands.tset" in (config_error('[commands]\ntset = "pytest"\n') or ""))
check("an unknown section is an error", "[gate]" in (config_error('[gate]\nx = "y"\n') or ""))
check("a non-string command is an error",
      "must be a string" in (config_error("[commands]\ntest = 3\n") or ""))
check("copy_untracked refuses a path that climbs out",
      "inside the repository" in (config_error('[land]\ncopy_untracked = ["../secrets"]\n') or ""))
check("copy_untracked refuses an absolute path",
      "inside the repository" in (config_error('[land]\ncopy_untracked = ["/etc"]\n') or ""))
check("a count_pattern with no capture group is an error",
      "capture group" in (config_error('[tests]\ncount_pattern = "passed"\n') or ""))
check("a mapped test rule needs a command",
      "command" in (config_error('[[mapped_tests]]\nmatch = "*.ts"\ntests = ["x"]\n') or ""))
check("malformed TOML is an error naming the file",
      "harness.toml" in (config_error("[commands\n") or ""))
for toml, expect in (
        ('[adr]\ndir = ".claude"\n', "adr.dir '.claude' contains the governed path '.claude/hooks/**'"),
        ('[adr]\ndir = "docs/*"\n', "must be a plain directory, not a glob"),
        ('[adr]\nrequire_for = ["**"]\n', "must start with a fixed directory or file name"),
        ('[adr]\nexempt = ["*/hooks/x.py"]\n', "adr.exempt: '*/hooks/x.py' must be a literal path, not a glob"),
        ('[adr]\nexempt = [".claude/hooks/**"]\n', "must be a literal path, not a glob"),
        ('[adr]\nrequire_for = [""]\n', "must be a relative path inside the repository"),
        ('[adr]\nexempt = ["/etc/x"]\n', "must be a relative path inside the repository")):
    got = config_error(toml) or ""
    check(f"[adr] refuses {toml.splitlines()[1]!r}", expect in got, got)

# --- count: last match wins, zero and absence both fail -----------------------
cfg = harness_config.merge({"tests": {"count_pattern": r"(\d+) passed"}})
check("count takes the last match", harness_config.count(cfg, "1 passed\n9 passed\n") == 9)
check("count is None when nothing matches", harness_config.count(cfg, "ok\n") is None)

# --- --shell output evals to the same values in bash --------------------------
d1 = fixture('[project]\nname = "demo app"\n[land]\ncopy_untracked = ["a b/c", "d"]\n'
             '[commands]\nlint = "echo \'quoted; $HOME\'"\n')
out = sh(["bash", "-c", '. scripts/harness_env.sh && harness_load "$PWD" && '
          'printf "%s|%s|%s|%s\\n" "$HARNESS_NAME" "${#HARNESS_COPY_UNTRACKED[@]}" '
          '"${HARNESS_COPY_UNTRACKED[0]}" "$HARNESS_LINT"'], d1)
check("bash sees names, arrays and quotes exactly as written",
      out.stdout.strip() == "demo app|2|a b/c|echo 'quoted; $HOME'", out.stdout + out.stderr)
bad = fixture("[commands]\ntset = 1\n")
out = sh(["bash", "-c", '. scripts/harness_env.sh && harness_load "$PWD"'], bad)
check("an unreadable harness.toml fails harness_load", out.returncode != 0, out.stdout)
check("and says which key", "tset" in out.stderr, out.stderr)

# --- run.sh: the configured command, its exit status, and its count -----------
def run_sh(test, pattern):
    d = fixture(f'[commands]\nsetup = ""\ntest = {json.dumps(test)}\n'
                f'[tests]\ncount_pattern = {json.dumps(pattern)}\n')
    return sh(["./scripts/run.sh", "--tests"], d)


r = run_sh("echo '3 tests passed'", r"(\d+) tests passed")
check("run.sh passes a test command that reports checks", r.returncode == 0, r.stdout + r.stderr)
check("run.sh shows the test command's output", "3 tests passed" in r.stdout, r.stdout)
r = run_sh("true", r"(\d+) tests passed")
check("run.sh fails a green run that reported no checks", r.returncode != 0, r.stdout)
r = run_sh("echo '0 tests passed'", r"(\d+) tests passed")
check("run.sh fails a run that reported zero checks", r.returncode != 0, r.stdout)
r = run_sh("true", "")
check("with count_pattern empty, exit status alone decides", r.returncode == 0, r.stdout + r.stderr)
r = run_sh("echo '5 tests passed'; exit 5", r"(\d+) tests passed")
check("run.sh propagates a failing test command", r.returncode != 0, r.stdout)
r = run_sh("", "")
check("an empty test command is refused", r.returncode != 0, r.stdout)

# --- gate.sh: setup, lint, suite in order; a failure stops the rest ------------
def gate(lint_status, args=()):
    d = fixture('[commands]\nsetup = "echo setup >> log"\n'
                f'lint = "echo lint >> log; exit {lint_status}"\n'
                'test = "echo test >> log; echo \'1 checks total\'"\n')
    r = sh(["./scripts/gate.sh", *args], d)
    log = (d / "log").read_text().split() if (d / "log").exists() else []
    return r, log


r, log = gate(0)
check("the gate runs setup, lint and tests once each, in order",
      r.returncode == 0 and log == ["setup", "lint", "test"], f"{log} {r.stdout}{r.stderr}")
r, log = gate(7)
check("a lint failure fails the gate with lint's status and skips the suite",
      r.returncode == 7 and log == ["setup", "lint"], f"{r.returncode} {log}")
r, log = gate(0, ["--lint-only"])
check("--lint-only runs lint and nothing else", r.returncode == 0 and log == ["lint"], str(log))

# --- the mapped-test hook follows [[mapped_tests]] ----------------------------
HOOK = REPO / ".claude" / "hooks" / "test_for_edited_module.py"


def hook(root, edited):
    payload = json.dumps({"tool_input": {"file_path": str(root / edited)}, "cwd": str(root)})
    return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                          text=True, env={**env(), "CLAUDE_PROJECT_DIR": str(root)}, timeout=60)


d3 = fixture('[[mapped_tests]]\nmatch = "*.ts"\nself = "*.test.ts"\n'
             'tests = ["{dir}/{stem}.test.ts"]\ncommand = "bash {test}"\n',
             scripts=("harness_config.py",))
(d3 / "src").mkdir()
(d3 / "src" / "app.ts").write_text("export {}\n")
(d3 / "src" / "app.test.ts").write_text("echo ran; exit 0\n")
(d3 / "src" / "lone.ts").write_text("export {}\n")
r = hook(d3, "src/app.ts")
check("a configured rule maps app.ts to app.test.ts and runs it",
      r.returncode == 0 and "app.test.ts" in r.stdout, r.stdout + r.stderr)
(d3 / "src" / "app.test.ts").write_text("echo broken; exit 1\n")
r = hook(d3, "src/app.ts")
check("a failing mapped test exits 2 with its output", r.returncode == 2 and "broken" in r.stderr,
      r.stdout + r.stderr)
r = hook(d3, "src/app.test.ts")
check("editing a test file runs that test", r.returncode == 2 and "broken" in r.stderr, r.stderr)
check("a file with no mapped test is silent", hook(d3, "src/lone.ts").returncode == 0)
check("a file no rule matches is silent", hook(d3, "README.md").returncode == 0)
(d3 / "harness.toml").write_text("[[mapped_tests]]\nmatch = 1\n")
r = hook(d3, "src/app.ts")
check("an unreadable harness.toml is reported, not skipped", r.returncode == 2, r.stdout + r.stderr)

d4 = fixture(None, scripts=("harness_config.py",))
(d4 / "pkg" / "tests").mkdir(parents=True)
(d4 / "pkg" / "mod.py").write_text("x = 1\n")
(d4 / "pkg" / "tests" / "test_mod.py").write_text("raise SystemExit(1)\n")
(d4 / ".venv").mkdir()
(d4 / ".venv" / "mod.py").write_text("x = 1\n")
shutil.copy2(d4 / "pkg" / "tests" / "test_mod.py", d4 / ".venv" / "test_mod.py")
r = hook(d4, "pkg/mod.py")
check("by default pkg/mod.py maps to pkg/tests/test_mod.py", r.returncode == 2 and "test_mod.py" in r.stderr,
      r.stdout + r.stderr)
check("files under a skipped directory are ignored", hook(d4, ".venv/test_mod.py").returncode == 0)

# --- pre-commit lints with harness.toml's command -------------------------------
d5 = fixture('[commands]\nlint = "echo config-lint-ran; exit 3"\n', scripts=("harness_config.py", "harness_env.sh", "gate.sh"))
sh(["git", "config", "core.hooksPath", str(REPO / ".githooks")], d5)
sh(["git", "checkout", "-q", "-b", "work"], d5)
(d5 / "f.txt").write_text("x\n")
sh(["git", "add", "-A"], d5)
r = sh(["git", "commit", "-qm", "x"], d5)
check("pre-commit runs the configured lint and refuses on its failure",
      r.returncode != 0 and "config-lint-ran" in r.stdout + r.stderr, r.stdout + r.stderr)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
