"""Coverage reports (JSON and Markdown)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from . import __version__
from .witness import ForgeResult


def build_report(result: ForgeResult) -> Dict[str, Any]:
    killed = result.killed
    by_rule: Dict[str, List] = defaultdict(list)
    for mutant in result.mutants:
        by_rule[mutant.rule_id].append(mutant)

    rules = []
    for rule in result.mapping.rules:
        mutants = by_rule.get(rule.id, [])
        caught = [m for m in mutants if m.id in killed]
        rules.append(
            {
                "id": rule.id,
                "source": rule.source,
                "target": rule.target,
                "mutants": len(mutants),
                "killed": len(caught),
                "score": round(len(caught) / len(mutants), 4) if mutants else 1.0,
                "survivors": [
                    {"id": m.id, "operator": m.operator, "description": m.description}
                    for m in mutants
                    if m.id not in killed
                ],
            }
        )

    return {
        "tool": "witnessforge",
        "version": __version__,
        "mapping": result.mapping.name,
        "summary": {
            "record_length": result.layout.record_length,
            "fields": len(result.layout.fields),
            "rules": len(result.mapping.rules),
            "mutants": len(result.mutants),
            "killed": len(killed),
            "score": round(result.score, 4),
            "candidate_pool": result.pool_size,
            "witness_records": len(result.witnesses),
        },
        "rules": rules,
        "witnesses": [
            {"index": i, "kills": sorted(w.kills)} for i, w in enumerate(result.witnesses)
        ],
    }


def to_markdown(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# WitnessForge report: {report['mapping']}",
        "",
        f"**Mutation score: {s['score']:.0%}** ({s['killed']} of {s['mutants']} mapping faults detected)",
        "",
        f"- Witness records: **{s['witness_records']}** (selected from {s['candidate_pool']} candidates)",
        f"- Record length: {s['record_length']} bytes, {s['fields']} fields, {s['rules']} rules",
        "",
        "| Rule | Source | Target | Faults | Detected | Score |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["rules"]:
        lines.append(
            f"| {r['id']} | {r['source']} | `{r['target']}` | {r['mutants']} | {r['killed']} | {r['score']:.0%} |"
        )
    survivors = [(r["id"], x) for r in report["rules"] for x in r["survivors"]]
    lines += ["", "## Undetected faults", ""]
    if survivors:
        lines.append(
            "These faults change no output for any valid input the tool could build. "
            "Review each one: it is either an equivalent mutant (harmless) or a gap in the spec."
        )
        lines.append("")
        for rule_id, s_ in survivors:
            lines.append(f"- `{s_['id']}`: {s_['description']}")
    else:
        lines.append("None. Every generated fault is detected by the witness set.")
    lines.append("")
    return "\n".join(lines)
