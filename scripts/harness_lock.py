"""Hold repository mutexes by descriptor in the common Git directory."""
import argparse
import fcntl
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path


def git_environment() -> dict[str, str]:
    """Remove repository overrides inherited from Git hooks."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def common_dir(root: Path) -> Path:
    """Resolve the common directory from the explicit execution checkout."""
    done = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                          env=git_environment(), capture_output=True, text=True, check=True)
    return Path(done.stdout.strip()).resolve()


@contextmanager
def repository_lock(root: Path, name: str, *, blocking: bool = True):
    """Hold a stable-inode mutex until its descriptor closes.

    Inherited descriptors are accepted only after checking their inode and
    acquiring the lock on that same open description; an environment flag
    alone cannot bypass locking.
    """
    if not name.replace("-", "").isalnum():
        raise ValueError("invalid mutex name")
    path = common_dir(root) / "harness" / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    inherited = os.environ.get("HARNESS_LOCK_" + name.upper().replace("-", "_"))
    descriptor = None
    if inherited and inherited.isdecimal():
        try:
            candidate = int(inherited)
            held, expected = os.fstat(candidate), path.stat()
            if (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino):
                descriptor = os.dup(candidate)
        except (OSError, ValueError):
            descriptor = None
    with (os.fdopen(descriptor, "a+") if descriptor is not None else path.open("a+")) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield handle


def main() -> int:
    """Execute a command or acquire a shell-owned inherited descriptor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--acquire-fd", type=int)
    parser.add_argument("--check-held", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.check_held:
        value = os.environ.get("HARNESS_LOCK_" + args.name.upper().replace("-", "_"), "")
        try:
            expected = common_dir(args.root) / "harness/locks" / f"{args.name}.lock"
            held, wanted = os.fstat(int(value)), expected.stat()
            if (held.st_dev, held.st_ino) != (wanted.st_dev, wanted.st_ino):
                return 1
            fcntl.flock(int(value), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0
        except (OSError, ValueError):
            return 1
    if args.acquire_fd is not None:
        expected = common_dir(args.root) / "harness/locks" / f"{args.name}.lock"
        held, wanted = os.fstat(args.acquire_fd), expected.stat()
        if (held.st_dev, held.st_ino) != (wanted.st_dev, wanted.st_ino):
            parser.error("descriptor does not name the common repository mutex")
        fcntl.flock(args.acquire_fd, fcntl.LOCK_EX)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    with repository_lock(args.root, args.name) as handle:
        env = dict(os.environ)
        env["HARNESS_LOCK_" + args.name.upper().replace("-", "_")] = str(handle.fileno())
        return subprocess.run(command, env=env, pass_fds=(handle.fileno(),)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
