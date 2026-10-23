"""harness_install.py is a dry run unless told otherwise, and writes trust only on request.

Everything runs against a throwaway repository and a throwaway home directory;
this checkout and the real ~/.codex, ~/.gemini and ~/.claude are never touched.
Expectations are literals written here (CLAUDE.md Rule 11).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import harness_doctor  # noqa: E402
import harness_install  # noqa: E402

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
ENV = {k: v for k, v in os.environ.items() if k not in GIT_ENV and k != "CODEX_HOME"}

with tempfile.TemporaryDirectory() as raw:
    base = Path(raw).resolve()  # /var is a symlink to /private/var on macOS
    repo, home = base / "repo", base / "home"
    (repo / "scripts").mkdir(parents=True)
    home.mkdir()
    for name in (
        "paths.py",
        "harness_config.py",
        "render_hooks.py",
        "hook_adapter.py",
        "harness_doctor.py",
        "harness_install.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts")
    shutil.copy2(ROOT / "harness.toml", repo)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.11"\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, env=ENV, check=True)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        'model = "x"\n\n[projects."/other"]\ntrust_level = "trusted"\n'
    )
    (home / ".gemini").mkdir()
    (home / ".gemini" / "trustedFolders.json").write_text(
        json.dumps({"/other": "TRUST_FOLDER"})
    )
    codex_before = (home / ".codex" / "config.toml").read_text()
    gemini_before = (home / ".gemini" / "trustedFolders.json").read_text()
    installer = repo / "scripts" / "harness_install.py"

    def install(*flags):
        return subprocess.run(
            [sys.executable, str(installer), "--home", str(home), *flags],
            cwd=repo,
            env=ENV,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def hooks_path():
        return subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=repo,
            env=ENV,
            capture_output=True,
            text=True,
        ).stdout.strip()

    done = install()
    check(
        "a bare run exits 0 and says it is a dry run",
        done.returncode == 0 and "dry run" in done.stdout,
        done.stdout,
    )
    check(
        "a dry run lists the render, hooksPath and uv sync steps",
        all(
            f"would: {s}" in done.stdout
            for s in ("render the hook files", "git config core.hooksPath", "uv sync")
        ),
        done.stdout,
    )
    check(
        "a dry run without --trust does not list trust steps",
        "would: trust" not in done.stdout,
        done.stdout,
    )
    check(
        "a dry run changes nothing",
        hooks_path() == ""
        and not (repo / ".codex" / "hooks.json").exists()
        and not (repo / ".venv").exists()
        and (home / ".codex" / "config.toml").read_text() == codex_before
        and (home / ".gemini" / "trustedFolders.json").read_text() == gemini_before,
    )
    done = install("--trust")
    check(
        "--trust alone lists the trust steps but still writes nothing",
        "would: trust" in done.stdout
        and (home / ".codex" / "config.toml").read_text() == codex_before
        and (home / ".gemini" / "trustedFolders.json").read_text() == gemini_before,
        done.stdout,
    )

    done = install("--apply")
    check(
        "--apply renders the hook files",
        done.returncode == 0
        and (repo / ".codex" / "hooks.json").is_file()
        and (repo / ".gemini" / "settings.json").is_file(),
        done.stdout + done.stderr,
    )
    check("--apply sets core.hooksPath", hooks_path() == ".githooks")
    check("--apply runs uv sync", (repo / ".venv").is_dir())
    check(
        "--apply without --trust leaves Codex and Gemini config alone",
        (home / ".codex" / "config.toml").read_text() == codex_before
        and (home / ".gemini" / "trustedFolders.json").read_text() == gemini_before,
    )

    problems, _ = harness_doctor.runtime_findings(repo, home)
    check(
        "the doctor reports an untrusted folder for Codex and Gemini",
        any(p.startswith("codex does not trust") for p in problems)
        and any(p.startswith("gemini does not trust") for p in problems),
        str(problems),
    )
    done = install("--apply", "--trust")
    check("--apply --trust exits 0", done.returncode == 0, done.stdout + done.stderr)
    codex_after = (home / ".codex" / "config.toml").read_text()
    check(
        "Codex config keeps its earlier content and gains a trusted project table",
        codex_after.startswith(codex_before)
        and f'[projects."{repo.resolve()}"]\ntrust_level = "trusted"\n' in codex_after
        or f'[projects."{repo}"]\ntrust_level = "trusted"\n' in codex_after,
        codex_after,
    )
    rules = json.loads((home / ".gemini" / "trustedFolders.json").read_text())
    check(
        "Gemini rules keep the earlier rule and gain TRUST_FOLDER for the repo",
        rules.get("/other") == "TRUST_FOLDER"
        and rules.get(str(repo)) == "TRUST_FOLDER",
        str(rules),
    )
    problems, _ = harness_doctor.runtime_findings(repo, home)
    check(
        "the doctor then finds both folders trusted",
        not any("does not trust" in p for p in problems),
        str(problems),
    )
    install("--apply", "--trust")
    check(
        "a second --apply --trust adds nothing more",
        (home / ".codex" / "config.toml").read_text() == codex_after
        and json.loads((home / ".gemini" / "trustedFolders.json").read_text()) == rules,
    )
    check("the installer never creates ~/.claude", not (home / ".claude").exists())

    # Gemini matches by ancestor: a parent rule covers a child, a longer DO_NOT_TRUST rule wins.
    (home / ".gemini" / "trustedFolders.json").write_text(
        json.dumps({str(base): "TRUST_FOLDER"})
    )
    check(
        "Gemini trust is inherited from an ancestor rule",
        harness_doctor.gemini_trusted(repo, home),
    )
    (home / ".gemini" / "trustedFolders.json").write_text(
        json.dumps({str(base): "TRUST_FOLDER", str(repo): "DO_NOT_TRUST"})
    )
    check(
        "a longer DO_NOT_TRUST rule beats the ancestor",
        not harness_doctor.gemini_trusted(repo, home),
    )
    (home / ".gemini" / "trustedFolders.json").write_text("{not json")
    check(
        "a broken trustedFolders.json is untrusted, not a crash",
        not harness_doctor.gemini_trusted(repo, home),
    )
    try:
        harness_install.trust_gemini(repo, home)
        refused = False
    except SystemExit:
        refused = True
    check(
        "trust_gemini refuses to overwrite a broken trustedFolders.json",
        refused
        and (home / ".gemini" / "trustedFolders.json").read_text() == "{not json",
    )

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
