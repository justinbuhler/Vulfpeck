"""Check mutation restoration identity and cleanup in disposable Git specimens."""

import difflib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO

RUNNER = REPO / "mutants/runner.py"
SPEC = importlib.util.spec_from_file_location("mutation_runner", RUNNER)
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one independent restoration expectation."""
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'} {name}")
    if not condition and detail:
        print(detail)


def git(repo: Path, *args: str) -> str:
    """Run Git only in the supplied fixture."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=ENV, check=True, capture_output=True, text=True
    ).stdout


def fingerprint() -> str:
    """Read the specimen through the real runner with a clean Git environment."""
    with patch.dict(os.environ, ENV, clear=True):
        return R.tree_digest()


assert hasattr(R, "ROOT") and hasattr(R, "LEDGER")
original_root, original_ledger = R.ROOT, R.LEDGER
with tempfile.TemporaryDirectory() as directory:
    area = Path(directory)
    repo = area / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    specimen = repo / "specimen.txt"
    specimen.write_bytes(b"same bytes\n")
    specimen.chmod(0o644)
    git(repo, "add", "specimen.txt")
    R.ROOT, R.LEDGER = repo, repo / "mutants/mutation_ledger.jsonl"
    try:
        baseline = fingerprint()
        specimen.chmod(0o600)
        check("chmod-only changes alter specimen identity", fingerprint() != baseline)
        specimen.chmod(0o644)
        external = area / "external.txt"
        external.write_bytes(b"same bytes\n")
        specimen.unlink()
        specimen.symlink_to(external)
        linked = fingerprint()
        check(
            "a regular file replaced by a same-byte symlink changes identity",
            linked != baseline,
        )
        external.write_bytes(b"external target changed\n")
        check(
            "external target bytes are not part of symlink identity",
            fingerprint() == linked,
        )
        external.write_bytes(b"same bytes\n")
        other = area / "other.txt"
        other.write_bytes(b"same bytes\n")
        specimen.unlink()
        specimen.symlink_to(other)
        check(
            "same-byte symlinks with different targets have different identities",
            fingerprint() != linked,
        )
        specimen.unlink()
        specimen.symlink_to("missing-one")
        broken = fingerprint()
        specimen.unlink()
        specimen.symlink_to("missing-two")
        check(
            "different broken symlink targets change identity", fingerprint() != broken
        )
        specimen.unlink()
        check(
            "a broken symlink differs from an absent tracked path",
            fingerprint() != broken,
        )
        specimen.write_bytes(b"same bytes\n")
        specimen.chmod(0o644)
        check(
            "independent regular files with restored bytes and mode share identity",
            fingerprint() == baseline,
        )
        literal = os.fsencode(str(external))
        external.write_bytes(literal)
        reference = area / "reference-link"
        reference.symlink_to(external)
        specimen.write_bytes(literal)
        specimen.chmod(stat.S_IMODE(reference.lstat().st_mode))
        regular = fingerprint()
        specimen.unlink()
        specimen.symlink_to(external)
        check(
            "file kind differs even when permissions and serialized bytes agree",
            fingerprint() != regular,
        )
        nested = repo / "nested/deeper"
        nested.mkdir(parents=True)
        nested.chmod(0o755)
        leaf = nested / "victim.txt"
        leaf.write_bytes(b"same bytes\n")
        leaf.chmod(0o644)
        git(repo, "add", "nested/deeper/victim.txt")
        nested_baseline = fingerprint()
        nested.chmod(0o700)
        check(
            "tracked ancestor directory permissions change identity",
            fingerprint() != nested_baseline,
        )
        nested.chmod(0o755)
        check(
            "restored directory permissions restore identity",
            fingerprint() == nested_baseline,
        )
        outside = area / "outside-directory"
        (outside / "deeper").mkdir(parents=True)
        (outside / "deeper/victim.txt").write_bytes(b"same bytes\n")
        (outside / "deeper/victim.txt").chmod(0o644)
        (repo / "nested").rename(repo / ".git/nested-saved")
        (repo / "nested").symlink_to(outside, target_is_directory=True)
        read_bytes = Path.read_bytes
        leaf_reads = []

        def guarded_read(path: Path) -> bytes:
            """Expose a byte read through the unexpected ancestor alias."""
            if path == leaf:
                leaf_reads.append(path)
                raise AssertionError("tracked leaf reached through an ancestor link")
            return read_bytes(path)

        rejected = False
        with patch.object(Path, "read_bytes", guarded_read):
            try:
                fingerprint()
            except SystemExit as exc:
                rejected = "tree not" in str(exc)
            except AssertionError:
                pass
        check(
            "ancestor links are rejected before reading leaf bytes",
            rejected and not leaf_reads,
        )
    finally:
        R.ROOT, R.LEDGER = original_root, original_ledger


