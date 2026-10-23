"""Exercise allowlisted copies against real Git source and destination trees."""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
PASS, FAIL = [], []


def check(name, condition):
    """Record one input-policy result."""
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'} {name}")


check("explicit input copier exists", importlib.util.find_spec("harness_inputs") is not None)
if FAIL:
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1)
import harness_inputs as inputs

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    source.mkdir(); target.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (source / "data").mkdir()
    (source / "data" / "one.json").write_text("{}\n")
    check("empty allowlist copies nothing", inputs.copy_inputs(source, target, []) == [])
    manifest = inputs.copy_inputs(source, target, ["data"])
    check("listed input is copied and hashed", (target / "data/one.json").read_text() == "{}\n"
          and manifest[0]["sha256"] == inputs.digest(target / "data/one.json"))
    check("copy owns separate inode", (source / "data/one.json").stat().st_ino != (target / "data/one.json").stat().st_ino)
    for name in (".env.local", "config/.env", "private/service.key", "tmp/worktrees/task"):
        check(f"credential/scratch path rejected: {name}", not inputs.safe_input(name))
    (source / "data/.env.local").write_text("fixture secret")
    try:
        inputs.copy_inputs(source, target, ["data"])
        refused = False
    except ValueError:
        refused = True
    check("listed parent cannot hide a private child", refused)
    (source / "link").symlink_to(source / "data/one.json")
    try:
        inputs.copy_inputs(source, target, ["link"])
        refused = False
    except ValueError:
        refused = True
    check("symlink input is refused", refused)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(bool(FAIL))
