"""Path authority for the repository.

Import repository paths from here; never anchor them to `Path(__file__).parent`
in another module (AGENTS.md invariant 2).
"""
from pathlib import Path
import os
import subprocess

CODE = Path(__file__).resolve().parents[1]
REPO = CODE.parent if CODE.name == ".vulfpeck" else CODE
SCRIPTS = CODE / "scripts"
TESTS = CODE / "tests"
OUTPUT = REPO / "output"


def root_for(cwd: Path | None = None) -> Path:
    """Resolve execution checkout and reject a foreign installed target."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, env=env)
    if CODE.name == ".vulfpeck" and done.returncode:
        raise ValueError("installed harness execution requires its target Git checkout")
    target = Path(done.stdout.strip()).resolve() if done.returncode == 0 else REPO
    if CODE.name == ".vulfpeck":
        def common(path):
            return subprocess.check_output(["git", "-C", str(path), "rev-parse", "--path-format=absolute",
                                            "--git-common-dir"], text=True, env=env).strip()
        if common(target) != common(REPO):
            raise ValueError("installed harness execution is outside its target Git repository")
    return target


if __name__ == "__main__":
    print(root_for())
