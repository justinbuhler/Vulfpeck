"""scripts/jev.py: floor-bound decisions, fallback, redaction and the data rule.

Every expectation is a literal written in this file (Rule 11), never read back
from jev.py's constants. The HTTP layer is replaced, so no network and no quota
is used, and the environment is set explicitly so the check count cannot move
with whether a key happens to be configured.
"""

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jev  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + detail) if detail and not cond else ""))


FAKE_KEY = "fake-key-for-tests-0123456789"
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
os.environ["JEV_DISABLE"] = ""
for seam in ("settings", "_urlopen"):
    assert hasattr(jev, seam), seam
jev.settings = lambda: {"enabled": True, "timeout": 1.0}

TIERS = {"low": ["**/*.md", "docs/**"], "high": ["scripts/gate.sh", ".claude/hooks/**"], "critical": ["scripts/destroy.py"]}


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def serve(answers=None, error=None):
    """Replace the HTTP seam; returns the list of captured requests."""
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
check("docs only is Low", jev.floor_tier(["README.md", "docs/a/b.txt"], TIERS)[0] == "Low")
check("an unmatched path is Medium", jev.floor_tier(["scripts/x.py"], TIERS)[0] == "Medium")
check("gate.sh is High", jev.floor_tier(["scripts/gate.sh"], TIERS)[0] == "High")
check("a hook anywhere under the hooks dir is High", jev.floor_tier([".claude/hooks/a/b.py"], TIERS)[0] == "High")
check("the highest path sets the floor and is named",
      jev.floor_tier(["README.md", "scripts/x.py", "scripts/gate.sh"], TIERS) == ("High", ["scripts/gate.sh"]))
check("a critical glob outranks a high one", jev.floor_tier(["scripts/gate.sh", "scripts/destroy.py"], TIERS)[0] == "Critical")

# --- Jev proposes, code decides ------------------------------------------------
serve({"tier": tier_answers(Low=0.95, Medium=0.05), "alters_values": {"noul": 0.05}})
r = cons(["scripts/gate.sh"])
check("Jev cannot lower the floor: High path, Jev Low at 0.95 stays High", r["tier"] == "High" and r["floor"] == "High", str(r))
check("and the Jev reading is still reported", r["jev_tier"] == "Low" and r["source"] == "jev" and r["probabilities"]["Low"] == 0.95)

serve({"tier": tier_answers(High=0.7, Medium=0.3), "alters_values": {"noul": 0.9}})
r = cons(["scripts/x.py"])
check("Jev raises Medium to High at 0.7", r["tier"] == "High" and r["status"] == "accepted", str(r))

serve({"tier": tier_answers(Critical=0.65, High=0.35), "alters_values": {"noul": 0.9}})
check("Jev raises to Critical at 0.65", cons(["scripts/x.py"])["tier"] == "Critical")

serve({"tier": tier_answers(High=0.6, Medium=0.4), "alters_values": {"noul": 0.9}})
check("High at exactly 0.6 is accepted", cons(["scripts/x.py"])["status"] == "accepted")

serve({"tier": tier_answers(High=0.55, Medium=0.45), "alters_values": {"noul": 0.9}})
r = cons(["scripts/x.py"])
check("High at 0.55 is a referral, not a decision", r["status"] == "referred" and "High at 0.55" in r["reason"], str(r))
check("a referral carries the provisional max(floor, Jev) tier", r["tier"] == "High")

serve({"tier": tier_answers(Medium=0.7, High=0.3), "alters_values": {"noul": 0.1}})
check("Medium at 0.7 is below 0.8, so a referral", cons(["scripts/x.py"])["status"] == "referred")

serve({"tier": tier_answers(Medium=0.83, High=0.17), "alters_values": {"noul": 0.65}})
r = cons(["scripts/x.py"])
check("the worked example: Medium at 0.83 with alters_values 0.65 is referred", r["status"] == "referred", str(r))

serve({"tier": tier_answers(Low=0.8, Medium=0.2), "alters_values": {"noul": 0.35}})
r = cons(["README.md"])
check("Low at exactly 0.8 with alters_values exactly 0.35 is accepted", r["status"] == "accepted" and r["tier"] == "Low", str(r))
check("and says the reviewer must confirm", any("reviewer" in n for n in r["notes"]))

serve({"tier": tier_answers(Low=0.9, Medium=0.1), "alters_values": {"noul": 0.36}})
check("alters_values 0.36 voids a Low acceptance", cons(["README.md"])["status"] == "referred")

serve({"tier": tier_answers(Medium=0.9, Low=0.1), "alters_values": {"noul": 0.1}})
check("Jev can lift a Low docs floor to Medium", cons(["README.md"])["tier"] == "Medium")

