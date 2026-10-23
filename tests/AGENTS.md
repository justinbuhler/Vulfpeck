# AGENTS.md - tests/

## Purpose
Fixture-driven regression checks for shared infrastructure and the harness.
Folder-specific tests live beside their scripts in `<folder>/scripts/tests/`. No network, no
API keys, no quota consumed.
`./scripts/run.sh --tests` prints the file list and the per-file counts; both
have moved several times in a single day, so neither is written down here.

## Owns
Shared and repository-wide `test_*.py` files and nothing else.

## Writes
Temporary directories only. A test that writes into the repository is a bug -
see the Rules.

## Never here
Production code. Fixtures large enough to need their own folder.

## Rules
- **Not pytest.** Each file is a standalone script with a local
  `check(name, cond, detail)` helper appending to `PASS`/`FAIL`, printing one
  line per check, and exiting non-zero if any failed. Do not add a framework.
- Shared tests reach root modules by putting `scripts/` on the path:
  `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))`
- Folder tests put the repository root on the path and import the qualified
  module, for example `from <folder>.scripts import <module>`.
- **Never manufacture production state the harness exists to test.** A behavioural
  test that supplies a prerequisite production is responsible for supplying cannot
  see production fail to supply it. Execute the real initialisation or preamble and
  vary only the dependency you are controlling. For example, a harness that
  injects its own `failed=0` cannot see a `run.sh` that never initialises the
  flag, which aborts a *clean* run under `set -u`.
- **Assert the seam exists before writing through it.** A test that sets
  `mod.OUT = tmp` on a module that renamed `OUT` silently writes to the real
  artefact. Add `assert hasattr(mod, "OUT")` before the override.
- Every check names the behaviour it protects, not the function it calls.
- Run one file by its declared path. Run all: `./scripts/run.sh --tests`.
- **The check count must not move with the environment.** A test whose branches
  depend on whether a credential happens to be configured stops exercising the
  path it was written for the moment someone sets the variable. Neutralise the
  loader (`mod.load_env = lambda *a, **k: []`) so both paths always run.

See the repository AGENTS.md for the invariants these protect.
