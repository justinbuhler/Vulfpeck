"""Path authority for the repository.

Import repository paths from here; never anchor them to `Path(__file__).parent`
in another module (AGENTS.md invariant 2).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
TESTS = REPO / "tests"
OUTPUT = REPO / "output"
