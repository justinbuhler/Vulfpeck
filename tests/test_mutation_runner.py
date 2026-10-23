"""Run production mutation cloning and verdicts against tiny real candidate repos."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harness_lock import git_environment

PASS, FAIL = [], []


def check(name, condition, detail=""):
    """Record one runner behavior."""
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'} {name}" + (f" {detail}" if not condition else ""))


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], env=git_environment(), text=True).strip()
    git("init", "-q", "-b", "main")
    (root / "tests").mkdir(); (root / "mutants/patches").mkdir(parents=True)
    (root / ".gitignore").write_text("tmp/\n__pycache__/\n")
    (root / "decision.py").write_text("flag = 1\n")
    (root / "tests/test_logic.py").write_text(
        "import sys\nfrom decision import flag\nok = flag > 0\n"
        "print('PASS positive decision' if ok else 'FAIL positive decision')\n"
        "print('1 passed, 0 failed' if ok else '0 passed, 1 failed')\nsys.exit(0 if ok else 1)\n")
    for mutant, before, after in (("M001", "flag = 1", "flag = 0"), ("M002", "flag = 1", "flag = 2"),
                                   ("M003", "absent = 1", "absent = 0")):
        (root / f"mutants/patches/{mutant}.patch").write_text(
            f"# target: decision.py\n--- a/decision.py\n+++ b/decision.py\n@@ -1 +1 @@\n-{before}\n+{after}\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "candidate")
    before = git("status", "--porcelain"), git("show-ref"), (root / "decision.py").read_bytes()
    for name, expected in (("M001", "KILLED"), ("M002", "SURVIVED"), ("M003", "PATCH_ERROR")):
        receipt = root / f"tmp/json/{name}.json"
        done = subprocess.run([sys.executable, str(ROOT / "mutants/runner.py"), "--root", str(root),
               "--workers", "1", "--timeout", "5", "--only", name, "--receipt", str(receipt)],
               env=git_environment(), capture_output=True, text=True)
        proof = json.loads(receipt.read_text())
        check(f"{name} reports {expected}", proof["mutants"].get(name, {}).get("status") == expected, done.stderr)
        check(f"{name} exit reflects release outcome", (done.returncode == 0) == (expected == "KILLED"), done.stderr)
        check(f"{name} binds source and corpus", proof["source_before"] == proof["source_after"] and bool(proof["corpus_digest"]))
    after = git("status", "--porcelain"), git("show-ref"), (root / "decision.py").read_bytes()
    check("mutations leave source bytes index status and refs unchanged", before == after)
    (root / "decision.py").write_text("flag = 0\n")
    git("add", "decision.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "red baseline")
    receipt = root / "tmp/json/red.json"
    done = subprocess.run([sys.executable, str(ROOT / "mutants/runner.py"), "--root", str(root),
           "--workers", "1", "--timeout", "5", "--only", "M001", "--receipt", str(receipt)],
           env=git_environment(), capture_output=True, text=True)
    check("red baseline refuses before applying patches", done.returncode != 0
          and "baseline is not green" in json.loads(receipt.read_text()).get("error", ""), done.stderr)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(bool(FAIL))
