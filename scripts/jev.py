#!/usr/bin/env python3
"""A small Jev client with typed decisions: Jev proposes, code decides.

Jev is TypeSafe's judgment model (https://api.typesafe.ai/v1/systemone). It
answers a Choice (one option, a probability for each) or a Noul (a yes/no
probability), never free text. This module sends the question, then code applies
fixed thresholds. Thresholds live here, never in a prompt (docs/mutation-testing.md).

DATA RULE. A request carries only file paths, diff stats, names of changed
symbols, the task description and short rule text. It never carries file
contents or environment values. Every string is redacted for secret shapes
(bearer tokens, key=value pairs, PEM blocks, vendor key formats) before it is
sent, paths that name credential files are withheld, and a request over
MAX_REQUEST_TOKENS is truncated, or refused when it cannot be made to fit.

FALLBACK. `JEV_DISABLE=1`, `[jev] enabled = false`, a missing key, a transport
error or a malformed answer all give a deterministic result marked
`source: "fallback"`. A failed call is never read as "no".

KEY. `TYPESAFE_API_KEY` from the environment, else from `~/.env` (read, never
copied, printed or logged). Error messages carry an HTTP status, never a body.

Decisions, each returning a plain dict with `probabilities` and `source`:

    consequence(paths, description)    Low | Medium | High | Critical, floor-bound
    intent_alignment(request, proposed) facts plus a code-computed `drift`
    reassess(context)                  continue | change_approach | escalate

    python3 scripts/jev.py consequence --path scripts/gate.sh --description "..."
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
from adr import glob_match  # noqa: E402
from paths import REPO  # noqa: E402

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
MAX_REQUEST_TOKENS = 4500
CHARS_PER_TOKEN = 4.0  # measured ~4.3 on JSON payloads; 4.0 errs high
TIERS = ("Low", "Medium", "High", "Critical")
ACTIONS = ("continue", "change_approach", "escalate")

# docs/mutation-testing.md section 2, applied by consequence().
ACCEPT_HIGH = 0.6  # High or Critical at or above this is accepted
ACCEPT_LOW = 0.8  # Low or Medium at or above this, and ...
ALTERS_MAX = 0.35  # ... alters-values at or below this, is accepted
# intent_alignment() drift rule (ported from jev_intent_align.py).
YES = 0.6
CORE = 0.5
# reassess(): a fingerprint seen this often is a stop (AGENTS.md "Stop and reassess").
STOP_AT = 3

_urlopen = urllib.request.urlopen  # the one HTTP seam; tests replace it


class JevError(Exception):
    """Jev could not give a usable answer; the caller falls back."""


# --- redaction ----------------------------------------------------------------
_KEY = r"(?:secret|token|passw|api[_-]?key|private|credential|auth)"
_VAL = r"[A-Za-z0-9._~+/=\-]"
_PATTERNS = [
    (
        "pem",
        re.compile(
            r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"  # security-review: ignore - detection regex
        ),
    ),
    ("cookie", re.compile(r"(?i)\b(?:Set-Cookie|Cookie)\s*[:=]\s*([^\r\n]+)")),
    ("bearer", re.compile(r"(?i)\bBearer\s+(" + _VAL + r"{8,})")),
    (
        "authorization",
        re.compile(
            r"(?i)\bAuthorization\s*[:=]\s*[\"']?(?:Basic\s+)?(" + _VAL + r"{8,})"
        ),
    ),
    (
        "jwt",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    ),
    ("aws-key", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("github", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b")),
    ("slack", re.compile(r"\b(xox[abp]-[A-Za-z0-9-]{10,})\b")),
    ("google", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b")),
    ("stripe", re.compile(r"\b(sk_(?:live|test)_[A-Za-z0-9]{8,})\b")),
    ("openai", re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b")),
    ("url-cred", re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@")),
    (
        "kv",
        re.compile(
            r"(?i)[\"']?\b(?:[A-Za-z0-9_.-]*"
            + _KEY
            + r"[A-Za-z0-9_.-]*)[\"']?\s*[:=]\s*[\"']?("
            + _VAL
            + r"{8,})"
        ),
    ),
    (
        "entropy",
        re.compile(
            r"(?i)\b[A-Za-z0-9_.-]*"
            + _KEY
            + r"[A-Za-z0-9_.-]*\W{1,6}((?:[A-Za-z0-9+/=_-]{32,}|[0-9a-f]{32,}))"
        ),
    ),
]
_PROTECTED_DIRS = {".ssh", ".gnupg", ".aws", ".config", "secrets", "keyring"}
_PROTECTED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def redact_text(text):
    """Return `text` with every secret-shaped value replaced by `<redacted:TYPE>`."""
    for kind, pattern in _PATTERNS:

        def sub(m, kind=kind):
            return m.group(0).replace(m.group(1), f"<redacted:{kind}>")

        text = pattern.sub(sub, text)
    return text


def redact(node):
    """Deep copy of a JSON value with every string redacted."""
    if isinstance(node, str):
        return redact_text(node)
    if isinstance(node, dict):
        return {k: redact(v) for k, v in node.items()}
    if isinstance(node, list):
        return [redact(v) for v in node]
    return node


def is_protected_path(path):
    """True for a path that names a credential file or sits in a secret directory."""
    p = PurePath(str(path))
    name = p.name
    return bool(
        set(p.parts[:-1]) & _PROTECTED_DIRS
        or name.startswith((".env", "id_", "credentials"))
        or name in {"token.json", ".tokens", "keyring"}
        or p.suffix.lower() in _PROTECTED_SUFFIXES
        or (name.startswith("service-account") and name.endswith(".json"))
    )


# --- size cap -----------------------------------------------------------------
def tokens(value):
    """Estimated input tokens for a JSON value."""
    return (
        int(
            len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            / CHARS_PER_TOKEN
        )
        + 1
    )


def fit(state, questions, limit=MAX_REQUEST_TOKENS):
    """Return a copy of `state` truncated until state plus questions fit `limit`.

    Halves the longest list, then the longest string. Marks `truncated: true`.
    Raises JevError when nothing is left to cut.
    """
    state = copy.deepcopy(state)
    while tokens([state, questions]) > limit:
        lists = [k for k, v in state.items() if isinstance(v, list) and v]
        texts = [k for k, v in state.items() if isinstance(v, str) and len(v) > 40]
        if lists:
            k = max(lists, key=lambda key: len(json.dumps(state[key])))
            state[k] = state[k][: len(state[k]) // 2]
        elif texts:
            k = max(texts, key=lambda key: len(state[key]))
            state[k] = state[k][: len(state[k]) // 2]
        else:
            raise JevError("request_too_large: nothing left to truncate")
        state["truncated"] = True
    return state


# --- transport ----------------------------------------------------------------
def api_key(env=None, env_file=None):
    """The Jev key from the environment, else from `~/.env`; None when absent."""
    env = os.environ if env is None else env
    if env.get("TYPESAFE_API_KEY", "").strip():
        return env["TYPESAFE_API_KEY"].strip()
    path = Path(env_file) if env_file else Path.home() / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        m = re.fullmatch(r"\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*?)\s*", line)
        if m and m.group(1).strip("'\"").strip():
            return m.group(1).strip("'\"").strip()
    return None


def settings():
    """The `[jev]` table of harness.toml."""
    return harness_config.load(REPO)["jev"]


def disabled_reason():
    """Why Jev is off, or None."""
    if os.environ.get("JEV_DISABLE") == "1":
        return "JEV_DISABLE=1"
    if not settings()["enabled"]:
        return "[jev] enabled = false"
    return None


def evaluate(state, questions):
    """POST one request and return its validated `answers`. Raises JevError."""
    key = api_key()
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")
    state = fit(redact(state), questions)
    body = json.dumps(
        {"state": state, "model": MODEL, "questions": redact(questions)}
    ).encode("utf-8")
    request = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with _urlopen(request, timeout=settings()["timeout"]) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise JevError(
            f"HTTP {exc.code}"
        ) from exc  # status only: a body can echo the request
    except (OSError, ValueError) as exc:
        raise JevError(type(exc).__name__) from exc
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise JevError("response has no answers")
    for name, question in questions.items():
        _check_answer(name, question["type"], answers.get(name))
    return answers


def _check_answer(name, kind, answer):
    if not isinstance(answer, dict):
        raise JevError(f"{name}: no answer")
    if kind == "noul":
        if (
            isinstance(answer.get("noul"), bool)
            or not isinstance(answer.get("noul"), (int, float))
            or not 0 <= answer["noul"] <= 1
        ):
            raise JevError(f"{name}: noul is not a probability")
    else:
        probs = answer.get("probabilities")
        if (
            not isinstance(probs, dict)
            or not probs
            or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
                for v in probs.values()
            )
        ):
            raise JevError(f"{name}: choice has no probabilities")


def _noul(question, inspect):
    return {"type": "noul", "instructions": {"question": question, "inspect": inspect}}


def _choice(question, criteria):
    return {
        "type": "choice",
        "instructions": {"question": question},
        "criteria": {name: {"what": what} for name, what in criteria.items()},
    }


def _top(answer):
    """(option, probability) with the highest probability; ties go to the later (safer) option."""
    probs = answer["probabilities"]
    best = max(probs.values())
    return [name for name, p in probs.items() if p == best][-1], best


def _ask(questions, state):
    """(answers, None) on success, (None, reason) when Jev is off or failed."""
    reason = disabled_reason()
    if reason:
        return None, reason
    try:
        return evaluate(state, questions), None
    except JevError as exc:
        return None, str(exc)[:200]


# --- consequence --------------------------------------------------------------
RULES = (
    "Low: docs, formatting, mechanical. Medium: bounded deterministic logic. "
    "High: matching, identity, or logic that sets an answer, record, decision status or reported value. "
    "Critical: human-authority decisions, destructive writes, broad reconciliation."
)


def floor_tier(paths, tiers):
    """Highest tier any path reaches by the `[tiers]` globs; a path matching none, and an empty change, is Medium.

    Returns:
        (tuple): the tier name and the sorted paths that set it.
    """

    def rank(path):
        for tier, number in (("critical", 3), ("high", 2), ("medium", 1), ("low", 0)):
            if any(glob_match(g, path) for g in tiers[tier]):
                return number
        return 1

    ranks = {p: rank(p) for p in paths}
    top = max(ranks.values(), default=1)  # an empty change is Medium, never Low
    return TIERS[top], sorted(p for p, r in ranks.items() if r == top)


def consequence(paths, description, *, stats=None, symbols=None, tiers=None):
    """Rate a change Low, Medium, High or Critical.

    The code floor comes from path globs. Jev may raise the tier above it, never
    lower it. Acceptance follows docs/mutation-testing.md section 2, mapped to
    the two Jev answers: `tier` (Choice, with a probability per tier) and
    `alters_values` (Noul).

      - High or Critical at probability >= 0.6: accepted.
      - Low or Medium at probability >= 0.8 with alters_values <= 0.35: accepted
        (the independent reviewer still confirms the stated context).
      - Anything else: `status: "referred"`; `tier` is then the provisional
        max(floor, Jev's top tier) and the owner decides.
      - Jev off or failed: `status: "fallback"`, `tier` is the floor.

    Args:
        paths (list[str]): repository-relative changed paths.
        description (str): the task description, never file contents.
        stats (list[str] | None): diff stats such as "scripts/x.py +10 -2".
        symbols (list[str] | None): names of changed functions or classes.
        tiers (dict | None): `[tiers]` globs; defaults to harness.toml.

    Returns:
        (dict): tier, floor, floor_paths, jev_tier, probabilities, alters_values,
        status, source, reason, notes.
    """
    floor, floor_paths = floor_tier(paths, tiers or harness_config.load(REPO)["tiers"])
    out = {
        "decision": "consequence",
        "tier": floor,
        "floor": floor,
        "floor_paths": floor_paths,
        "jev_tier": None,
        "probabilities": {},
        "alters_values": None,
        "status": "fallback",
        "source": "fallback",
        "reason": None,
        "notes": [],
    }
    shown = [p for p in paths if not is_protected_path(p)]
    state = {
        "paths": shown,
        "stats": [
            st
            for st in (stats or [])
            if not any(is_protected_path(t) for t in st.split())
        ],
        "symbols": list(symbols or []),
        "description": (description or "")[:2000],
        "rules": RULES,
    }
    questions = {
        "tier": _choice(
            "What is the consequence tier of this change? Judge by what it can do to the "
            "project's conclusions, never by what a file is named.",
            {
                "Low": "docs, formatting, mechanical",
                "Medium": "bounded deterministic logic",
                "High": "matching, identity, or logic that sets an answer, record, decision "
                "status or reported value",
                "Critical": "human-authority decisions, destructive writes, broad reconciliation",
            },
        ),
        "alters_values": _noul(
            "Can this change alter an answer, record, decision or reported value?",
            "description",
        ),
    }
    answers, error = _ask(questions, state)
    if answers is None:
        out["reason"] = error
        return out
    probs = {t: float(answers["tier"]["probabilities"].get(t, 0.0)) for t in TIERS}
    jev_tier, p = _top({"probabilities": probs})
    alters = float(answers["alters_values"]["noul"])
    out.update(
        jev_tier=jev_tier,
        probabilities=probs,
        alters_values=alters,
        source="jev",
        tier=TIERS[max(TIERS.index(floor), TIERS.index(jev_tier))],
    )
    if jev_tier in ("High", "Critical") and p >= ACCEPT_HIGH:
        out["status"] = "accepted"
    elif jev_tier in ("Low", "Medium") and p >= ACCEPT_LOW and alters <= ALTERS_MAX:
        out["status"] = "accepted"
        out["notes"].append(
            "Low or Medium accepted on Jev's word: the independent reviewer must confirm the context"
        )
    else:
        out["status"] = "referred"
        out["reason"] = (
            f"Jev says {jev_tier} at {p:.2f} with alters_values {alters:.2f}; "
            f"outside {ACCEPT_HIGH} (High/Critical) and {ACCEPT_LOW}/{ALTERS_MAX} (Low/Medium)"
        )
    return out


# --- intent alignment -----------------------------------------------------------
_INTENT = {
    "has_actionable_outcome": (
        "Does `user_request` state an outcome that can be acted on now? "
        "Answer false only when no outcome can be inferred.",
        ["user_request"],
    ),
    "intent_preserves_requested_outcome": (
        "Does `proposed_work` preserve the outcome in `user_request` "
        "without changing its meaning?",
        ["user_request", "proposed_work"],
    ),
    "intent_preserves_explicit_constraints": (
        "Does `proposed_work` preserve every explicit constraint, "
        "limitation, authorization, and requested scope in `user_request`?",
        ["user_request", "proposed_work"],
    ),
    "work_supports_requested_outcome": (
        "Does `proposed_work` avoid conflicting with the outcome in `user_request`?",
        ["user_request", "proposed_work"],
    ),
    "intent_is_too_narrow": (
        "Does `proposed_work` drop a material deliverable, surface, state, or success "
        "condition from `user_request`?",
        ["user_request", "proposed_work"],
    ),
    "intent_adds_unrequested_work": (
        "Does `proposed_work` add a material goal, deliverable, or decision "
        "absent from `user_request`?",
        ["user_request", "proposed_work"],
    ),
    "work_omits_material_scope": (
        "Is `proposed_work` plainly unable to cover a material part of `user_request`?",
        ["user_request", "proposed_work"],
    ),
    "essential_clarification_needed": (
        "Would executing `proposed_work` require the user to resolve an "
        "essential ambiguity, contradiction, or explicitly reserved choice "
        "first? Routine implementation choices do not.",
        ["user_request", "proposed_work"],
    ),
    "can_proceed": (
        "Can work proceed from `proposed_work` because `user_request` is actionable and its "
        "outcome and constraints are preserved?",
        ["user_request", "proposed_work"],
    ),
}


def intent_alignment(request, proposed):
    """Advisory nine-question check that `proposed` still serves `request`.

    `drift` is computed here, not by Jev: true when the proposal adds unrequested
    work (>= 0.6), omits scope (too narrow or omits material scope, >= 0.6), or
    does not preserve the outcome (< 0.5).

    Returns:
        (dict): scores (the nine probabilities), drift, issues, status, source, reason.
    """
    out = {
        "decision": "intent_alignment",
        "drift": False,
        "scores": {},
        "probabilities": {},
        "issues": [],
        "status": "fallback",
        "source": "fallback",
        "reason": None,
    }
    state = {"user_request": request[:4000], "proposed_work": proposed[:4000]}
    questions = {key: _noul(q, inspect) for key, (q, inspect) in _INTENT.items()}
    answers, error = _ask(questions, state)
    if answers is None:
        out["reason"] = error
        return out
    s = {key: round(float(answers[key]["noul"]), 4) for key in _INTENT}
    adds = s["intent_adds_unrequested_work"] >= YES
    omits = s["intent_is_too_narrow"] >= YES or s["work_omits_material_scope"] >= YES
    changes = s["intent_preserves_requested_outcome"] < CORE
    issues = [
        msg
        for hit, msg in (
            (adds, "adds unrequested work"),
            (omits, "omits requested scope"),
            (changes, "does not preserve the requested outcome"),
            (
                s["intent_preserves_explicit_constraints"] < CORE,
                "drops an explicit constraint",
            ),
            (
                s["essential_clarification_needed"] >= YES,
                "needs an essential clarification",
            ),
        )
        if hit
    ]
    out.update(
        scores=s,
        probabilities=s,
        issues=issues,
        drift=adds or omits or changes,
        status="ok",
        source="jev",
    )
    return out


# --- reassess -------------------------------------------------------------------
def reassess(context):
    """Decide continue, change_approach or escalate for a loop that keeps failing.

    The floor is code: a fingerprint seen fewer than 3 times continues, 3 or more
    is at least change_approach (AGENTS.md "Stop and reassess"). Jev may raise the
    action (to escalate), never lower it, and only at probability >= 0.6;
    below that the result is `referred` at the floor.

    Args:
        context (dict): `fingerprint` (str), `attempts` (int >= 1, times this
            fingerprint was seen), `last_errors` (list of one-line strings).

    Returns:
        (dict): action, floor, jev_action, probabilities, status, source, reason.
    """
    attempts = context.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("reassess: attempts must be an integer >= 1")
    floor = ACTIONS[1] if attempts >= STOP_AT else ACTIONS[0]
    out = {
        "decision": "reassess",
        "action": floor,
        "floor": floor,
        "jev_action": None,
        "probabilities": {},
        "status": "fallback",
        "source": "fallback",
        "reason": None,
    }
    state = {
        "fingerprint": str(context.get("fingerprint", ""))[:120],
        "attempts": attempts,
        "last_errors": [
            str(e).splitlines()[0][:200] if str(e) else ""
            for e in context.get("last_errors", [])[-5:]
        ],
    }
    questions = {
        "action": _choice(
            "A repair loop keeps hitting the same failure. What should the agent do next?",
            {
                "continue": "the failure is new or the last repair was materially different, so another attempt is sound",
                "change_approach": "repeating the same repair will not work; a different approach is needed",
                "escalate": "the cause is outside the agent's reach or needs a decision only the owner can make",
            },
        )
    }
    answers, error = _ask(questions, state)
    if answers is None:
        out["reason"] = error
        return out
    probs = {a: float(answers["action"]["probabilities"].get(a, 0.0)) for a in ACTIONS}
    jev_action, p = _top({"probabilities": probs})
    out.update(
        jev_action=jev_action,
        probabilities=probs,
        source="jev",
        action=ACTIONS[max(ACTIONS.index(floor), ACTIONS.index(jev_action))]
        if p >= ACCEPT_HIGH
        else floor,
    )
    if p >= ACCEPT_HIGH:
        out["status"] = "accepted"
    else:
        out["status"] = "referred"
        out["reason"] = (
            f"Jev says {jev_action} at {p:.2f}, below {ACCEPT_HIGH}; kept the floor"
        )
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Ask Jev one typed question; prints JSON."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("consequence")
    c.add_argument("--path", action="append", default=[])
    c.add_argument("--description", default="")
    a = sub.add_parser("intent")
    a.add_argument("--request", required=True)
    a.add_argument("--proposed", required=True)
    r = sub.add_parser("reassess")
    r.add_argument("--fingerprint", required=True)
    r.add_argument("--attempts", type=int, required=True)
    r.add_argument("--error", action="append", default=[])
    args = parser.parse_args()
    if args.cmd == "consequence":
        result = consequence(args.path, args.description)
    elif args.cmd == "intent":
        result = intent_alignment(args.request, args.proposed)
    else:
        result = reassess(
            {
                "fingerprint": args.fingerprint,
                "attempts": args.attempts,
                "last_errors": args.error,
            }
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
