"""Exercise allowlisted copies against real Git source and destination trees."""

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
PASS, FAIL = [], []
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def check(name, condition):
    """Record one input-policy result."""
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'} {name}")


check(
    "explicit input copier exists",
    importlib.util.find_spec("harness_inputs") is not None,
)
if FAIL:
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1)
import harness_inputs as inputs

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    source.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True, env=ENV)
    subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
    (source / "data").mkdir()
    (source / "data" / "one.json").write_text("{}\n")
    check(
        "empty allowlist copies nothing", inputs.copy_inputs(source, target, []) == []
    )
    manifest = inputs.copy_inputs(source, target, ["data"])
    check(
        "listed input is copied and hashed",
        (target / "data/one.json").read_text() == "{}\n"
        and manifest[0]["sha256"] == hashlib.sha256(b"{}\n").hexdigest(),
    )
    check(
        "copy owns separate inode",
        (source / "data/one.json").stat().st_ino
        != (target / "data/one.json").stat().st_ino,
    )
    for name in (
        ".env.local",
        "config/.env",
        "private/service.key",
        "tmp/worktrees/task",
    ):
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

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    (source / "data").mkdir(parents=True)
    (target / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
    (source / "data/tracked.json").write_text("uncommitted main bytes\n")
    (source / "data/untracked.json").write_text("ordinary input\n")
    (target / "data/tracked.json").write_text("staged candidate bytes\n")
    subprocess.run(
        ["git", "-C", str(target), "add", "data/tracked.json"], check=True, env=ENV
    )
    inputs.copy_inputs(source, target, ["data"])
    check(
        "tracked destination preserves candidate bytes",
        (target / "data/tracked.json").read_text() == "staged candidate bytes\n",
    )
    staged = subprocess.run(
        ["git", "-C", str(target), "show", ":data/tracked.json"],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
    )
    check(
        "tracked destination preserves staged candidate bytes",
        staged.stdout == "staged candidate bytes\n",
    )
    check(
        "untracked input beside tracked content is copied",
        (target / "data/untracked.json").read_text() == "ordinary input\n",
    )
    check(
        "mixed-directory copy preserves ordinary source bytes",
        (source / "data/untracked.json").read_text() == "ordinary input\n",
    )

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    (source / "src").mkdir(parents=True)
    (target / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
    tracked = target / "src/value.json"
    tracked.write_text("tracked candidate\n")
    subprocess.run(
        ["git", "-C", str(target), "add", "src/value.json"], check=True, env=ENV
    )
    (source / "src/VALUE.json").write_text("declared input\n")
    (source / "src/ordinary.json").write_text("ordinary input\n")
    destination = target / "src/VALUE.json"
    aliases = destination.exists() and destination.samefile(tracked)
    print(f"case fixture aliases tracked destination: {aliases}")
    manifest = inputs.copy_inputs(source, target, ["src"])
    check(
        "different-case input preserves tracked destination bytes",
        tracked.read_text() == "tracked candidate\n",
    )
    check(
        "different-case input follows actual destination identity",
        destination.read_text()
        == ("tracked candidate\n" if aliases else "declared input\n"),
    )
    expected_paths = (
        {"src/ordinary.json"} if aliases else {"src/ordinary.json", "src/VALUE.json"}
    )
    check(
        "only actual tracked aliases are excluded from copies",
        {item["path"] for item in manifest} == expected_paths,
    )
    check(
        "ordinary input beside a case variant is copied",
        (target / "src/ordinary.json").read_text() == "ordinary input\n",
    )

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    source.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
    victim = source / "protected.json"
    victim.write_text("protected source\n")
    tracked = target / "value.json"
    tracked.symlink_to(victim)
    subprocess.run(["git", "-C", str(target), "add", "value.json"], check=True, env=ENV)
    (source / "VALUE.json").write_text("declared input\n")
    (source / "first.json").write_text("new first input\n")
    (target / "first.json").write_text("existing first input\n")
    aliases = (target / "VALUE.json").is_symlink()
    try:
        inputs.copy_inputs(source, target, ["first.json", "VALUE.json"])
        refused = False
    except ValueError:
        refused = True
    check(
        "tracked symlink case variant follows the actual link boundary",
        refused == aliases,
    )
    check(
        "tracked symlink case variant preserves the protected target",
        victim.read_text() == "protected source\n",
    )
    check(
        "tracked symlink case variant is checked before earlier writes",
        (target / "first.json").read_text()
        == ("existing first input\n" if aliases else "new first input\n"),
    )

with tempfile.TemporaryDirectory() as directory:
    source, target = Path(directory) / "source", Path(directory) / "target"
    source.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
    tool = source / "tool.sh"
    tool.write_text("#!/bin/sh\nprintf 'declared tool runs\\n'\n")
    tool.chmod(0o755)
    inputs.copy_inputs(source, target, ["tool.sh"])
    copied = target / "tool.sh"
    check(
        "declared executable preserves source permissions",
        copied.stat().st_mode & 0o777 == 0o755,
    )
    try:
        result = subprocess.run([str(copied)], capture_output=True, text=True, env=ENV)
        runs = result.returncode == 0 and result.stdout == "declared tool runs\n"
    except PermissionError:
        runs = False
    check("declared executable runs after copying", runs)

for linked in ("ancestor", "hardlink"):
    with tempfile.TemporaryDirectory() as directory:
        source, target = Path(directory) / "source", Path(directory) / "target"
        source.mkdir()
        target.mkdir()
        subprocess.run(["git", "init", "-q", str(target)], check=True, env=ENV)
        (source / "first.json").write_text("new first input\n")
        (target / "first.json").write_text("existing first input\n")
        if linked == "ancestor":
            (source / "alias").mkdir()
            (source / "alias/value.json").write_text("source input\n")
            (target / "src").mkdir()
            victim = target / "src/value.json"
            victim.write_text("tracked candidate\n")
            subprocess.run(
                ["git", "-C", str(target), "add", "src/value.json"], check=True, env=ENV
            )
            (target / "alias").symlink_to("src", target_is_directory=True)
            declared = "alias/value.json"
            expected = "tracked candidate\n"
        else:
            (source / "one.json").write_text("source input\n")
            victim = source / "other.json"
            victim.write_text("unrelated main data\n")
            (target / "one.json").hardlink_to(victim)
            declared = "one.json"
            expected = "unrelated main data\n"
        try:
            inputs.copy_inputs(source, target, ["first.json", declared])
            refused = False
        except ValueError:
            refused = True
        check(f"destination {linked} is refused before copying", refused)
        check(
            f"destination {linked} preserves its original target",
            victim.read_text() == expected,
        )
        check(
            f"destination {linked} prevents earlier allowlist writes",
            (target / "first.json").read_text() == "existing first input\n",
        )
print(f"{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(bool(FAIL))