# --- fallback ------------------------------------------------------------------
sent = serve({"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}})
os.environ["JEV_DISABLE"] = "1"
r = cons(["scripts/x.py"])
os.environ["JEV_DISABLE"] = ""
check("JEV_DISABLE=1 gives the floor, marked fallback, with no request sent",
      r["tier"] == "Medium" and r["source"] == "fallback" and r["status"] == "fallback" and not sent, str(r))
check("the fallback says why", r["reason"] == "JEV_DISABLE=1")

jev.settings = lambda: {"enabled": False, "timeout": 1.0}
sent = serve({"tier": tier_answers(Critical=0.99), "alters_values": {"noul": 0.99}})
r = cons(["scripts/x.py"])
jev.settings = lambda: {"enabled": True, "timeout": 1.0}
check("[jev] enabled = false also falls back without a request", r["source"] == "fallback" and not sent)

serve(error=urllib.error.HTTPError(jev.URL, 500, "boom", {}, None))
r = cons(["scripts/gate.sh"])
check("an HTTP error falls back to the floor and reports the status only",
      r["tier"] == "High" and r["source"] == "fallback" and r["reason"] == "HTTP 500", str(r))

serve(error=urllib.error.URLError("no route"))
r = cons(["scripts/x.py"])
check("a network error falls back", r["source"] == "fallback" and r["tier"] == "Medium")

serve({"tier": {"choice": "High"}, "alters_values": {"noul": 0.5}})
check("a Choice with no probabilities falls back", cons(["scripts/x.py"])["source"] == "fallback")
serve({"tier": tier_answers(High=0.9), "alters_values": {"noul": 1.5}})
check("a Noul outside 0..1 falls back", cons(["scripts/x.py"])["source"] == "fallback")
serve({"tier": tier_answers(High=0.9)})
check("a missing answer falls back", cons(["scripts/x.py"])["source"] == "fallback")

del os.environ["TYPESAFE_API_KEY"]
real_home_key = jev.api_key
jev.api_key = lambda env=None, env_file=None: real_home_key(env, env_file=Path(tempfile.mkdtemp()) / "none")
sent = serve({"tier": tier_answers(High=0.9), "alters_values": {"noul": 0.5}})
r = cons(["scripts/x.py"])
jev.api_key = real_home_key
os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
check("no key falls back without sending", r["source"] == "fallback" and not sent and "not set" in r["reason"], str(r))

# --- the key -------------------------------------------------------------------
envfile = Path(tempfile.mkdtemp()) / ".env"
envfile.write_text("# c\nOTHER=1\nexport TYPESAFE_API_KEY='from-file-key'\n")
check("the key is read from the env file when the environment has none", jev.api_key({}, envfile) == "from-file-key")
check("the environment wins over the file", jev.api_key({"TYPESAFE_API_KEY": " env-key "}, envfile) == "env-key")
check("no key anywhere is None", jev.api_key({}, envfile.parent / "missing") is None)

# --- the request: shape, redaction, data rule ------------------------------------
sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
secret_text = ("fix Authorization: Bearer abcdef1234567890XYZ and password=hunter2hunter2 "
               "and sk-abcdefghijklmnopqrstuvwxyz0123 and AKIAABCDEFGHIJKLMNOP")
content = "UNIQUE_FILE_CONTENT_9f3a"
src = Path(tempfile.mkdtemp()) / "mod.py"
src.write_text(f"x = '{content}'\n")
cons([str(src), ".env", ".ssh/id_rsa", "scripts/x.py"], secret_text, stats=["scripts/x.py +1 -0"], symbols=["load"])
req = sent[0]
raw = req.data.decode()
body = json.loads(raw)
check("one POST to the Jev endpoint with the pinned model", len(sent) == 1 and req.full_url == "https://api.typesafe.ai/v1/systemone"
      and req.get_method() == "POST" and body["model"] == "jev-latest")
check("the bearer header carries the key and the body does not",
      req.get_header("Authorization") == f"Bearer {FAKE_KEY}" and FAKE_KEY not in raw)
for needle in ("abcdef1234567890XYZ", "hunter2hunter2", "sk-abcdefghijklmnopqrstuvwxyz0123", "AKIAABCDEFGHIJKLMNOP"):
    check(f"redaction strips {needle[:6]}...", needle not in raw)
check("a redaction marker replaces them", "<redacted:" in body["state"]["description"])
check("the state holds only paths, stats, symbols, description and rules",
      set(body["state"]) == {"paths", "stats", "symbols", "description", "rules"}, str(set(body["state"])))
check("file contents are never in the request", content not in raw)
check("credential-file paths are withheld, other paths kept",
      body["state"]["paths"] == [str(src), "scripts/x.py"], str(body["state"]["paths"]))
check("both typed questions are sent", {k: v["type"] for k, v in body["questions"].items()} == {"tier": "choice", "alters_values": "noul"})

sent = serve({"tier": tier_answers(Medium=0.9), "alters_values": {"noul": 0.1}})
cons([f"dir{i}/file_{i}_with_a_long_name.py" for i in range(5000)], "d" * 9000)
body = json.loads(sent[0].data.decode())
check("an oversized request is truncated under the cap and marked",
      body["state"].get("truncated") is True and len(sent[0].data) / 4.0 < 4500 and len(body["state"]["paths"]) < 5000,
      str(len(sent[0].data)))
try:
    jev.fit({"a": 1}, {"q": "x" * 40000})
    refused = False
except jev.JevError:
    refused = True
check("a request that cannot be cut under the cap is refused, not sent", refused)

# --- intent alignment ------------------------------------------------------------
GOOD = {"has_actionable_outcome": 0.9, "intent_preserves_requested_outcome": 0.9, "intent_preserves_explicit_constraints": 0.9,
        "work_supports_requested_outcome": 0.9, "intent_is_too_narrow": 0.1, "intent_adds_unrequested_work": 0.1,
        "work_omits_material_scope": 0.1, "essential_clarification_needed": 0.1, "can_proceed": 0.9}


def intent(**over):
    serve({k: {"noul": v} for k, v in {**GOOD, **over}.items()})
    return jev.intent_alignment("fix the gate", "fix the gate and rewrite land.sh")


r = intent()
check("nine questions are asked and a clean answer has no drift", r["drift"] is False and len(r["scores"]) == 9 and r["issues"] == [], str(r))
check("adding unrequested work at 0.6 is drift", intent(intent_adds_unrequested_work=0.6)["drift"] is True)
check("adding unrequested work at 0.59 is not drift", intent(intent_adds_unrequested_work=0.59)["drift"] is False)
check("omitting scope (too narrow) at 0.6 is drift", intent(intent_is_too_narrow=0.6)["drift"] is True)
check("omitting scope (material scope) at 0.6 is drift", intent(work_omits_material_scope=0.6)["drift"] is True)
check("not preserving the outcome at 0.49 is drift", intent(intent_preserves_requested_outcome=0.49)["drift"] is True)
check("preserving the outcome at 0.5 is not drift", intent(intent_preserves_requested_outcome=0.5)["drift"] is False)
check("an unrelated low score is not drift", intent(can_proceed=0.1)["drift"] is False)
check("the drift reason is named", "adds unrequested work" in intent(intent_adds_unrequested_work=0.9)["issues"])
sent = serve(error=urllib.error.HTTPError(jev.URL, 429, "x", {}, None))
r = jev.intent_alignment("a", "b")
check("intent fallback reports no drift, marked fallback", r["drift"] is False and r["source"] == "fallback" and r["reason"] == "HTTP 429")
body = json.loads(sent[0].data.decode())
check("the intent state is the request and the proposal only", set(body["state"]) == {"user_request", "proposed_work"})

# --- reassess ---------------------------------------------------------------------
def action(attempts, **probs):
    full = {"continue": 0.0, "change_approach": 0.0, "escalate": 0.0, **probs}
    serve({"action": {"probabilities": full, "choice": max(full, key=full.get)}})
    return jev.reassess({"fingerprint": "pytest:test_x", "attempts": attempts, "last_errors": ["E assert 1 == 2"]})


check("first sighting, Jev says continue: continue", action(1, **{"continue": 0.9})["action"] == "continue")
check("third sighting, Jev says continue: still change_approach (Jev cannot lower)",
      action(3, **{"continue": 0.9})["action"] == "change_approach")
check("first sighting, Jev says escalate at 0.8: escalate", action(1, escalate=0.8, change_approach=0.2)["action"] == "escalate")
r = action(1, escalate=0.5, **{"continue": 0.3, "change_approach": 0.2})
check("Jev below 0.6 is a referral at the floor", r["status"] == "referred" and r["action"] == "continue", str(r))
check("Jev may raise change_approach to escalate at 0.7", action(4, escalate=0.7, change_approach=0.3)["action"] == "escalate")
os.environ["JEV_DISABLE"] = "1"
fb = [jev.reassess({"fingerprint": "f", "attempts": n, "last_errors": []})["action"] for n in (1, 2, 3, 6)]
os.environ["JEV_DISABLE"] = ""
check("fallback: seen 1 and 2 times continue, 3 and 6 change approach",
      fb == ["continue", "continue", "change_approach", "change_approach"], str(fb))
for bad in (0, -1, True, "3", None):
    try:
        jev.reassess({"fingerprint": "f", "attempts": bad})
        ok = False
    except ValueError:
        ok = True
    check(f"attempts={bad!r} is refused", ok)
sent = serve({"action": {"probabilities": {"continue": 1.0, "change_approach": 0.0, "escalate": 0.0}}})
jev.reassess({"fingerprint": "f", "attempts": 1, "last_errors": ["boom line one\nsecret line two", "token=abcdef123456789 here"]})
body = json.loads(sent[0].data.decode())
check("errors are cut to their first line and redacted",
      body["state"]["last_errors"] == ["boom line one", "token=<redacted:kv> here"], str(body["state"]["last_errors"]))

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
