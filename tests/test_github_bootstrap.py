"""scripts/github_bootstrap.sh: dry run changes nothing, --apply labels and protects main.

A fake `gh` on PATH records every argv it receives, so nothing here reaches
GitHub. The expected labels are a literal table, not read from the script
(CLAUDE.md Rule 11); they are checked against docs/agents/triage-labels.md,
which is the document agents read.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO, SCRIPTS  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


EXPECTED_LABELS = {"needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix"}

FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [ "$1" = repo ]; then
  [ -n "${FAKE_NO_REPO:-}" ] && exit 1
  echo owner/vulfpeck; exit 0
fi
if [ "$1" = api ]; then
  cat > "$GH_BODY"
  [ -n "${FAKE_REFUSE:-}" ] && { echo "Upgrade to GitHub Pro"; exit 1; }
fi
exit 0
"""


def run(tmp, *args, **env_extra):
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log, body = tmp / "gh.log", tmp / "body.json"
    for f in (log, body):
        f.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FAKE_"))}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", GH_LOG=str(log), GH_BODY=str(body), **env_extra)
    proc = subprocess.run(["bash", str(SCRIPTS / "github_bootstrap.sh"), *args],
                          capture_output=True, text=True, env=env, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls, (body.read_text() if body.exists() else "")


doc = (REPO / "docs" / "agents" / "triage-labels.md").read_text()
doc_labels = set(re.findall(r"^\|\s*[\w-]+\s*\|\s*([\w-]+)\s*\|$", doc, re.M)) - {"---"}
check("the triage-labels doc names exactly the expected labels", doc_labels == EXPECTED_LABELS,
      f"doc: {sorted(doc_labels)}")

with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)

    proc, calls, _ = run(tmp)
    check("dry run exits 0", proc.returncode == 0, proc.stderr)
    check("dry run makes no mutating gh call",
          not any(c.startswith(("label", "api")) for c in calls), str(calls))
    check("dry run says nothing was changed", "dry run" in proc.stdout, proc.stdout)

    proc, calls, body = run(tmp, "--apply")
    check("--apply exits 0", proc.returncode == 0, proc.stderr)
    created = {c.split()[2] for c in calls if c.startswith("label create")}
    check("--apply creates exactly the expected labels", created == EXPECTED_LABELS, str(created))
    check("every label create is idempotent (--force)",
          all("--force" in c for c in calls if c.startswith("label create")), str(calls))
    put = [c for c in calls if c.startswith("api")]
    check("--apply PUTs protection on owner/vulfpeck main",
          put == ["api --method PUT repos/owner/vulfpeck/branches/main/protection --input -"], str(put))
    protection = json.loads(body or "{}")
    check("main requires the tests status check",
          protection.get("required_status_checks", {}).get("contexts") == ["tests"], body)
    check("main refuses force-pushes and deletion",
          protection.get("allow_force_pushes") is False and protection.get("allow_deletions") is False, body)

    proc, calls, _ = run(tmp, "--apply", FAKE_REFUSE="1")
    check("a refused protection call exits non-zero", proc.returncode != 0, proc.stdout)
    check("a refused protection call says why", "Upgrade to GitHub Pro" in proc.stderr, proc.stderr)
    check("a refused protection call never claims main is protected",
          "protected" not in proc.stdout, proc.stdout)

    proc, calls, _ = run(tmp, "--apply", FAKE_NO_REPO="1")
    check("no GitHub repository exits non-zero before any change",
          proc.returncode != 0 and not any(c.startswith(("label", "api")) for c in calls), str(calls))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
