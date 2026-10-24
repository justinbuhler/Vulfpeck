"""Rule 11: tracked .claude/workflows/*.js agent() calls route through ROLE.

This test owns its own expectation tables. They are hand-written literals,
never read, imported or derived from the workflow files' own MODEL/ROLE
tables - deriving them would make the test agree with whatever the workflow
says, which is the failure this control exists to prevent.

Adding a workflow means adding it to EXPECTED_CALLS and its call sites to
EXPECTED_CALL_ROLES here, by hand. MODEL-00 fails until you do.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / ".claude" / "workflows"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (("\n          " + detail) if detail and not cond else "")
    )


EXPECTED_CALL_ROLES = {
    ("review-changes.js", "`review:${d.key}`"): "REVIEWER",
    ("review-changes.js", "`verify:${f.file}`"): "VERIFIER",
}

EXPECTED_ROLE_CLASS = {
    "REVIEWER": "REASONING_STRONG",
    "VERIFIER": "REASONING_STRONG",
}

APPROVED_MODELS = {"REASONING_STRONG": "opus", "MECHANICAL": "haiku"}
EXPECTED_CALLS = {"review-changes.js": 2}

# The label value following `label:` - a backtick template, a single-quoted
# string, or a bare dotted identifier (`j.label`). NOT `[^,\n]+`: when label is
# the last key in a single-line literal, that swallows the closing `}`.
_LABEL_VALUE = r"`[^`]*`|'[^']*'|[\w.]+"


def routing_violations(fname, src):
    """Every way this workflow's agent() routing can be wrong, as strings.

    Each violation string leads with the marker of the control it caught, so a
    mutation test can assert WHICH control fired, not just that one did:
      MODEL-01  call-site structure - every tracked agent() call spreads
                exactly one approved ROLE.*
      MODEL-02  role definition structure - every ROLE resolves to exactly one
                approved MODEL capability, and no call site names a model directly
      MODEL-03A independent call-site -> role expectation
      MODEL-03B independent role -> model-class expectation
    """
    v = []
    call_count = len(re.findall(r"\bagent\(", src))
    if call_count != EXPECTED_CALLS[fname]:
        v.append(
            f"MODEL-01: {fname}: {call_count} agent() call(s), expected {EXPECTED_CALLS[fname]}"
        )

    spread_count = len(re.findall(r"\.\.\.ROLE\.", src))
    if spread_count != call_count:
        v.append(
            f"MODEL-01: {fname}: {spread_count} ...ROLE. spread(s), expected {call_count} (one per agent() call)"
        )

    pairs = re.findall(rf"\.\.\.ROLE\.([A-Z_]+)\s*,\s*label:\s*({_LABEL_VALUE})", src)
    if len(pairs) != call_count:
        v.append(
            f"MODEL-01: {fname}: {len(pairs)} spread/label pair(s), expected {call_count}"
        )

    actual_roles = {(fname, label.strip()): role for role, label in pairs}
    expected_roles = {k: r for k, r in EXPECTED_CALL_ROLES.items() if k[0] == fname}
    for key in sorted(set(actual_roles) | set(expected_roles)):
        exp, act = expected_roles.get(key), actual_roles.get(key)
        if exp == act:
            continue
        _, label = key
        if act is None:
            v.append(
                f"MODEL-03A: {fname} {label} expected {exp}, but no tracked call site was found for it"
            )
        elif exp is None:
            v.append(
                f"MODEL-03A: {fname} {label} is not a tracked call site (spreads role {act})"
            )
        else:
            v.append(f"MODEL-03A: {fname} {label} expected {exp}, found {act}")

    role_table = dict(re.findall(r"(\w+):\s*\{\s*model:\s*MODEL\.(\w+)\s*\}", src))

    orphan_spreads = {role for role, _ in pairs} - set(role_table)
    if orphan_spreads:
        v.append(
            f"MODEL-02: {fname}: spread(s) name role(s) missing from this file's own ROLE table: {sorted(orphan_spreads)}"
        )

    for role, cls in role_table.items():
        if role not in EXPECTED_ROLE_CLASS:
            v.append(
                f"MODEL-03B: {fname}: ROLE.{role} is not a role this test recognises"
            )
        elif EXPECTED_ROLE_CLASS[role] != cls:
            v.append(
                f"MODEL-03B: {role} expected {EXPECTED_ROLE_CLASS[role]}, found {cls}"
            )

    model_block = re.search(r"const MODEL = Object\.freeze\(\{(.*?)\}\)", src, re.S)
    model_table = (
        dict(re.findall(r"(\w+):\s*'([^']+)'", model_block.group(1)))
        if model_block
        else {}
    )
    if model_table != APPROVED_MODELS:
        v.append(
            f"MODEL-02: {fname}: MODEL table is {model_table}, approved is {APPROVED_MODELS}"
        )

    bad_models = [
        m.group(1)
        for m in re.finditer(r"model:\s*(\S+?)[\s,}]", src)
        if not re.match(r"MODEL\.(REASONING_STRONG|MECHANICAL)$", m.group(1))
    ]
    if bad_models:
        v.append(
            f"MODEL-02: {fname}: model: value(s) not routed through MODEL.*: {bad_models}"
        )

    model_kw_count = len(re.findall(r"\bmodel:\s*", src))
    if model_kw_count != len(role_table):
        v.append(
            f"MODEL-02: {fname}: {model_kw_count} 'model:' occurrence(s) in the file, "
            f"expected {len(role_table)} (one per this file's own ROLE entry)"
        )

    return v


print("MODEL routing: tracked workflow agent() calls take their model from a ROLE")

# Enforcement is over the DIRECTORY, not over this literal: a new workflow must
# be explicitly routed and explicitly expected, or this fails. is_file() rather
# than the glob name alone, so a broken symlink or a directory named *.js
# reports as missing instead of passing coverage.
_named = {p.name for p in WF_DIR.glob("*.js")}
_usable = {p.name for p in WF_DIR.glob("*.js") if p.is_file()}
_unenforced = sorted(_named - set(EXPECTED_CALLS))
_missing = sorted(set(EXPECTED_CALLS) - _usable)
check(
    "MODEL-00: every workflow in .claude/workflows/ is covered by the expectation table",
    not _unenforced and not _missing,
    f"MODEL-00: workflow(s) present but unenforced: {_unenforced}; "
    f"expected but absent from disk: {_missing}",
)

for _fname in EXPECTED_CALLS:
    if not (WF_DIR / _fname).is_file():
        continue  # MODEL-00 already failed on it
    _v = routing_violations(_fname, (WF_DIR / _fname).read_text())
    check(f"MODEL routing: {_fname} has no control violations", not _v, "; ".join(_v))


# --- MODEL-04: known bypasses must be rejected, permanently -------------------
# A str.replace whose target has moved silently returns the source unchanged,
# which would make the check pass while proving nothing. So each assertion
# requires the mutation to have changed the text AND that the flag came from the
# SPECIFIC control the mutation is supposed to defeat.
def _mutation_rejected(fname, mutate, expected_marker):
    if not (WF_DIR / fname).is_file():
        return (
            False,
            f"MODEL-00: {fname} is expected but absent from disk - mutation not runnable",
        )
    src = (WF_DIR / fname).read_text()
    mutated = mutate(src)
    applied = mutated != src
    violations = routing_violations(fname, mutated) if applied else []
    fired = [vio for vio in violations if vio.startswith(expected_marker + ":")]
    if not applied:
        detail = f"mutation did not change {fname} - its str.replace target was not found (moved or missing)"
    elif not violations:
        detail = f"mutation changed {fname} but routing_violations() found nothing wrong with it"
    elif not fired:
        detail = f"expected {expected_marker} to fire but it did not - saw instead: {'; '.join(violations)}"
    else:
        detail = ""
    return applied and bool(fired), detail


WF = "review-changes.js"
check(
    "MODEL-04: a call site re-pointed at another role is rejected",
    *_mutation_rejected(
        WF,
        lambda s: s.replace(
            "{ ...ROLE.VERIFIER, label: `verify:${f.file}`",
            "{ ...ROLE.REVIEWER, label: `verify:${f.file}`",
        ),
        "MODEL-03A",
    ),
)
check(
    "MODEL-04: a role downgraded from reasoning to mechanical is rejected",
    *_mutation_rejected(
        WF,
        lambda s: s.replace(
            "VERIFIER: { model: MODEL.REASONING_STRONG }",
            "VERIFIER: { model: MODEL.MECHANICAL }",
        ),
        "MODEL-03B",
    ),
)
check(
    "MODEL-04: an agent() call with no ROLE spread is rejected",
    *_mutation_rejected(
        WF,
        lambda s: s.replace(
            "{ ...ROLE.REVIEWER, label: `review:${d.key}`", "{ label: `review:${d.key}`"
        ),
        "MODEL-01",
    ),
)
check(
    "MODEL-04: a model named directly at a call site is rejected",
    *_mutation_rejected(
        WF,
        lambda s: s.replace(
            "{ ...ROLE.REVIEWER, label: `review:${d.key}`",
            "{ ...ROLE.REVIEWER, model: 'sonnet', label: `review:${d.key}`",
        ),
        "MODEL-02",
    ),
)
check(
    "MODEL-04: a MODEL table re-pointed at another model is rejected",
    *_mutation_rejected(
        WF,
        lambda s: s.replace("REASONING_STRONG: 'opus'", "REASONING_STRONG: 'sonnet'"),
        "MODEL-02",
    ),
)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
