"""Rule 3's freshness gate, including the stale case that must not pass.

CG-02 is the one that matters, and its basis was measured rather than assumed:
`pendingChanges` is a live content comparison between the working tree and the
index - an edited file shows up before the background daemon syncs - so a
stopped daemon cannot mask staleness. The stale case below is a fixture, so it
never depends on stopping a daemon or racing one.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codegraph_freshness as cg

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


def markers(violations):
    """The CG-0N markers a violation list carries, in order."""
    return [v.split()[0] for v in violations]


CLEAN = {"added": 0, "modified": 0, "removed": 0}
FRESH = {
    "initialized": True,
    "lastIndexed": "2026-08-27T12:00:00.000Z",
    "pendingChanges": dict(CLEAN),
    "worktreeMismatch": None,
    "fileCount": 203,
    "projectPath": str(cg.REPO),
}

print("\ncodegraph freshness: the positive case")
check("a fresh index raises nothing", cg.evaluate(FRESH) == [], str(cg.evaluate(FRESH)))

print("\nCG-02 regression: an unsynced tree is stale and must not pass")
for _kind in cg.PENDING_KINDS:
    _stale = cg.evaluate(dict(FRESH, pendingChanges=dict(CLEAN, **{_kind: 1})))
    check(f"CG-02 fires on one {_kind} file", markers(_stale) == ["CG-02"], str(_stale))
    check(
        f"CG-02 names `codegraph sync` as the remedy for a {_kind} file",
        _stale and "codegraph sync" in _stale[0],
        str(_stale),
    )

_three = cg.evaluate(
    dict(FRESH, pendingChanges={"added": 1, "modified": 2, "removed": 3})
)
check(
    "CG-02 reports the total across all three kinds",
    _three and "6 uningested" in _three[0],
    str(_three),
)
check(
    "CG-02 breaks the total down by kind",
    _three and "2 modified" in _three[0],
    str(_three),
)

print("\nCG-02 does not depend on the daemon")
# The premise, measured: pendingChanges is a live tree-vs-index content
# comparison, so it reports staleness whether or not a daemon is running. A
# check that trusted a daemon-maintained counter would pass a stopped daemon.
check(
    "a non-zero pendingChanges is stale regardless of lastIndexed being recent",
    markers(
        cg.evaluate(
            dict(
                FRESH,
                pendingChanges=dict(CLEAN, modified=1),
                lastIndexed="2099-01-01T00:00:00Z",
            )
        )
    )
    == ["CG-02"],
)

print("\nCG-01 / CG-03 / CG-04")
check(
    "CG-01 fires when the index does not exist",
    markers(cg.evaluate({"initialized": False})) == ["CG-01"],
)
check("CG-01 fires on an empty status document", markers(cg.evaluate({})) == ["CG-01"])
check("CG-01 fires on a None status", markers(cg.evaluate(None)) == ["CG-01"])
check(
    "CG-01 short-circuits: an uninitialised index reports one cause, not four",
    len(
        cg.evaluate(
            {
                "initialized": False,
                "pendingChanges": {"modified": 9},
                "worktreeMismatch": "/elsewhere",
            }
        )
    )
    == 1,
)

_mismatch = cg.evaluate(dict(FRESH, worktreeMismatch="/some/other/worktree"))
check(
    "CG-03 fires on a worktree mismatch",
    markers(_mismatch) == ["CG-03"],
    str(_mismatch),
)
check(
    "CG-03 names the worktree it found",
    _mismatch and "/some/other/worktree" in _mismatch[0],
    str(_mismatch),
)

# The case that actually happened, and which worktreeMismatch does not report:
# codegraph resolves upward out of a worktree and answers from a parent
# directory's index. On 2026-09-02 that made this whole gate pass green against a
# 724-file Go/Java/JavaScript project while the repository had no index at all.
_parent = cg.evaluate(dict(FRESH, projectPath="/Users/someone/Projects/Parent"))
check(
    "CG-03 fires when the index describes a different project entirely",
    markers(_parent) == ["CG-03"],
    str(_parent),
)
check(
    "CG-03 names both the indexed project and the expected one",
    _parent
    and "/Users/someone/Projects/Parent" in _parent[0]
    and str(cg.REPO) in _parent[0],
    str(_parent),
)
check(
    "CG-03 fires independently of the tool's own verdict",
    markers(cg.evaluate(dict(FRESH, worktreeMismatch=None, projectPath="/elsewhere")))
    == ["CG-03"],
)
_nopath = cg.evaluate({k: v for k, v in FRESH.items() if k != "projectPath"})
check(
    "CG-03 fires when the index does not say which project it describes",
    markers(_nopath) == ["CG-03"],
    str(_nopath),
)
check(
    "CG-03 stays silent when projectPath is the repository by another spelling",
    not cg.evaluate(dict(FRESH, projectPath=str(cg.REPO) + "/./")),
)

for _missing in (None, "", 0):
    _v = cg.evaluate(dict(FRESH, lastIndexed=_missing))
    check(
        f"CG-04 fires when lastIndexed is {_missing!r}",
        markers(_v) == ["CG-04"],
        str(_v),
    )

print("\nviolations accumulate rather than reporting only the first")
_many = dict(
    FRESH,
    pendingChanges=dict(CLEAN, added=1),
    worktreeMismatch="/elsewhere",
    lastIndexed=None,
)
check(
    "a status wrong three ways reports three markers",
    markers(cg.evaluate(_many)) == ["CG-02", "CG-03", "CG-04"],
    str(cg.evaluate(_many)),
)

print("\nevery declared marker is reachable")
_seen = set()
for _s in (
    {"initialized": False},
    dict(FRESH, pendingChanges=dict(CLEAN, modified=1)),
    dict(FRESH, worktreeMismatch="/elsewhere"),
    dict(FRESH, lastIndexed=None),
):
    _seen.update(markers(cg.evaluate(_s)))
check(
    f"all {len(cg.MARKERS)} declared markers fire in these fixtures",
    _seen == set(cg.MARKERS),
    f"unreached: {sorted(set(cg.MARKERS) - _seen)}",
)

print("\na slow codegraph is reported, not silently killed by the hook")
# The UserPromptSubmit hook gives this script 30s and throws its output away on
# overrun. These expectations are written out here, not read from the script.
import contextlib, io, json as _json, subprocess as _sp

_hook = [
    h
    for e in _json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"][
        "UserPromptSubmit"
    ]
    for h in e["hooks"]
    if "codegraph_freshness.py" in h["command"]
]
check("the freshness hook is registered once", len(_hook) == 1, str(_hook))
check("the hook budget is 30s", _hook and _hook[0].get("timeout") == 30, str(_hook))
check(
    "the status call gives up at 8s, inside the 30s hook budget",
    cg.STATUS_TIMEOUT_S == 8,
    str(cg.STATUS_TIMEOUT_S),
)
_real_run = cg.subprocess.run


def _slow(*a, **k):
    check("the status call passes a timeout", k.get("timeout") == 8, str(k))
    raise _sp.TimeoutExpired(a[0], k.get("timeout"))


cg.subprocess.run = _slow
try:
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _rc = cg.main()
finally:
    cg.subprocess.run = _real_run
check("a timeout exits 0", _rc == 0, str(_rc))
check(
    "a timeout is named, not reported as absence",
    _buf.getvalue().strip().splitlines()[-1]
    == "codegraph status timed out after 8s - freshness not asserted",
    repr(_buf.getvalue()),
)


def _absent(*a, **k):
    raise FileNotFoundError("codegraph")


cg.subprocess.run = _absent
try:
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        cg.main()
finally:
    cg.subprocess.run = _real_run
check(
    "an absent codegraph keeps its old message",
    _buf.getvalue().strip()
    == "codegraph unavailable or not initialised - freshness not asserted",
    repr(_buf.getvalue()),
)
check(
    "read_status still returns None on timeout",
    (lambda: (setattr(cg.subprocess, "run", _slow), cg.read_status(ROOT))[1])() is None,
)
cg.subprocess.run = _real_run

print("\nthe live index is actually fresh")
# This is the check that makes rule 3 real, and the first draft of it was a
# tautology: it asserted the script's exit status agreed with evaluate() on the
# same status document, so a stale index passed 23/23 while the gate script
# exited 1. A control that compares a producer with itself agrees with itself -
# the same failure 66a48ec spent twelve mutations on. It now asserts freshness.
_live = cg.read_status(ROOT)
# `initialized: false` alone cannot separate two different states: a worktree
# that was never indexed, where rule 3 does not apply, and a `.codegraph/` that
# exists but is half-written, in a repository that is supposed to have a good
# one. Skipping the first is what 9e827715 fixed and is kept. Skipping the
# second would retire the only live assertion that notices a corrupt index,
# while rule 3 tells everyone to trust codegraph over text search.
if _live is None or (
    not _live.get("initialized") and not (ROOT / ".codegraph").is_dir()
):
    # Rule 3 is conditional - "WHEN `.codegraph/` exists" - so a tree with no
    # index is one the rule does not reach, not a stale one. Two shapes mean the
    # same thing: codegraph absent returns None, and codegraph present against an
    # unindexed tree returns a document with initialized false. A provisioned
    # gate worktree is the second, and CG-01 there asserted a freshness failure
    # about an index that was never supposed to exist.
    #
    # CG-01 keeps its meaning for a tree that SHOULD have an index; evaluate() is
    # untouched and its four fixtures above still pin every marker.
    check(
        "codegraph not initialised here - live freshness not asserted, fixtures still hold",
        True,
    )
else:
    check(
        "the live status document carries the keys evaluate() reads",
        {"initialized", "pendingChanges"} <= set(_live),
        str(sorted(_live)),
    )
    _violations = cg.evaluate(_live)
    check(
        "the live codegraph index is fresh",
        _violations == [],
        "\n          ".join(_violations) + "\n          run `codegraph sync`",
    )

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
