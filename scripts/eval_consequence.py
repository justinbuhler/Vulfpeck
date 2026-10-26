#!/usr/bin/env python3
"""Score the consequence planner (scripts/jev.py) against labelled real pull requests.

Each case in `evals/consequence/cases.jsonl` is a merged PR reduced to what the
planner sees: paths, diff stats, changed symbols and the description. Each row of
`labels.jsonl` is one labeller's tier for one case. The labeller `--gold` names
(default `gold`, an adjudicated set) is the reference. For every case the report
holds the code floor (`[tiers]` globs) and Jev's reading, replayed from its record.

What it measures:

  - under: gold above the floor. A raise-only Jev is the only thing that can fix
    these, so they bound its benefit. `--check` fails when there is one, which makes
    the labels a regression gate on the `[tiers]` table.
  - over: floor above gold. Extra checks Jev cannot remove by design.
  - Jev's own tier and the final tier (max of floor and Jev) against gold.
  - a sweep of ACCEPT_HIGH, ACCEPT_LOW and ALTERS_MAX through the acceptance rule
    `jev.accepted`, only when a case was answered.
  - agreement between every pair of labellers except the gold one: percent, and
    Cohen's kappa over the four tiers.

Offline by default: JEV_OFFLINE=1, so only recorded answers are used and a case
with none falls back to its floor (docs/mutation-testing.md "No live call by
default"). `--collect` is the only mode that sends, and a request that already has
a record is never sent again.

    python3 scripts/eval_consequence.py [--gold LABELLER] [--json] [--check]
    python3 scripts/eval_consequence.py --collect
"""

import argparse
import itertools
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
import jev  # noqa: E402
from paths import REPO  # noqa: E402

CASES = REPO / "evals" / "consequence" / "cases.jsonl"
LABELS = REPO / "evals" / "consequence" / "labels.jsonl"
TIERS = jev.TIERS
NOTE = "Labels are model or human as recorded in the labeller name; this report infers nothing."
SWEEP_STEP = 0.1  # the grid is each shipped threshold, one step below and one above


