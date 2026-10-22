"""Prove descriptor ownership and release using competing real processes."""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
PASS, FAIL = [], []


def check(name, condition):
    """Record one lock result."""
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'} {name}")


check("descriptor lock exists", importlib.util.find_spec("harness_lock") is not None)
if FAIL:
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1)
import harness_lock as locks

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    with locks.repository_lock(root, "probe") as handle:
        code = "from pathlib import Path; from harness_lock import repository_lock; " \
               f"c=repository_lock(Path({str(root)!r}), 'probe', blocking=False); c.__enter__()"
        env = {**os.environ, "PYTHONPATH": str(Path(locks.__file__).parent)}
        contender = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        check("second process cannot steal empty-metadata live descriptor", contender.returncode != 0)
        check("owner descriptor remains open", not handle.closed)
    contender = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    check("closing descriptor releases without unlinking lock", contender.returncode == 0)
    check("lock inode remains stable", (locks.common_dir(root) / "harness/locks/probe.lock").is_file())
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "fixture", "--allow-empty"], check=True)
    linked = root / "linked"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(linked)], check=True)
    with locks.repository_lock(root, "verification"):
        code = "from pathlib import Path; from harness_lock import repository_lock; " \
               f"repository_lock(Path({str(linked)!r}), 'verification', blocking=False).__enter__()"
        contender = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        check("linked checkout shares verification ownership", contender.returncode != 0)
        spoof = subprocess.run([sys.executable, str(Path(locks.__file__)), "--root", str(linked),
                                "--name", "verification", "--check-held"],
                               env={**env, "HARNESS_LOCK_VERIFICATION": "1"}, capture_output=True)
        check("environment string cannot waive verification mutex", spoof.returncode != 0)
    code = "from pathlib import Path; from harness_lock import repository_lock; " \
           f"h=repository_lock(Path({str(root)!r}), 'probe'); h.__enter__(); " \
           "print('ready', flush=True); input()"
    with subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as child:
        check("crash fixture really holds descriptor", child.stdout.readline().strip() == "ready")
        child.kill(); child.wait()
    with locks.repository_lock(root, "probe", blocking=False):
        check("OS releases killed holder without PID reaping race", True)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(bool(FAIL))