def run_specimen(scenario: str, interrupt: int | None = None) -> dict[str, object]:
    """Run the actual CLI against tiny generated patches in a real Git fixture."""
    with tempfile.TemporaryDirectory() as directory:
        area = Path(directory)
        repo = area / "repo"
        (repo / "mutants/patches").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / ".venv/bin").mkdir(parents=True)
        (repo / ".venv/bin/python3").symlink_to(sys.executable)
        shutil.copy2(RUNNER, repo / "mutants/runner.py")
        (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
        target = repo / "target.txt"
        target.write_text("base\n")
        external = area / "external.txt"
        other = area / "other.txt"
        external.write_text("base\n")
        other.write_text("base\n")
        nested = repo / "nested"
        if scenario.startswith("ancestor"):
            nested.mkdir()
            nested.chmod(0o755)
        victim = (
            nested / "victim.txt"
            if scenario.startswith("ancestor")
            else repo / "victim.txt"
        )
        external_directory = area / "external-directory"
        external_directory.mkdir()
        external_leaf = external_directory / "victim.txt"
        external_leaf.write_text("base\n")
        external_leaf.chmod(0o644)
        if scenario == "link-target":
            victim.symlink_to(external)
        elif scenario == "broken-target":
            victim.symlink_to("missing-one")
        else:
            victim.write_text("base\n")
            victim.chmod(0o644)
        probe = f"""import sys, time
from pathlib import Path
scenario = {scenario!r}
phase = Path("target.txt").read_text().strip()
active = phase == "mutant" or (scenario.startswith("baseline") and phase == "base")
if phase == "next":
    Path(".git/next-mutant").write_text("ran")
if active:
    Path(".git/first-mutant").write_text("ran")
    victim = Path("nested/victim.txt" if scenario.startswith("ancestor") else "victim.txt")
    if scenario in ("pass-mode", "fail-mode", "interrupt-mode", "baseline-mode", "baseline-fail-mode", "baseline-interrupt"):
        victim.chmod(0o600)
    elif scenario in ("fail-kind", "link-target", "broken-target"):
        victim.unlink()
        victim.symlink_to("missing-two" if scenario == "broken-target" else {str(other)!r})
    elif scenario == "target-link":
        Path("target.txt").unlink()
        Path("target.txt").symlink_to({str(external)!r})
    elif scenario == "ancestor-link":
        Path("nested").rename(".git/nested-saved")
        Path("nested").symlink_to({str(external_directory)!r}, target_is_directory=True)
    elif scenario in ("ancestor-mode", "ancestor-restored"):
        Path("nested").chmod(0o700)
        if scenario == "ancestor-restored":
            Path("nested").chmod(0o755)
    if scenario.startswith("interrupt") or scenario == "baseline-interrupt":
        Path(".git/ready").write_text("ready")
        deadline = time.monotonic() + 5
        while not Path(".git/release").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    if scenario in ("fail-clean", "fail-mode", "fail-kind", "link-target", "broken-target", "target-link", "baseline-fail-mode", "ancestor-link", "ancestor-mode"):
        print("FAIL fixture test failure")
        print("0 passed, 1 failed")
        sys.exit(1)
print("PASS fixture test")
print("1 passed, 0 failed")
"""
        (repo / "tests/test_probe.py").write_text(probe)
        for identifier, text in (("M101", "mutant\n"), ("M102", "next\n")):
            delta = "".join(
                difflib.unified_diff(
                    ["base\n"], [text], fromfile="a/target.txt", tofile="b/target.txt"
                )
            )
            (repo / f"mutants/patches/{identifier}.patch").write_text(
                f"# mutant_id: {identifier}\n# target: target.txt\n" + delta
            )
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@test",
            "commit",
            "-qm",
            "base",
        )
        ledger = repo / ".git/ledger.jsonl"
        command = [
            sys.executable,
            str(repo / "mutants/runner.py"),
            "--workers",
            "1",
            "--timeout",
            "3",
            "--require-green",
            "--ledger",
            str(ledger),
        ]
        runner_env = ENV.copy()
        if scenario == "apply-failure":
            tools = repo / ".git/tools"
            tools.mkdir()
            real_git = shutil.which("git", path=ENV["PATH"])
            assert real_git is not None
            wrapper = tools / "git"
            wrapper.write_text(f"""#!{sys.executable}
import os, sys
from pathlib import Path
args = sys.argv[1:]
if args and args[0] == "apply" and "-R" not in args and Path(args[-1]).stem == "M101":
    Path("victim.txt").chmod(0o600)
    print("fixture apply failure after metadata change", file=sys.stderr)
    sys.exit(1)
os.execv({real_git!r}, ["git", *args])
""")
            wrapper.chmod(0o755)
            runner_env["PATH"] = str(tools) + os.pathsep + ENV["PATH"]
        if scenario == "cleanup-interrupt":
            driver = repo / ".git/driver.py"
            driver.write_text(f"""import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("fixture_runner", {str(repo / "mutants/runner.py")!r})
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
assert hasattr(runner, "discard_bytecode")
def interrupted_cleanup(target):
    Path("victim.txt").chmod(0o600)
    raise KeyboardInterrupt("fixture bytecode cleanup interrupted")
runner.discard_bytecode = interrupted_cleanup
sys.exit(runner.main())
""")
            command[1] = str(driver)
        reached = True
        if interrupt is None:
            result = subprocess.run(
                command,
                cwd=repo,
                env=runner_env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        else:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=runner_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ready = repo / ".git/ready"
                deadline = time.monotonic() + 8
                while (
                    not ready.exists()
                    and process.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                reached = ready.exists()
                if reached:
                    process.send_signal(interrupt)
                    (repo / ".git/release").write_text("release")
                stdout, stderr = process.communicate(timeout=10)
                code = process.returncode
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        rows = (
            [json.loads(line) for line in ledger.read_text().splitlines()]
            if ledger.exists()
            else []
        )
        return {
            "command": command,
            "cwd": str(repo),
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "rows": rows,
            "target_restored": not target.is_symlink()
            and target.read_text() == "base\n",
            "next_ran": (repo / ".git/next-mutant").exists(),
            "reached": reached,
            "victim_mode": stat.S_IMODE(victim.lstat().st_mode),
            "victim_link": os.readlink(victim) if victim.is_symlink() else None,
            "ancestor_is_link": nested.is_symlink(),
            "external_unchanged": external.read_text() == "base\n"
            and other.read_text() == "base\n"
            and external_leaf.read_text() == "base\n"
            and stat.S_IMODE(external_leaf.stat().st_mode) == 0o644,
        }


for scenario in ("clean", "fail-clean", "ancestor-clean", "ancestor-restored"):
    outcome = run_specimen(scenario)
    verified = [row for row in outcome["rows"] if row.get("tree_verified")]
    check(
        f"{scenario}: successful reversals retain valid results",
        outcome["code"] == 0
        and {row["mutant_id"] for row in verified} == {"M101", "M102"}
        and outcome["target_restored"],
    )

for scenario in (
    "pass-mode",
    "fail-mode",
    "fail-kind",
    "link-target",
    "broken-target",
    "target-link",
    "baseline-mode",
    "baseline-fail-mode",
    "apply-failure",
    "cleanup-interrupt",
    "ancestor-link",
    "ancestor-mode",
):
    outcome = run_specimen(scenario)
    check(
        f"{scenario}: unexpected restoration changes fail loudly",
        outcome["code"] != 0 and "tree not" in outcome["stderr"],
        json.dumps(outcome),
    )
    check(
        f"{scenario}: poisoned specimens receive no verified result",
        not any(row.get("tree_verified") for row in outcome["rows"]),
    )
    check(
        f"{scenario}: no later mutant is attempted",
        not outcome["next_ran"]
        and not any(row.get("mutant_id") == "M102" for row in outcome["rows"]),
    )
    check(
        f"{scenario}: unrelated external targets stay unchanged",
        outcome["external_unchanged"],
    )
    if scenario in (
        "pass-mode",
        "fail-mode",
        "baseline-mode",
        "baseline-fail-mode",
        "apply-failure",
        "cleanup-interrupt",
    ):
        check(
            f"{scenario}: unexpected permissions are reported without force-reset",
            outcome["victim_mode"] == 0o600,
        )
    if scenario == "cleanup-interrupt":
        check(
            "bytecode interruption still reverses the applied patch",
            outcome["target_restored"],
        )
    if scenario == "ancestor-link":
        check(
            "ancestor-link: the unexpected link remains available for diagnosis",
            outcome["ancestor_is_link"],
        )

for scenario, received in (
    ("interrupt-clean", signal.SIGTERM),
    ("interrupt-clean", signal.SIGINT),
    ("interrupt-mode", signal.SIGTERM),
    ("baseline-interrupt", signal.SIGTERM),
):
    outcome = run_specimen(scenario, received)
    label = f"{scenario}/{signal.Signals(received).name}"
    check(f"{label}: interruption reached the active fixture", outcome["reached"])
    check(
        f"{label}: interruption exits without verified results",
        outcome["code"] != 0
        and not any(row.get("tree_verified") for row in outcome["rows"]),
    )
    check(f"{label}: patch reversal still runs", outcome["target_restored"])
    check(f"{label}: no later mutant is attempted", not outcome["next_ran"])
    if scenario != "interrupt-clean":
        check(
            f"{label}: restoration is checked while unwinding",
            "tree not" in outcome["stderr"],
        )

# A patch that no longer applies silently stops testing its behaviour (#32).
stale = [
    patch.name
    for patch in sorted((REPO / "mutants/patches").glob("M*.patch"))
    if subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=REPO, env=ENV, capture_output=True
    ).returncode
]
check("every mutant patch applies to the working tree", not stale, f"stale: {stale}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(bool(FAIL))