def natural(case_id):
    """Sort key putting pr-2 before pr-10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", case_id)]


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_cases(path):
    """The case rows sorted by id; a repeated id is an error."""
    cases = read_jsonl(path)
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path}: duplicate case id")
    return sorted(cases, key=lambda c: natural(c["id"]))


def load_labels(path, case_ids):
    """Labels as {labeller: {case id: tier}}; refuses an unknown tier or case and a repeat."""
    labels = {}
    for row in read_jsonl(path):
        who, cid, tier = row["labeller"], row["id"], row["tier"]
        if tier not in TIERS:
            raise SystemExit(f"{path}: unknown tier {tier!r} for {cid} by {who}")
        if cid not in case_ids:
            raise SystemExit(f"{path}: label for unknown case {cid!r} by {who}")
        if cid in labels.setdefault(who, {}):
            raise SystemExit(f"{path}: duplicate label for {cid} by {who}")
        labels[who][cid] = tier
    return labels


def compare(rows):
    """Confusion matrix of (case id, gold tier, predicted tier) rows, gold on the rows.

    Returns:
        (dict): matrix, exact, of, and the ids whose prediction is below (under)
        or above (over) gold.
    """
    matrix = {g: {p: 0 for p in TIERS} for g in TIERS}
    under, over = [], []
    for cid, gold, predicted in rows:
        matrix[gold][predicted] += 1
        if TIERS.index(predicted) < TIERS.index(gold):
            under.append(cid)
        elif TIERS.index(predicted) > TIERS.index(gold):
            over.append(cid)
    return {
        "matrix": matrix,
        "exact": sum(matrix[t][t] for t in TIERS),
        "of": len(rows),
        "under": sorted(under, key=natural),
        "over": sorted(over, key=natural),
    }


def agreement(a, b, labels_a, labels_b):
    """Percent agreement and Cohen's kappa for two labellers over the cases both labelled.

    Kappa is None when it is undefined: no shared case, or both labellers used one
    tier throughout (chance agreement is 1).
    """
    shared = sorted(set(labels_a) & set(labels_b), key=natural)
    out = {
        "a": a,
        "b": b,
        "shared": len(shared),
        "agree": 0,
        "percent": None,
        "kappa": None,
        "disagreements": [i for i in shared if labels_a[i] != labels_b[i]],
    }
    if not shared:
        return out
    n = len(shared)
    out["agree"] = n - len(out["disagreements"])
    out["percent"] = round(100 * out["agree"] / n, 1)
    chance = sum(
        (sum(labels_a[i] == t for i in shared) / n)
        * (sum(labels_b[i] == t for i in shared) / n)
        for t in TIERS
    )
    if chance != 1:
        out["kappa"] = round((out["agree"] / n - chance) / (1 - chance), 4)
    return out


def sweep(answered, gold_labels):
    """The acceptance rule at thresholds around the shipped ones, one row per grid point.

    `answered` is [(case id, consequence result)]. Thresholds change only whether
    Jev's reading is accepted or referred, never the final tier, so each row
    counts the accepted cases that have a gold label by whether the final tier
    matches gold, is below it (a miss) or above it.
    """

    def axis(value):
        return [round(value + step * SWEEP_STEP, 2) for step in (-1, 0, 1)]

    shipped = tuple(round(v, 2) for v in (jev.ACCEPT_HIGH, jev.ACCEPT_LOW, jev.ALTERS_MAX))
    rows = []
    for high, low, alters_max in itertools.product(
        axis(jev.ACCEPT_HIGH), axis(jev.ACCEPT_LOW), axis(jev.ALTERS_MAX)
    ):
        accepted = [
            (cid, r)
            for cid, r in answered
            if jev.accepted(
                r["jev_tier"],
                r["probabilities"][r["jev_tier"]],
                r["alters_values"],
                high,
                low,
                alters_max,
            )
        ]
        labelled = [
            (TIERS.index(r["tier"]), TIERS.index(gold_labels[cid]))
            for cid, r in accepted
            if cid in gold_labels
        ]
        rows.append(
            {
                "accept_high": high,
                "accept_low": low,
                "alters_max": alters_max,
                "shipped": (high, low, alters_max) == shipped,
                "accepted": len(accepted),
                "referred": len(answered) - len(accepted),
                "accepted_exact": sum(t == g for t, g in labelled),
                "accepted_under": sum(t < g for t, g in labelled),
                "accepted_over": sum(t > g for t, g in labelled),
            }
        )
    return rows


def build_report(cases, labels, gold, tiers):
    """Run every case through jev.consequence and assemble the report."""
    gold_labels = labels.get(gold, {})
    results = [
        (
            c["id"],
            jev.consequence(
                c["paths"],
                c["description"],
                stats=c["stats"],
                symbols=c["symbols"],
                tiers=tiers,
            ),
        )
        for c in cases
    ]
    labelled = [(cid, r) for cid, r in results if cid in gold_labels]
    answered = [(cid, r) for cid, r in results if r["source"] == "jev"]
    answered_labelled = [(cid, r) for cid, r in answered if cid in gold_labels]
    unlabelled = [cid for cid, _ in results if cid not in gold_labels]
    fallback_reasons = Counter(r["reason"] for _, r in results if r["source"] != "jev")
    others = sorted(set(labels) - {gold})
    return {
        "gold": gold,
        "note": NOTE,
        "cases": len(cases),
        "with_gold": len(labelled),
        "without_gold": len(unlabelled),
        "without_gold_ids": unlabelled,
        "floor_vs_gold": compare([(cid, gold_labels[cid], r["floor"]) for cid, r in labelled]),
        "jev": {
            "answered": len(answered),
            "fallback": len(results) - len(answered),
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "status": {
                s: sum(r["status"] == s for _, r in answered)
                for s in ("accepted", "referred")
            },
            "answered_labelled": len(answered_labelled),
            "jev_vs_gold": compare(
                [(cid, gold_labels[cid], r["jev_tier"]) for cid, r in answered_labelled]
            ),
            "final_vs_gold": compare(
                [(cid, gold_labels[cid], r["tier"]) for cid, r in answered_labelled]
            ),
        },
        "sweep": sweep(answered, gold_labels) if answered else None,
        "agreement": [
            agreement(a, b, labels[a], labels[b])
            for a, b in itertools.combinations(others, 2)
        ],
        "per_case": [
            {
                "id": cid,
                "gold": gold_labels.get(cid),
                "floor": r["floor"],
                "source": r["source"],
                "jev_tier": r["jev_tier"],
                "tier": r["tier"],
                "status": r["status"],
                "record": r["record"],
            }
            for cid, r in results
        ],
    }


def _ids(ids):
    return f"({', '.join(ids)})" if ids else "none"


def _matrix_lines(matrix, column):
    corner = "gold \\ " + column
    return [f"  {corner:<16}" + "".join(f"{t:>9}" for t in TIERS)] + [
        f"  {gold:<16}" + "".join(f"{matrix[gold][t]:>9}" for t in TIERS)
        for gold in TIERS
    ]


def render(rep):
    """The report as text; every list is already in a fixed order."""
    rows = {r["id"]: r for r in rep["per_case"]}
    floor = rep["floor_vs_gold"]
    jev_rep = rep["jev"]
    out = [
        f'consequence eval: {rep["cases"]} cases, gold labeller "{rep["gold"]}"',
        rep["note"],
        "",
        f'with a gold label: {rep["with_gold"]}',
        f'without a gold label: {rep["without_gold"]}'
        + (f' {_ids(rep["without_gold_ids"])}' if rep["without_gold_ids"] else ""),
        "",
        f'floor vs gold: {floor["exact"]} of {floor["of"]} exact',
        *_matrix_lines(floor["matrix"], "floor"),
        f'under (gold above floor): {len(floor["under"])} {_ids(floor["under"])}',
        "  the most a raise-only Jev can help",
        *[
            f'  {cid}  gold {rows[cid]["gold"]}, floor {rows[cid]["floor"]}'
            for cid in floor["under"]
        ],
        f'over (floor above gold): {len(floor["over"])} {_ids(floor["over"])}',
        "  extra checks Jev cannot remove by design",
        *[
            f'  {cid}  gold {rows[cid]["gold"]}, floor {rows[cid]["floor"]}'
            for cid in floor["over"]
        ],
        "",
        f'Jev: {jev_rep["answered"]} answered, {jev_rep["fallback"]} fallback',
    ]
    if jev_rep["fallback_reasons"]:
        out.append(
            "  fallback reasons: "
            + "; ".join(f"{k}: {v}" for k, v in jev_rep["fallback_reasons"].items())
        )
    out.append(
        f'  status: accepted {jev_rep["status"]["accepted"]}, referred {jev_rep["status"]["referred"]}'
    )
    for label, key, column in (
        ("jev_tier", "jev_vs_gold", "jev_tier"),
        ("final tier", "final_vs_gold", "final"),
    ):
        part = jev_rep[key]
        out += [
            f'{label} vs gold: {part["exact"]} of {part["of"]} exact, '
            f'under {_ids(part["under"])}, over {_ids(part["over"])}',
            *_matrix_lines(part["matrix"], column),
        ]
    out.append("")
    if rep["sweep"] is None:
        out.append("threshold sweep: skipped, no case was answered")
    else:
        out += [
            f'threshold sweep over the acceptance rule ({jev_rep["answered"]} answered; '
            "exact, under and over count accepted cases with a gold label; * is the shipped point)",
            "  high  low   alters  *  accepted  referred  exact  under  over",
            *[
                f'  {s["accept_high"]:.2f}  {s["accept_low"]:.2f}  {s["alters_max"]:.2f}  '
                f'{"*" if s["shipped"] else " "}  {s["accepted"]:>8}  {s["referred"]:>8}  '
                f'{s["accepted_exact"]:>5}  {s["accepted_under"]:>5}  {s["accepted_over"]:>4}'
                for s in rep["sweep"]
            ],
        ]
    out += ["", f'inter-labeller agreement (gold labeller "{rep["gold"]}" excluded)']
    for a in rep["agreement"]:
        percent = "n/a" if a["percent"] is None else f'{a["percent"]:.1f}%'
        kappa = "n/a" if a["kappa"] is None else f'{a["kappa"]:.4f}'
        out.append(
            f'  {a["a"]} vs {a["b"]}: {a["shared"]} shared, {percent} agree, '
            f'kappa {kappa}, disagree {", ".join(a["disagreements"]) or "none"}'
        )
    if not rep["agreement"]:
        out.append("  none: fewer than two labellers besides the gold one")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the consequence planner against labelled pull requests."
    )
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--gold", default="gold", help="labeller used as the reference")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="send requests that have no record (the default is offline)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a gold-labelled case has a gold tier above its floor",
    )
    args = parser.parse_args(argv)
    if args.collect:
        os.environ.pop("JEV_OFFLINE", None)
    else:
        os.environ["JEV_OFFLINE"] = "1"
    cases = load_cases(args.cases)
    labels = load_labels(args.labels, {c["id"] for c in cases})
    rep = build_report(
        cases, labels, args.gold, harness_config.load(REPO)["tiers"]
    )
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    if not args.check:
        return 0
    if not rep["with_gold"]:
        print(
            f'eval_consequence: --check passed vacuously: no case has a gold label from "{args.gold}"',
            file=sys.stderr,
        )
    return 1 if rep["floor_vs_gold"]["under"] else 0


if __name__ == "__main__":
    sys.exit(main())
