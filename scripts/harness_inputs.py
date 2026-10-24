"""Copy only declared nonsecret inputs into isolated repository specimens."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from harness_config import load

DENIED = {
    "tmp",
    "worktrees",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".codegraph",
    ".claude-flow",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "credentials",
    "auth.json",
    ".ssh",
    ".aws",
    ".azure",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "settings.local.json",
}


def digest(path: Path) -> str:
    """Hash one regular input file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_input(relative: str) -> bool:
    """Reject credential, scratch, cache, absolute, and traversal paths."""
    parts = PurePosixPath(relative).parts
    return (
        bool(parts)
        and not relative.startswith("/")
        and all(
            part.lower() not in DENIED
            and part not in ("..", ".")
            and not part.lower().startswith(".env")
            and not part.lower().endswith((".pem", ".key", ".har"))
            for part in parts
        )
    )


def copy_inputs(source: Path, target: Path, declared: list[str]) -> list[dict]:
    """Validate the entire allowlist before copying distinct, hashed files.

    Tracked destinations belong to the candidate and are never overwritten.
    Symlinks and nonregular inputs are refused before reading their contents.
    """
    source, target = source.resolve(), target.resolve()
    tracked = set(
        subprocess.run(
            ["git", "-C", str(target), "ls-files", "-z"],
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    ) - {""}
    tracked_ids = set()
    for name in tracked:
        tracked_path = target / name
        if tracked_path.exists():
            state = tracked_path.lstat()
            tracked_ids.add((state.st_dev, state.st_ino))
    files = {}
    for relative in declared:
        if not safe_input(relative):
            raise ValueError(f"unsafe provision input: {relative}")
        original = source / relative
        if (
            not original.resolve().is_relative_to(source)
            or any(
                (source / Path(*Path(relative).parts[:n])).is_symlink()
                for n in range(1, len(Path(relative).parts) + 1)
            )
            or not original.exists()
        ):
            raise ValueError(f"missing or linked provision input: {relative}")
        if target.is_relative_to(
            original.resolve()
        ) or original.resolve().is_relative_to(target):
            raise ValueError(f"input and destination ancestry overlap: {relative}")
        candidates = sorted(original.rglob("*")) if original.is_dir() else [original]
        for path in candidates:
            rel = path.relative_to(source).as_posix()
            if not safe_input(rel) or path.is_symlink():
                raise ValueError(f"unsafe provision input: {rel}")
            if path.is_dir():
                if (path / ".git").exists():
                    raise ValueError(f"nested checkout input: {rel}")
                continue
            if not path.is_file():
                raise ValueError(f"nonregular provision input: {rel}")
            destination = target / rel
            linked = any(
                (target / Path(*Path(rel).parts[:n])).is_symlink()
                for n in range(1, len(Path(rel).parts) + 1)
            )
            state = destination.lstat() if destination.exists() else None
            if rel in tracked or (
                state is not None
                and not linked
                and state.st_nlink == 1
                and (state.st_dev, state.st_ino) in tracked_ids
            ):
                print(f"tracked input excluded: {rel}", file=sys.stderr)
                continue
            if (
                not destination.resolve().is_relative_to(target)
                or linked
                or state is not None
                and state.st_nlink > 1
            ):
                raise ValueError(f"linked input destination: {rel}")
            files[rel] = path
    manifest = []
    for rel, path in sorted(files.items()):
        destination = target / rel
        before = digest(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        if digest(destination) != before or digest(path) != before:
            raise ValueError(f"input changed during copy: {rel}")
        manifest.append({"path": rel, "sha256": before, "source": str(path)})
    return manifest


def main() -> int:
    """Provision a land specimen from the project's explicit input list."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = copy_inputs(
            args.source, args.target, load(args.config_root)["land"]["copy_untracked"]
        )
        print(json.dumps(result))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"inputs: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
