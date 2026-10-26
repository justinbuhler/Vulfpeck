"""scripts/jev.py: floor-bound decisions, fallback, redaction and the data rule.

Every expectation is a literal written in this file (Rule 11), never read back
from jev.py's constants. The HTTP layer is replaced, so no network and no quota
is used, and the environment is set explicitly so the check count cannot move
with whether a key happens to be configured. Exchanges are recorded under the
SHA-256 of the request, so every scenario gets its own record directory and the
decision log goes to a scratch file; nothing here touches the real ones.
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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jev  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


FAKE_KEY = "fake-key-for-tests-0123456789"
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
os.environ["JEV_DISABLE"] = ""
os.environ["JEV_OFFLINE"] = ""
for seam in ("settings", "_urlopen", "record_dir", "harness_events"):
    assert hasattr(jev, seam), seam
assert hasattr(jev.harness_events, "log_path")
jev.settings = lambda: {"enabled": True, "timeout": 1.0}
SCRATCH = Path(tempfile.mkdtemp(prefix="jev-test."))
EVENTS = SCRATCH / "events.jsonl"
os.environ["HARNESS_EVENTS_LOG"] = str(EVENTS)
assert jev.harness_events.log_path() == EVENTS


def fresh_records():
    """Point the record directory at a new empty one and return it."""
    directory = Path(tempfile.mkdtemp(prefix="rec.", dir=SCRATCH))
    os.environ["JEV_RECORD_DIR"] = str(directory)
    assert jev.record_dir() == directory
    return directory


def events():
    """Every decision-log line written so far, parsed."""
    if not EVENTS.exists():
        return []
    return [json.loads(line) for line in EVENTS.read_text().splitlines()]


fresh_records()

TIERS = {
    "low": ["**/*.md", "docs/**"],
    "medium": ["docs/adr/**"],
    "high": ["scripts/gate.sh", ".claude/hooks/**"],
    "critical": ["scripts/destroy.py"],
}


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def serve(answers=None, error=None, keep_records=False):
    """Replace the HTTP seam; returns the list of captured requests.

    Each call starts from an empty record directory unless `keep_records`, because
    two scenarios that send the same request would otherwise replay each other.
    """
    if not keep_records:
        fresh_records()
    sent = []

    def fake(request, timeout):
        sent.append(request)
        if error:
            raise error
        return Reply({"answers": answers})

    jev._urlopen = fake
    return sent


def tier_answers(**probs):
    full = {"Low": 0.0, "Medium": 0.0, "High": 0.0, "Critical": 0.0, **probs}
    return {"probabilities": full, "choice": max(full, key=full.get)}


def cons(paths, description="d", **kw):
    return jev.consequence(paths, description, tiers=TIERS, **kw)


# --- the floor ----------------------------------------------------------------
check(
    "docs only is Low", jev.floor_tier(["README.md", "docs/a/b.txt"], TIERS)[0] == "Low"
)
check(
    "an unmatched path is Medium",
    jev.floor_tier(["scripts/x.py"], TIERS)[0] == "Medium",
)
check("gate.sh is High", jev.floor_tier(["scripts/gate.sh"], TIERS)[0] == "High")
check(
    "a hook anywhere under the hooks dir is High",
    jev.floor_tier([".claude/hooks/a/b.py"], TIERS)[0] == "High",
)
check(
    "the highest path sets the floor and is named",
    jev.floor_tier(["README.md", "scripts/x.py", "scripts/gate.sh"], TIERS)
    == ("High", ["scripts/gate.sh"]),
)
check(
    "a critical glob outranks a high one",
    jev.floor_tier(["scripts/gate.sh", "scripts/destroy.py"], TIERS)[0] == "Critical",
)

check(
    "an empty change is Medium, never Low", jev.floor_tier([], TIERS) == ("Medium", [])
)
check(
    "an ADR is Medium though docs/** is Low",
    jev.floor_tier(["docs/adr/0016-x.md"], TIERS)[0] == "Medium",
)
check(
    "a medium glob outranks the low glob that also matches",
    jev.floor_tier(["docs/adr/a.md", "docs/b.md"], TIERS)
    == ("Medium", ["docs/adr/a.md"]),
)

# The shipped harness.toml: every rule and machinery file is at least High, plain docs are Low.
SHIPPED = jev.harness_config.load(jev.REPO)["tiers"]
for path, want in {
    "AGENTS.md": "High",
    "CLAUDE.md": "High",
    "harness.toml": "High",
    ".claude/agents/reviewer-code.md": "High",
    ".claude/skills/land/SKILL.md": "High",
    ".claude/settings.json": "High",
    ".claude/hooks/x.py": "High",
    ".agents/skills/testing/SKILL.md": "High",
    ".codex/config.toml": "High",
    ".gemini/settings.json": "High",
    "scripts/paths.py": "High",
    "scripts/harness_config.py": "High",
    "scripts/jev.py": "High",
    "scripts/plan_tests.py": "High",
    "scripts/standalone_tests.sh": "High",
    "scripts/gate.sh": "High",
    "mutants/runner.py": "High",
    "mutants/patches/M001.patch": "High",
    "tests/test_jev.py": "High",
    "scripts/eval_consequence.py": "High",
    "scripts/eval_tasks.py": "High",
    "evals/consequence/labels.jsonl": "High",
    "evals/tasks/cases/budget-cap.json": "High",
    ".github/workflows/tests.yml": "High",
    ".githooks/pre-commit": "High",
    "docs/adr/0016-x.md": "Medium",
    "scripts/adr.py": "High",
    "scripts/harness_env.sh": "High",
    "docs/agents/issue-tracker.md": "High",
    "scripts/harness_doctor.py": "High",
    "scripts/guard_lib.py": "High",
    "scripts/hook_adapter.py": "High",
    "scripts/render_hooks.py": "High",
    "scripts/loop_reset.py": "High",
    "scripts/scope.py": "High",
    "scripts/harness_events.py": "High",
    "scripts/harness_inputs.py": "High",
    "scripts/harness_install.py": "High",
    "scripts/github_bootstrap.sh": "High",
    "pyproject.toml": "High",
    "uv.lock": "High",
    ".gitignore": "High",
    "scripts/codegraph_freshness.py": "Medium",
    "scripts/worktree_status.sh": "Medium",
    "tests/AGENTS.md": "High",
    "docs/AGENTS.md": "High",
    "docs/mutation-testing.md": "High",
    "docs/notes.md": "Low",
    "README.md": "Low",
    "ENGINEERING_LOG.md": "Low",
    "HARNESS.md": "Low",
}.items():
    check(
        f"shipped floor: {path} is {want}",
        jev.floor_tier([path], SHIPPED)[0] == want,
        jev.floor_tier([path], SHIPPED)[0],
    )

# --- Jev proposes, code decides ------------------------------------------------
serve({"tier": tier_answers(Low=0.95, Medium=0.05), "alters_values": {"noul": 0.05}})
r = cons(["scripts/gate.sh"])
check(
    "Jev cannot lower the floor: High path, Jev Low at 0.95 stays High",
    r["tier"] == "High" and r["floor"] == "High",
    str(r),
)
check(
    "and the Jev reading is still reported",
    r["jev_tier"] == "Low"
    and r["source"] == "jev"
    and r["probabilities"]["Low"] == 0.95,
)

serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
r = cons(["scripts/x.py"])
check(
    "Jev raises Medium to High at 0.7",
    r["tier"] == "High" and r["status"] == "accepted",
    str(r),
)

serve({"tier": tier_answers(Critical=0.65, High=0.35), "alters_values": {"noul": 0.9}})
check("Jev raises to Critical at 0.65", cons(["scripts/x.py"])["tier"] == "Critical")

serve({"tier": tier_answers(High=0.6, Medium=0.4), "alters_values": {"noul": 0.9}})
check("High at exactly 0.6 is accepted", cons(["scripts/x.py"])["status"] == "accepted")

serve({"tier": tier_answers(High=0.55, Medium=0.45), "alters_values": {"noul": 0.9}})
r = cons(["scripts/x.py"])
check(
    "High at 0.55 is a referral, not a decision",
    r["status"] == "referred" and "High at 0.55" in r["reason"],
    str(r),
)
check("a referral carries the provisional max(floor, Jev) tier", r["tier"] == "High")

serve({"tier": tier_answers(Medium=0.7, High=0.3), "alters_values": {"noul": 0.1}})
check(
    "Medium at 0.7 is below 0.8, so a referral",
    cons(["scripts/x.py"])["status"] == "referred",
)

serve({"tier": tier_answers(Medium=0.83, High=0.17), "alters_values": {"noul": 0.65}})
r = cons(["scripts/x.py"])
check(
    "the worked example: Medium at 0.83 with alters_values 0.65 is referred",
    r["status"] == "referred",
    str(r),
)

serve({"tier": tier_answers(Low=0.8, Medium=0.2), "alters_values": {"noul": 0.35}})
r = cons(["README.md"])
check(
    "Low at exactly 0.8 with alters_values exactly 0.35 is accepted",
    r["status"] == "accepted" and r["tier"] == "Low",
    str(r),
)
check("and says the reviewer must confirm", any("reviewer" in n for n in r["notes"]))

serve({"tier": tier_answers(Low=0.9, Medium=0.1), "alters_values": {"noul": 0.36}})
check(
    "alters_values 0.36 voids a Low acceptance",
    cons(["README.md"])["status"] == "referred",
)

serve({"tier": tier_answers(Medium=0.9, Low=0.1), "alters_values": {"noul": 0.1}})
check(
    "Jev can lift a Low docs floor to Medium", cons(["README.md"])["tier"] == "Medium"
)

# --- fallback ------------------------------------------------------------------
sent = serve({"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}})
os.environ["JEV_DISABLE"] = "1"
r = cons(["scripts/x.py"])
os.environ["JEV_DISABLE"] = ""
check(
    "JEV_DISABLE=1 gives the floor, marked fallback, with no request sent",
    r["tier"] == "Medium"
    and r["source"] == "fallback"
    and r["status"] == "fallback"
    and not sent,
    str(r),
)
check("the fallback says why", r["reason"] == "JEV_DISABLE=1")

jev.settings = lambda: {"enabled": False, "timeout": 1.0}
sent = serve({"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}})
r = cons(["scripts/x.py"])
jev.settings = lambda: {"enabled": True, "timeout": 1.0}
check(
    "[jev] enabled = false also falls back without a request",
    r["source"] == "fallback" and not sent,
)

serve(error=urllib.error.HTTPError(jev.URL, 500, "boom", {}, None))
r = cons(["scripts/gate.sh"])
check(
    "an HTTP error falls back to the floor and reports the status only",
    r["tier"] == "High" and r["source"] == "fallback" and r["reason"] == "HTTP 500",
    str(r),
)

serve(error=urllib.error.URLError("no route"))
r = cons(["scripts/x.py"])
check("a network error falls back", r["source"] == "fallback" and r["tier"] == "Medium")

serve({"tier": {"choice": "High"}, "alters_values": {"noul": 0.5}})
check(
    "a Choice with no probabilities falls back",
    cons(["scripts/x.py"])["source"] == "fallback",
)
serve({"tier": tier_answers(High=0.9), "alters_values": {"noul": 1.5}})
check("a Noul outside 0..1 falls back", cons(["scripts/x.py"])["source"] == "fallback")
serve({"tier": tier_answers(High=0.9)})
check("a missing answer falls back", cons(["scripts/x.py"])["source"] == "fallback")

del os.environ["TYPESAFE_API_KEY"]
real_home_key = jev.api_key
jev.api_key = lambda env=None, env_file=None: real_home_key(
    env, env_file=Path(tempfile.mkdtemp()) / "none"
)
sent = serve({"tier": tier_answers(High=0.9), "alters_values": {"noul": 0.5}})
r = cons(["scripts/x.py"])
jev.api_key = real_home_key
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
check(
    "no key falls back without sending",
    r["source"] == "fallback" and not sent and "not set" in r["reason"],
    str(r),
)

# --- the key -------------------------------------------------------------------
envfile = Path(tempfile.mkdtemp()) / ".env"
envfile.write_text("# c\nOTHER=1\nexport TYPESAFE_API_KEY='from-file-key'\n")
check(
    "the key is read from the env file when the environment has none",
    jev.api_key({}, envfile) == "from-file-key",
)
check(
    "the environment wins over the file",
    jev.api_key({"TYPESAFE_API_KEY": " env-key "}, envfile) == "env-key",
)
check("no key anywhere is None", jev.api_key({}, envfile.parent / "missing") is None)

# --- the request: shape, redaction, data rule ------------------------------------
sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
# Split so secret scanners do not read the placeholder as a real key.
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
secret_text = (
    "fix Authorization: Bearer abcdef1234567890XYZ and password=hunter2hunter2 "
    "and sk-abcdefghijklmnopqrstuvwxyz0123 and " + FAKE_AWS_KEY
)
content = "UNIQUE_FILE_CONTENT_9f3a"
src = Path(tempfile.mkdtemp()) / "mod.py"
src.write_text(f"x = '{content}'\n")
cons(
    [str(src), ".env", ".ssh/id_rsa", "scripts/x.py"],
    secret_text,
    stats=[
        "scripts/x.py +1 -0",
        ".env +3 -0",
        "keys/server.pem +9 -9",
        "old.txt => .ssh/id_rsa +1 -0",
    ],
    symbols=["load"],
)
req = sent[0]
raw = req.data.decode()
body = json.loads(raw)
check(
    "one POST to the Jev endpoint with the pinned model",
    len(sent) == 1
    and req.full_url == "https://api.typesafe.ai/v1/systemone"
    and req.get_method() == "POST"
    and body["model"] == "jev-latest",
)
check(
    "the bearer header carries the key and the body does not",
    req.get_header("Authorization") == f"Bearer {FAKE_KEY}" and FAKE_KEY not in raw,
)
for needle in (
    "abcdef1234567890XYZ",
    "hunter2hunter2",
    "sk-abcdefghijklmnopqrstuvwxyz0123",
    FAKE_AWS_KEY,
):
    check(f"redaction strips {needle[:6]}...", needle not in raw)
check("a redaction marker replaces them", "<redacted:" in body["state"]["description"])
check(
    "the state holds only paths, stats, symbols, description and rules",
    set(body["state"]) == {"paths", "stats", "symbols", "description", "rules"},
    str(set(body["state"])),
)
check("file contents are never in the request", content not in raw)
check(
    "credential-file paths are withheld, other paths kept",
    body["state"]["paths"] == [str(src), "scripts/x.py"],
    str(body["state"]["paths"]),
)
check(
    "credential paths are withheld from the stats too",
    body["state"]["stats"] == ["scripts/x.py +1 -0"],
    str(body["state"]["stats"]),
)
check(
    "both typed questions are sent",
    {k: v["type"] for k, v in body["questions"].items()}
    == {"tier": "choice", "alters_values": "noul"},
)

sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
cons([f"dir{i}/file_{i}_with_a_long_name.py" for i in range(5000)], "d" * 9000)
body = json.loads(sent[0].data.decode())
check(
    "an oversized request is truncated under the cap and marked",
    body["state"].get("truncated") is True
    and len(sent[0].data) / 4.0 < 4500
    and len(body["state"]["paths"]) < 5000,
    str(len(sent[0].data)),
)
try:
    jev.fit({"a": 1}, {"q": "x" * 40000})
    refused = False
except jev.JevError:
    refused = True
check("a request that cannot be cut under the cap is refused, not sent", refused)

# --- intent alignment ------------------------------------------------------------
GOOD = {
    "has_actionable_outcome": 0.9,
    "intent_preserves_requested_outcome": 0.9,
    "intent_preserves_explicit_constraints": 0.9,
    "work_supports_requested_outcome": 0.9,
    "intent_is_too_narrow": 0.1,
    "intent_adds_unrequested_work": 0.1,
    "work_omits_material_scope": 0.1,
    "essential_clarification_needed": 0.1,
    "can_proceed": 0.9,
}


def intent(**over):
    serve({k: {"noul": v} for k, v in {**GOOD, **over}.items()})
    return jev.intent_alignment("fix the gate", "fix the gate and rewrite land.sh")


r = intent()
check(
    "nine questions are asked and a clean answer has no drift",
    r["drift"] is False and len(r["scores"]) == 9 and r["issues"] == [],
    str(r),
)
check(
    "adding unrequested work at 0.6 is drift",
    intent(intent_adds_unrequested_work=0.6)["drift"] is True,
)
check(
    "adding unrequested work at 0.59 is not drift",
    intent(intent_adds_unrequested_work=0.59)["drift"] is False,
)
check(
    "omitting scope (too narrow) at 0.6 is drift",
    intent(intent_is_too_narrow=0.6)["drift"] is True,
)
check(
    "omitting scope (material scope) at 0.6 is drift",
    intent(work_omits_material_scope=0.6)["drift"] is True,
)
check(
    "not preserving the outcome at 0.49 is drift",
    intent(intent_preserves_requested_outcome=0.49)["drift"] is True,
)
check(
    "preserving the outcome at 0.5 is not drift",
    intent(intent_preserves_requested_outcome=0.5)["drift"] is False,
)
check("an unrelated low score is not drift", intent(can_proceed=0.1)["drift"] is False)
check(
    "the drift reason is named",
    "adds unrequested work" in intent(intent_adds_unrequested_work=0.9)["issues"],
)
sent = serve(error=urllib.error.HTTPError(jev.URL, 429, "x", {}, None))
r = jev.intent_alignment("a", "b")
check(
    "intent fallback reports no drift, marked fallback",
    r["drift"] is False and r["source"] == "fallback" and r["reason"] == "HTTP 429",
)
body = json.loads(sent[0].data.decode())
check(
    "the intent state is the request and the proposal only",
    set(body["state"]) == {"user_request", "proposed_work"},
)


# --- reassess ---------------------------------------------------------------------
def action(attempts, **probs):
    full = {"continue": 0.0, "change_approach": 0.0, "escalate": 0.0, **probs}
    serve({"action": {"probabilities": full, "choice": max(full, key=full.get)}})
    return jev.reassess(
        {
            "fingerprint": "pytest:test_x",
            "attempts": attempts,
            "last_errors": ["E assert 1 == 2"],
        }
    )


check(
    "first sighting, Jev says continue: continue",
    action(1, **{"continue": 0.9})["action"] == "continue",
)
check(
    "third sighting, Jev says continue: still change_approach (Jev cannot lower)",
    action(3, **{"continue": 0.9})["action"] == "change_approach",
)
check(
    "first sighting, Jev says escalate at 0.8: escalate",
    action(1, escalate=0.8, change_approach=0.2)["action"] == "escalate",
)
r = action(1, escalate=0.5, **{"continue": 0.3, "change_approach": 0.2})
check(
    "Jev below 0.6 is a referral at the floor",
    r["status"] == "referred" and r["action"] == "continue",
    str(r),
)
check(
    "Jev may raise change_approach to escalate at 0.7",
    action(4, escalate=0.7, change_approach=0.3)["action"] == "escalate",
)
os.environ["JEV_DISABLE"] = "1"
fb = [
    jev.reassess({"fingerprint": "f", "attempts": n, "last_errors": []})["action"]
    for n in (1, 2, 3, 6)
]
os.environ["JEV_DISABLE"] = ""
check(
    "fallback: seen 1 and 2 times continue, 3 and 6 change approach",
    fb == ["continue", "continue", "change_approach", "change_approach"],
    str(fb),
)
for bad in (0, -1, True, "3", None):
    try:
        jev.reassess({"fingerprint": "f", "attempts": bad})
        ok = False
    except ValueError:
        ok = True
    check(f"attempts={bad!r} is refused", ok)
sent = serve(
    {
        "action": {
            "probabilities": {"continue": 1.0, "change_approach": 0.0, "escalate": 0.0}
        }
    }
)
jev.reassess(
    {
        "fingerprint": "f",
        "attempts": 1,
        "last_errors": ["boom line one\nsecret line two", "token=abcdef123456789 here"],
    }
)
body = json.loads(sent[0].data.decode())
check(
    "errors are cut to their first line and redacted",
    body["state"]["last_errors"] == ["boom line one", "token=<redacted:kv> here"],
    str(body["state"]["last_errors"]),
)

# --- record and replay -------------------------------------------------------------
# The key is the SHA-256 of the exact bytes sent, so a test derives it from the
# captured request, never from jev.py; one body and its key are also pinned as literals.
Q = {"q": {"type": "noul"}}
LITERAL_BODY = (
    b'{"model":"jev-latest","questions":{"q":{"type":"noul"}},"state":{"a":1}}'
)
LITERAL_KEY = "bb7e2bc814cbcc4618474315d6df2291753ea8e0b2bb2d011f41d383d76410ee"
OK_ANSWER = {"q": {"noul": 0.25}}


def key_of(request):
    return hashlib.sha256(request.data).hexdigest()


def raises(call):
    """The JevError message `call` raises, or None when it returns."""
    try:
        call()
    except jev.JevError as exc:
        return str(exc)
    return None


records = fresh_records()
sent = serve(OK_ANSWER, keep_records=True)
answers, key = jev.evaluate({"a": 1}, Q)
check(
    "the body is canonical JSON: sorted keys, compact separators",
    len(sent) == 1 and sent[0].data == LITERAL_BODY,
    str(sent[0].data),
)
check(
    "the key is the SHA-256 of the body bytes",
    key == LITERAL_KEY and key_of(sent[0]) == LITERAL_KEY,
    key,
)
rec = json.loads((records / f"{LITERAL_KEY}.json").read_text())
check(
    "a miss records the exact payload sent under its SHA-256, and nothing else",
    sorted(os.listdir(records)) == [f"{LITERAL_KEY}.json"]
    and rec["request"] == {"state": {"a": 1}, "questions": Q}
    and rec["answers"] == OK_ANSWER
    and answers == OK_ANSWER,
    str(rec),
)
check(
    "the record names its version, key, url, model and a UTC timestamp",
    set(rec) == {"v", "key", "ts", "url", "model", "request", "answers"}
    and rec["v"] == 1
    and rec["key"] == LITERAL_KEY
    and rec["url"] == "https://api.typesafe.ai/v1/systemone"
    and rec["model"] == "jev-latest"
    and rec["ts"].endswith("+00:00")
    and datetime.fromisoformat(rec["ts"]).year >= 2026,
    str(rec),
)

sent = serve({"q": {"noul": 0.99}}, keep_records=True)
answers, key = jev.evaluate({"a": 1}, Q)
check(
    "a second identical call makes no network call and returns the recorded answer",
    sent == [] and answers == OK_ANSWER and key == LITERAL_KEY,
    str(answers),
)
del os.environ["TYPESAFE_API_KEY"]
answers, key = jev.evaluate({"a": 1}, Q)
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
check(
    "a recorded answer needs no API key", sent == [] and answers == OK_ANSWER, str(sent)
)

sent = serve(OK_ANSWER, keep_records=True)
_, first = jev.evaluate({"a": 1, "b": 2}, Q)
_, second = jev.evaluate({"b": 2, "a": 1}, Q)
check(
    "equal requests hash equally whatever the key order: one send, one key",
    len(sent) == 1
    and first == second
    and sent[0].data
    == b'{"model":"jev-latest","questions":{"q":{"type":"noul"}},"state":{"a":1,"b":2}}',
    str(sent),
)
jev.evaluate({"a": 2}, Q)
check("a different payload is a miss and is sent", len(sent) == 2)

# scripts/task_loop.py unpacks `_ask` as (answers, error); its own tests replace it with that shape.
sent = serve(OK_ANSWER)
asked = jev._ask(Q, {"a": 1})
os.environ["JEV_DISABLE"] = "1"
off = jev._ask(Q, {"a": 1})
os.environ["JEV_DISABLE"] = ""
serve(error=urllib.error.HTTPError(jev.URL, 503, "x", {}, None))
failed = jev._ask(Q, {"a": 1})
check(
    "_ask keeps its two-value contract: (answers, None), or (None, reason) when off or failed",
    asked == (OK_ANSWER, None)
    and off == (None, "JEV_DISABLE=1")
    and failed == (None, "HTTP 503"),
    str((asked, off, failed)),
)

# A decision's own record: the three decision functions each name it.
sent = serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
r = cons(["scripts/x.py"])
check(
    "consequence names the record of its exchange",
    r["record"] == key_of(sent[0])
    and (Path(os.environ["JEV_RECORD_DIR"]) / f"{r['record']}.json").is_file(),
    str(r["record"]),
)
sent = serve({k: {"noul": v} for k, v in GOOD.items()})
r = jev.intent_alignment("fix the gate", "fix the gate and rewrite land.sh")
check("intent_alignment names the record of its exchange", r["record"] == key_of(sent[0]))
sent = serve(
    {"action": {"probabilities": {"continue": 0.9, "change_approach": 0.1, "escalate": 0.0}}}
)
r = jev.reassess({"fingerprint": "f", "attempts": 1, "last_errors": []})
check("reassess names the record of its exchange", r["record"] == key_of(sent[0]))
serve(error=urllib.error.HTTPError(jev.URL, 500, "boom", {}, None))
check(
    "a fallback has no record",
    cons(["scripts/x.py"])["record"] is None
    and jev.intent_alignment("a", "b")["record"] is None
    and jev.reassess({"fingerprint": "f", "attempts": 1})["record"] is None,
)

# Offline: cache only, and a miss is an error the caller falls back on, never a "no".
os.environ["JEV_OFFLINE"] = "1"
sent = serve({"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}})
r = cons(["scripts/x.py"])
check(
    "an offline miss falls back: source fallback, the floor, and says why",
    r["source"] == "fallback"
    and r["status"] == "fallback"
    and r["tier"] == "Medium"
    and r["jev_tier"] is None
    and r["reason"] == "not recorded (offline)"
    and r["record"] is None
    and sent == [],
    str(r),
)
check(
    "an offline evaluate raises rather than return an answer",
    raises(lambda: jev.evaluate({"a": 1}, Q)) == "not recorded (offline)",
)
os.environ["JEV_OFFLINE"] = ""
sent = serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
cons(["scripts/x.py"])
os.environ["JEV_OFFLINE"] = "1"
del os.environ["TYPESAFE_API_KEY"]
sent2 = serve(
    {"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}},
    keep_records=True,
)
r = cons(["scripts/x.py"])
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
os.environ["JEV_OFFLINE"] = ""
check(
    "offline, a recorded answer is replayed with no key and no request",
    r["source"] == "jev"
    and r["tier"] == "High"
    and r["jev_tier"] == "High"
    and r["record"] == key_of(sent[0])
    and sent2 == [],
    str(r),
)

# A record that does not parse, or whose answers fail validation, is a miss.
sent = serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
cons(["scripts/x.py"])
records = Path(os.environ["JEV_RECORD_DIR"])
path = records / f"{key_of(sent[0])}.json"
good = json.loads(path.read_text())
BAD = {
    "truncated JSON": '{"v": 1, "answers": ',
    "no content": "",
    "a JSON list at the top": "[]",
    "no answers": json.dumps({"v": 1}),
    "a noul outside 0..1": json.dumps(
        {**good, "answers": {**good["answers"], "alters_values": {"noul": 1.5}}}
    ),
    "a choice with no probabilities": json.dumps(
        {**good, "answers": {**good["answers"], "tier": {"choice": "High"}}}
    ),
    "a question left unanswered": json.dumps(
        {**good, "answers": {"tier": good["answers"]["tier"]}}
    ),
}
os.environ["JEV_OFFLINE"] = "1"
for label, text in BAD.items():
    path.write_text(text)
    r = cons(["scripts/x.py"])
    check(
        f"a record with {label} is a miss, not an answer",
        r["source"] == "fallback"
        and r["reason"] == "not recorded (offline)"
        and len(sent) == 1,
        str(r),
    )
os.environ["JEV_OFFLINE"] = ""
path.write_text('{"v": 1, "answers": ')
r = cons(["scripts/x.py"])
check(
    "online, a corrupt record is re-fetched and replaced by a valid one",
    len(sent) == 2
    and r["source"] == "jev"
    and json.loads(path.read_text())["answers"] == good["answers"]
    and os.listdir(records) == [path.name],
    str(os.listdir(records)),
)

# The key and headers never reach the file; the request on disk is the redacted one.
sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
cons(["scripts/x.py"], "plain description")
text = (Path(os.environ["JEV_RECORD_DIR"]) / f"{key_of(sent[0])}.json").read_text()
check(
    "neither the API key nor the request headers are written to the record",
    FAKE_KEY not in text
    and "Authorization" not in text
    and "Bearer" not in text
    and "Content-Type" not in text,
)
sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
cons(["scripts/x.py"], secret_text)
text = (Path(os.environ["JEV_RECORD_DIR"]) / f"{key_of(sent[0])}.json").read_text()
check(
    "the recorded request is the redacted one actually sent",
    "<redacted:" in text
    and all(
        needle not in text
        for needle in (
            "abcdef1234567890XYZ",
            "hunter2hunter2",
            "sk-abcdefghijklmnopqrstuvwxyz0123",
            FAKE_AWS_KEY,
        )
    ),
)

# The decision log: one line per exchange, never the request content.
DESCRIPTION = "UNIQUE_DESCRIPTION_7c1e"
mark = len(events())
sent = serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
cons(["scripts/x.py"], DESCRIPTION)
cons(["scripts/x.py"], DESCRIPTION)
key = key_of(sent[0])
fresh_records()
os.environ["JEV_OFFLINE"] = "1"
cons(["scripts/x.py"], DESCRIPTION)
os.environ["JEV_OFFLINE"] = ""
serve(error=urllib.error.HTTPError(jev.URL, 500, "boom", {}, None))
cons(["scripts/x.py"], DESCRIPTION)
rows = events()[mark:]
check(
    "each exchange logs one jev line: call, hit, offline-miss, error",
    [(e["hook"], e["decision"]) for e in rows]
    == [("jev", "call"), ("jev", "hit"), ("jev", "offline-miss"), ("jev", "error")],
    str([(e["hook"], e["decision"]) for e in rows]),
)
check(
    "every line's subject is the first 12 characters of the exchange key",
    [e["subject"] for e in rows] == [key[:12]] * 4,
    str([e["subject"] for e in rows]),
)
check(
    "the reason is a short code: recorded, none, offline, the HTTP status",
    [e["reason"] for e in rows]
    == ["recorded", "", "not recorded (offline)", "HTTP 500"],
    str([e["reason"] for e in rows]),
)
raw = EVENTS.read_text()
check(
    "the log carries no request content, path or key",
    DESCRIPTION not in raw and "scripts/x.py" not in raw and FAKE_KEY not in raw,
)

mark = len(events())
directory = fresh_records()
os.environ["JEV_DISABLE"] = "1"
r = cons(["scripts/x.py"])
os.environ["JEV_DISABLE"] = ""
check(
    "disabled short-circuits before any record, key or log line",
    r["source"] == "fallback"
    and r["record"] is None
    and os.listdir(directory) == []
    and len(events()) == mark,
    str(r),
)

# Recording never raises into a caller.
blocker = SCRATCH / "not-a-directory"
blocker.write_text("a file where the record directory should be")
mark = len(events())
sent = serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
os.environ["JEV_RECORD_DIR"] = str(blocker / "records")
stderr = io.StringIO()
with contextlib.redirect_stderr(stderr):
    r = cons(["scripts/x.py"])
check(
    "a failing record write does not change the answer",
    r["source"] == "jev"
    and r["tier"] == "High"
    and r["status"] == "accepted"
    and len(sent) == 1,
    str(r),
)
check(
    "the failed write is a stderr note without the key, and the log says so",
    "not recorded" in stderr.getvalue()
    and FAKE_KEY not in stderr.getvalue()
    and [(e["decision"], e["reason"]) for e in events()[mark:]]
    == [("call", "not recorded")],
    stderr.getvalue(),
)

# Where the records live when nothing overrides them.
saved = os.environ.pop("JEV_RECORD_DIR")
git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
common = subprocess.run(
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    cwd=jev.REPO,
    capture_output=True,
    text=True,
    check=True,
    env=git_env,
).stdout.strip()
os.environ["GIT_DIR"] = str(SCRATCH / "no-such-git-dir")
default_dir = jev.record_dir()
del os.environ["GIT_DIR"]
os.environ["JEV_RECORD_DIR"] = saved
check(
    "by default records go to <git common dir>/harness/jev, whatever GIT_DIR says",
    default_dir == Path(common) / "harness" / "jev",
    str(default_dir),
)
check(
    "and never into the work tree outside .git",
    not default_dir.is_relative_to(jev.REPO)
    or default_dir.is_relative_to(jev.REPO / ".git"),
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
