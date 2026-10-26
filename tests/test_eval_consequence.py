"""scripts/eval_consequence.py: score the consequence planner against labelled cases.

Fixture cases and labels are written into a scratch directory, and every
expectation is a literal worked out by hand in the comments below (Rule 11),
never read back from the script. Jev answers come from a replaced HTTP seam and
are recorded by a `--collect` run, so the Jev path and the sweep then run
offline from those records, as they do in the gate. No network, no key, no quota.

The fixture floors come from the shipped `[tiers]`: README.md is Low, an
unmatched path is Medium, scripts/gate.sh is High, and nothing is Critical.

    id   floor   gold       alice   bob      carol  Jev (tier at p, alters)    final
    c1   Low     Low        Low     Low      Low    Low .90, .1  accepted      Low
    c2   Low     Medium     Low     Medium   Low    Medium .85, .2 accepted    Medium
    c3   Medium  Medium     Medium  Medium   -      High .70, .9 accepted      High
    c4   Medium  Low        Medium  Medium   -      Low .90, .1  accepted      Medium
    c5   High    High       High    High     -      Medium .50, .1 referred    High
    c6   High    Critical   High    High     -      High .65, .5 accepted      High
    c7   High    (none)     Critical High     -      High .80, .5 accepted      High
    c10  Low     Critical   Critical Critical -      (no answer)               Low
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_consequence as ev  # noqa: E402
import jev  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


for seam in ("settings", "_urlopen", "record_dir"):
    assert hasattr(jev, seam), seam
assert hasattr(jev.harness_events, "log_path")
jev.settings = lambda: {"enabled": True, "timeout": 1.0}
SCRATCH = Path(tempfile.mkdtemp(prefix="eval-consequence-test."))
EVENTS = SCRATCH / "events.jsonl"
os.environ["HARNESS_EVENTS_LOG"] = str(EVENTS)
assert jev.harness_events.log_path() == EVENTS
os.environ["TYPESAFE_API_KEY"] = "fake-key-for-tests-0123456789"
os.environ["JEV_DISABLE"] = ""
os.environ["JEV_OFFLINE"] = ""


def fresh_records():
    directory = Path(tempfile.mkdtemp(prefix="rec.", dir=SCRATCH))
    os.environ["JEV_RECORD_DIR"] = str(directory)
    assert jev.record_dir() == directory
    return directory


# --- fixtures ----------------------------------------------------------------------
def case(cid, path, stat, symbol):
    return {
        "id": cid,
        "pr": 0,
        "paths": [path],
        "removed": [],
        "stats": [stat],
        "symbols": [symbol],
        "description": f"desc {cid}",
    }


CASES_ROWS = [
    case("c1", "README.md", "README.md +1 -1", "one"),
    case("c2", "README.md", "README.md +2 -0", "two"),
    case("c3", "scripts/zzz_nothing.py", "scripts/zzz_nothing.py +3 -0", "three"),
    case("c4", "scripts/zzz_nothing.py", "scripts/zzz_nothing.py +4 -0", "four"),
    case("c5", "scripts/gate.sh", "scripts/gate.sh +1 -1", "gate"),
    case("c6", "scripts/gate.sh", "scripts/gate.sh +2 -2", "gate6"),
    case("c7", "scripts/gate.sh", "scripts/gate.sh +3 -3", "gate7"),
    case("c10", "README.md", "README.md +10 -0", "ten"),
]
TABLE = {
    "gold": dict(c1="Low", c2="Medium", c3="Medium", c4="Low", c5="High", c6="Critical", c10="Critical"),
    "alice": dict(c1="Low", c2="Low", c3="Medium", c4="Medium", c5="High", c6="High", c7="Critical", c10="Critical"),
    "bob": dict(c1="Low", c2="Medium", c3="Medium", c4="Medium", c5="High", c6="High", c7="High", c10="Critical"),
    "carol": dict(c1="Low", c2="Low"),
}  # fmt: skip
LABEL_ROWS = [
    {"id": cid, "labeller": who, "tier": tier, "rationale": f"{who} on {cid}"}
    for who, row in TABLE.items()
    for cid, tier in row.items()
]


def write_jsonl(name, rows):
    path = SCRATCH / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


CASES = write_jsonl("cases.jsonl", CASES_ROWS)
LABELS = write_jsonl("labels.jsonl", LABEL_ROWS)


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def probs(low, medium, high, critical):
    return {"Low": low, "Medium": medium, "High": high, "Critical": critical}


JEV = {  # description -> (tier probabilities, alters_values); c10 gets no answer
    "desc c1": (probs(0.9, 0.1, 0.0, 0.0), 0.1),
    "desc c2": (probs(0.05, 0.85, 0.1, 0.0), 0.2),
    "desc c3": (probs(0.0, 0.3, 0.7, 0.0), 0.9),
    "desc c4": (probs(0.9, 0.1, 0.0, 0.0), 0.1),
    "desc c5": (probs(0.2, 0.5, 0.3, 0.0), 0.1),
    "desc c6": (probs(0.0, 0.0, 0.65, 0.35), 0.5),
    "desc c7": (probs(0.0, 0.1, 0.8, 0.1), 0.5),
}
SENT = []


def fake_urlopen(request, timeout):
    SENT.append(request)
    description = json.loads(request.data)["state"]["description"]
    if description not in JEV:
        raise urllib.error.URLError("no route")
    tiers, alters = JEV[description]
    return Reply(
        {
            "answers": {
                "tier": {"probabilities": tiers},
                "alters_values": {"noul": alters},
            }
        }
    )


jev._urlopen = fake_urlopen


def run(*argv, cases=None, labels=None):
    """Run main() in-process; returns (exit code, stdout, stderr). JEV_OFFLINE is restored."""
    saved = os.environ.get("JEV_OFFLINE")
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ev.main(
                [
                    "--cases",
                    str(cases or CASES),
                    "--labels",
                    str(labels or LABELS),
                    *argv,
                ]
            )
    finally:
        if saved is None:
            os.environ.pop("JEV_OFFLINE", None)
        else:
            os.environ["JEV_OFFLINE"] = saved
    return code, out.getvalue(), err.getvalue()


def report(*argv, **kw):
    code, out, _ = run("--json", *argv, **kw)
    assert code == 0, out
    return json.loads(out)


def exits(*argv, **kw):
    """The SystemExit message main() raises, or None."""
    try:
        run(*argv, **kw)
    except SystemExit as exc:
        return str(exc)
    return None


TIER_ORDER = ["Low", "Medium", "High", "Critical"]


def matrix(**rows):
    """A 4x4 matrix with every cell written out: matrix(Low=dict(Low=1)) fills the rest with 0."""
    return {
        gold: {floor: rows.get(gold, {}).get(floor, 0) for floor in TIER_ORDER}
        for gold in TIER_ORDER
    }


# --- no records, offline: the floor-only half of the report ---------------------------
records = fresh_records()
SENT.clear()
code, text, err = run("--gold", "gold")
check(
    "a default run is offline: no request is sent even with a key and a fake endpoint",
    SENT == [] and code == 0,
    str(len(SENT)),
)
rep = report()
check(
    "cases are counted with and without a gold label, the unlabelled by id",
    rep["cases"] == 8
    and rep["with_gold"] == 7
    and rep["without_gold"] == 1
    and rep["without_gold_ids"] == ["c7"],
    str(rep),
)
check(
    "the floor-vs-gold matrix has gold on the rows and the floor in the columns",
    rep["floor_vs_gold"]["matrix"]
    == matrix(
        Low=dict(Low=1, Medium=1),
        Medium=dict(Low=1, Medium=1),
        High=dict(High=1),
        Critical=dict(Low=1, High=1),
    ),
    str(rep["floor_vs_gold"]["matrix"]),
)
check(
    "floor exact matches are 3 of 7 gold-labelled cases",
    rep["floor_vs_gold"]["exact"] == 3 and rep["floor_vs_gold"]["of"] == 7,
)
check(
    "under is gold above floor, by id in natural order (c10 after c6)",
    rep["floor_vs_gold"]["under"] == ["c2", "c6", "c10"],
    str(rep["floor_vs_gold"]["under"]),
)
check(
    "over is floor above gold, by id",
    rep["floor_vs_gold"]["over"] == ["c4"],
    str(rep["floor_vs_gold"]["over"]),
)
check(
    "with no record every case falls back and says it was not recorded, never no",
    rep["jev"]["answered"] == 0
    and rep["jev"]["fallback"] == 8
    and rep["jev"]["fallback_reasons"] == {"not recorded (offline)": 8}
    and rep["jev"]["status"] == {"accepted": 0, "referred": 0},
    str(rep["jev"]),
)
check("no case answered, so no sweep", rep["sweep"] is None)
check(
    "the text says the sweep was skipped rather than leaving it out",
    "threshold sweep: skipped, no case was answered" in text,
)
check(
    "the report states labels are model or human as recorded and infers nothing",
    "Labels are model or human as recorded in the labeller name; this report infers nothing."
    in text
    and rep["note"]
    == "Labels are model or human as recorded in the labeller name; this report infers nothing.",
)

# --- collect: the only mode that sends, and only what has no answer yet --------------
records = fresh_records()
SENT.clear()
code, text, err = run("--collect")
check(
    "--collect sends one request per case",
    code == 0 and len(SENT) == 8,
    str(len(SENT)),
)
by_desc = {
    json.loads(r.data)["state"]["description"]: hashlib.sha256(r.data).hexdigest()
    for r in SENT
}
check(
    "each answered case is recorded under the SHA-256 of its request, the unanswered one is not",
    sorted(os.listdir(records)) == sorted(f"{by_desc[d]}.json" for d in JEV),
    str(sorted(os.listdir(records))),
)
body = json.loads(SENT[4].data)["state"]
check(
    "a case reaches Jev with its paths, stats, symbols and description",
    body["paths"] == ["scripts/gate.sh"]
    and body["stats"] == ["scripts/gate.sh +1 -1"]
    and body["symbols"] == ["gate"]
    and body["description"] == "desc c5",
    str(body),
)
SENT.clear()
run("--collect")
check(
    "a second --collect sends only the case that has no answer",
    len(SENT) == 1 and json.loads(SENT[0].data)["state"]["description"] == "desc c10",
    str(len(SENT)),
)
SENT.clear()
os.environ["JEV_OFFLINE"] = "1"
run("--collect")
os.environ["JEV_OFFLINE"] = ""
check("--collect overrides an inherited JEV_OFFLINE=1", len(SENT) == 1, str(len(SENT)))

# --- offline replay of those records ------------------------------------------------
SENT.clear()
mark = len(EVENTS.read_text().splitlines())
rep = report()
new = [json.loads(line) for line in EVENTS.read_text().splitlines()[mark:]]
check("the replay makes no request", SENT == [], str(len(SENT)))
check(
    "one decision-log line per case, not one per sweep point",
    [e["decision"] for e in new] == ["hit"] * 7 + ["offline-miss"]
    and all(e["hook"] == "jev" for e in new),
    str([e["decision"] for e in new]),
)
check(
    "answered and fallback counts, and the fallback reason",
    rep["jev"]["answered"] == 7
    and rep["jev"]["fallback"] == 1
    and rep["jev"]["fallback_reasons"] == {"not recorded (offline)": 1}
    and rep["jev"]["answered_labelled"] == 6,
    str(rep["jev"]),
)
check(
    "statuses at the shipped thresholds: six accepted, c5 referred",
    rep["jev"]["status"] == {"accepted": 6, "referred": 1},
    str(rep["jev"]["status"]),
)
check(
    "Jev's own tier against gold: 3 of 6 exact, under c5 and c6, over c3",
    rep["jev"]["jev_vs_gold"]
    == {
        "matrix": matrix(
            Low=dict(Low=2),
            Medium=dict(Medium=1, High=1),
            High=dict(Medium=1),
            Critical=dict(High=1),
        ),
        "exact": 3,
        "of": 6,
        "under": ["c5", "c6"],
        "over": ["c3"],
    },
    str(rep["jev"]["jev_vs_gold"]),
)
check(
    "the final tier against gold: 3 of 6 exact, under c6, over c3 and c4",
    rep["jev"]["final_vs_gold"]
    == {
        "matrix": matrix(
            Low=dict(Low=1, Medium=1),
            Medium=dict(Medium=1, High=1),
            High=dict(High=1),
            Critical=dict(High=1),
        ),
        "exact": 3,
        "of": 6,
        "under": ["c6"],
        "over": ["c3", "c4"],
    },
    str(rep["jev"]["final_vs_gold"]),
)
row = {r["id"]: r for r in rep["per_case"]}
check(
    "a case row names gold, floor, Jev's tier, the final tier, status and its record",
    row["c5"]
    == {
        "id": "c5",
        "gold": "High",
        "floor": "High",
        "source": "jev",
        "jev_tier": "Medium",
        "tier": "High",
        "status": "referred",
        "record": by_desc["desc c5"],
    },
    str(row["c5"]),
)
check(
    "an unlabelled case and a fallback case are rows too",
    row["c7"]["gold"] is None
    and row["c7"]["tier"] == "High"
    and row["c10"]["source"] == "fallback"
    and row["c10"]["record"] is None
    and row["c10"]["jev_tier"] is None
    and [r["id"] for r in rep["per_case"]]
    == ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c10"],
)

# --- the sweep: the real acceptance rule at shifted thresholds ---------------------
sweep = rep["sweep"]
by_point = {(s["accept_high"], s["accept_low"], s["alters_max"]): s for s in sweep}
check(
    "the sweep is the grid of shipped thresholds +/- 0.1: 27 points in ascending order",
    len(sweep) == 27
    and [(s["accept_high"], s["accept_low"], s["alters_max"]) for s in sweep]
    == sorted(by_point)
    and sorted({k[0] for k in by_point}) == [0.5, 0.6, 0.7]
    and sorted({k[1] for k in by_point}) == [0.7, 0.8, 0.9]
    and sorted({k[2] for k in by_point}) == [0.25, 0.35, 0.45],
)
check(
    "at the shipped point six are accepted and c5 referred, matching the statuses above",
    by_point[(0.6, 0.8, 0.35)]
    == {
        "accept_high": 0.6,
        "accept_low": 0.8,
        "alters_max": 0.35,
        "shipped": True,
        "accepted": 6,
        "referred": 1,
        "accepted_exact": 2,
        "accepted_under": 1,
        "accepted_over": 2,
    },
    str(by_point[(0.6, 0.8, 0.35)]),
)
check(
    "only the shipped point is marked shipped",
    [k for k, s in by_point.items() if s["shipped"]] == [(0.6, 0.8, 0.35)],
)
check(
    "ACCEPT_HIGH 0.7 refers c6 (High at 0.65) and its under disappears",
    (
        by_point[(0.7, 0.8, 0.35)]["accepted"],
        by_point[(0.7, 0.8, 0.35)]["referred"],
        by_point[(0.7, 0.8, 0.35)]["accepted_under"],
    )
    == (5, 2, 0),
    str(by_point[(0.7, 0.8, 0.35)]),
)
check(
    "ACCEPT_LOW 0.9 refers c2 (Medium at 0.85); Low at exactly 0.9 still clears 0.9",
    (
        by_point[(0.5, 0.9, 0.25)]["accepted"],
        by_point[(0.5, 0.9, 0.25)]["referred"],
        by_point[(0.5, 0.9, 0.25)]["accepted_exact"],
        by_point[(0.5, 0.9, 0.25)]["accepted_under"],
        by_point[(0.5, 0.9, 0.25)]["accepted_over"],
    )
    == (5, 2, 1, 1, 2),
    str(by_point[(0.5, 0.9, 0.25)]),
)
sweep_text = run("--gold", "gold")[1]
check(
    "the text lists the sweep and marks the shipped point",
    "threshold sweep" in sweep_text and "0.60  0.80  0.35  *" in sweep_text,
    sweep_text,
)

# --- agreement between labellers -----------------------------------------------------
check(
    "every labeller pair except gold, in name order, with shared ids",
    [(a["a"], a["b"], a["shared"]) for a in rep["agreement"]]
    == [("alice", "bob", 8), ("alice", "carol", 2), ("bob", "carol", 2)],
    str(rep["agreement"]),
)
pairs = {(a["a"], a["b"]): a for a in rep["agreement"]}
check(
    "alice vs bob: 6 of 8 agree (75.0%), kappa (0.75 - 0.25) / (1 - 0.25) = 0.6667",
    pairs[("alice", "bob")]["agree"] == 6
    and pairs[("alice", "bob")]["percent"] == 75.0
    and pairs[("alice", "bob")]["kappa"] == 0.6667
    and pairs[("alice", "bob")]["disagreements"] == ["c2", "c7"],
    str(pairs[("alice", "bob")]),
)
check(
    "alice vs carol: full agreement on one tier has no kappa, not 1.0",
    pairs[("alice", "carol")]["percent"] == 100.0
    and pairs[("alice", "carol")]["kappa"] is None
    and pairs[("alice", "carol")]["disagreements"] == [],
    str(pairs[("alice", "carol")]),
)
check(
    "bob vs carol: 1 of 2 agree, chance agreement 0.5, kappa 0.0",
    pairs[("bob", "carol")]["percent"] == 50.0
    and pairs[("bob", "carol")]["kappa"] == 0.0
    and pairs[("bob", "carol")]["disagreements"] == ["c2"],
    str(pairs[("bob", "carol")]),
)
text = run("--gold", "gold")[1]
check(
    "the text gives each pair on one line",
    "alice vs bob: 8 shared, 75.0% agree, kappa 0.6667, disagree c2, c7" in text
    and "alice vs carol: 2 shared, 100.0% agree, kappa n/a, disagree none" in text,
    text,
)
alt = report("--gold", "alice")
check(
    "--gold names the excluded labeller: with alice as gold the pairs involve gold",
    [(a["a"], a["b"]) for a in alt["agreement"]]
    == [("bob", "carol"), ("bob", "gold"), ("carol", "gold")],
    str([(a["a"], a["b"]) for a in alt["agreement"]]),
)
solo = report("--gold", "carol")
check(
    "with a single other labeller there is no pair to report",
    [a for a in solo["agreement"] if "carol" in (a["a"], a["b"])] == [],
)

# --- text output --------------------------------------------------------------------
check(
    "the text names under and over cases with their tiers",
    "under (gold above floor): 3 (c2, c6, c10)" in text
    and "c2  gold Medium, floor Low" in text
    and "over (floor above gold): 1 (c4)" in text
    and "c4  gold Low, floor Medium" in text,
    text,
)
check(
    "the text says what under and over mean for a raise-only Jev",
    "the most a raise-only Jev can help" in text
    and "extra checks Jev cannot remove by design" in text,
)
check(
    "the text gives the exact counts, the unlabelled ids and the Jev counts",
    "floor vs gold: 3 of 7 exact" in text
    and "without a gold label: 1 (c7)" in text
    and "Jev: 7 answered, 1 fallback" in text
    and "jev_tier vs gold: 3 of 6 exact" in text
    and "final tier vs gold: 3 of 6 exact" in text,
    text,
)
check(
    "text output is deterministic",
    run("--gold", "gold")[1] == run("--gold", "gold")[1],
)
check(
    "JSON output is deterministic",
    run("--json")[1] == run("--json")[1],
)
check(
    "the JSON has exactly the documented top-level keys",
    set(rep)
    == {
        "gold",
        "note",
        "cases",
        "with_gold",
        "without_gold",
        "without_gold_ids",
        "floor_vs_gold",
        "jev",
        "sweep",
        "agreement",
        "per_case",
    },
    str(sorted(rep)),
)

# --- --check ---------------------------------------------------------------------------
code, out, err = run("--check")
check(
    "--check exits 1 when a gold-labelled case is under",
    code == 1 and "under (gold above floor): 3" in out,
    out,
)
check("without --check the same data exits 0", run()[0] == 0)
check(
    "--check exits 0 when no gold-labelled case is under",
    run("--check", "--gold", "carol")[0] == 0,
)
code, out, err = run("--check", "--gold", "nobody")
check(
    "--check with no gold labels passes but says it checked nothing",
    code == 0 and "no case has a gold label" in err,
    err,
)
over_only = write_jsonl(
    "over-only.jsonl",
    [{"id": "c4", "labeller": "gold", "tier": "Low", "rationale": "x"}],
)
check(
    "an over case alone does not fail --check",
    run("--check", labels=over_only)[0] == 0,
)

# --- the data files are validated -----------------------------------------------------
bad = write_jsonl(
    "bad-tier.jsonl",
    [{"id": "c1", "labeller": "gold", "tier": "Severe", "rationale": "x"}],
)
check("an unknown tier is refused", "unknown tier" in (exits(labels=bad) or ""))
dup = write_jsonl(
    "dup.jsonl",
    [
        {"id": "c1", "labeller": "gold", "tier": "Low", "rationale": "x"},
        {"id": "c1", "labeller": "gold", "tier": "High", "rationale": "y"},
    ],
)
check("a duplicate label is refused", "duplicate label" in (exits(labels=dup) or ""))
stray = write_jsonl(
    "stray.jsonl", [{"id": "zzz", "labeller": "gold", "tier": "Low", "rationale": "x"}]
)
check(
    "a label for an unknown case is refused",
    "unknown case" in (exits(labels=stray) or ""),
)

# --- the shipped files and the command line ---------------------------------------------
fresh_records()
shipped = report(
    "--gold",
    "lead",
    cases=ROOT / "evals" / "consequence" / "cases.jsonl",
    labels=ROOT / "evals" / "consequence" / "labels.jsonl",
)
shipped_cases = len(
    (ROOT / "evals" / "consequence" / "cases.jsonl").read_text().splitlines()
)
check(
    "the shipped cases and labels load and every case is scored",
    shipped["cases"] == shipped_cases
    and shipped["with_gold"] + shipped["without_gold"] == shipped_cases,
    str(shipped["cases"]),
)

env = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "JEV_DISABLE": "1",
    "JEV_RECORD_DIR": str(fresh_records()),
    "HARNESS_EVENTS_LOG": str(EVENTS),
}


def cli(*argv):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "eval_consequence.py"),
            "--cases",
            str(CASES),
            "--labels",
            str(LABELS),
            *argv,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


done = cli("--check")
check(
    "the command line exits 1 under --check with an under case",
    done.returncode == 1 and "under (gold above floor): 3" in done.stdout,
    done.stdout + done.stderr,
)
done = cli("--check", "--gold", "carol")
check("and exits 0 when there is none", done.returncode == 0, done.stdout + done.stderr)
done = cli("--json")
check(
    "--json prints one JSON document and exits 0",
    done.returncode == 0 and json.loads(done.stdout)["cases"] == 8,
    done.stdout + done.stderr,
)

# --- the gold set is a gate on the shipped [tiers] --------------------------------------
# The shipped cases and labels, scored against the shipped harness.toml, offline: every
# gold-labelled pull request must have a floor at or above its gold tier. An empty record
# directory means Jev answers nothing, so only the floor is under test.
gate_env = {
    k: v
    for k, v in env.items()
    if k
    not in ("TYPESAFE_API_KEY", "JEV_RECORD_DIR", "HARNESS_EVENTS_LOG", "JEV_DISABLE")
}
gate_env.update(
    JEV_RECORD_DIR=str(fresh_records()),
    HARNESS_EVENTS_LOG=str(SCRATCH / "gate-events.jsonl"),
)
done = subprocess.run(
    [
        sys.executable,
        str(ROOT / "scripts" / "eval_consequence.py"),
        "--cases",
        str(ROOT / "evals" / "consequence" / "cases.jsonl"),
        "--labels",
        str(ROOT / "evals" / "consequence" / "labels.jsonl"),
        "--gold",
        "gold",
        "--json",
        "--check",
    ],
    capture_output=True,
    text=True,
    env=gate_env,
    check=False,
)
try:
    gate = json.loads(done.stdout)
except ValueError:
    gate = {}
check(
    "the shipped gold set carries 27 gold-labelled pull requests",
    gate.get("with_gold") == 27,
    done.stdout[:300] + done.stderr,
)
check(
    "no shipped pull request has a gold tier above the shipped floor",
    gate.get("floor_vs_gold", {}).get("under") == [],
    str(gate.get("floor_vs_gold", {}).get("under")),
)
check(
    "the same run under --check exits 0",
    done.returncode == 0,
    f"{done.returncode} {done.stderr}",
)
gate_events = SCRATCH / "gate-events.jsonl"
sent_lines = [
    json.loads(line)
    for line in (gate_events.read_text().splitlines() if gate_events.exists() else [])
    if json.loads(line)["decision"] == "call"
]
check(
    "the gate ran offline: Jev answered no case and nothing was sent",
    gate.get("jev", {}).get("answered") == 0 and sent_lines == [],
    f"{gate.get('jev')} {sent_lines}",
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
