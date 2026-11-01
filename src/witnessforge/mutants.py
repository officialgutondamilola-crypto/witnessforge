"""Mutation operators: realistic faults derived from each mapping rule.

Every mutant is a full (layout, mapping) pair that differs from the specification
in exactly one rule-level fault, so a mutant that survives points at one rule.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Optional

from .copybook import Field, Layout
from .mapping import Mapping, Rule


@dataclass(frozen=True)
class Mutant:
    id: str
    rule_id: str
    operator: str
    description: str
    layout: Layout
    mapping: Mapping


OPERATORS = {
    "drop_step": "A transformation step is missing (e.g. no strip, no date conversion)",
    "case_flip": "upper() and lower() are swapped",
    "date_swap": "Day and month are swapped in the input date format",
    "truncate": "The value is cut one character short",
    "scale_loss": "The implied decimal (V) is ignored, so 123.45 becomes 12345",
    "sign_loss": "The sign of a signed numeric is dropped",
    "offset_shift": "The field is read one byte off its copybook offset",
    "wrong_source": "The rule reads a neighbouring field of the same type",
    "blank_as_empty": "Blank values become \"\" instead of null",
    "map_default": "Every code falls through to the lookup default",
    "array_drop_last": "The last OCCURS element is lost",
    "array_first_only": "Only the first OCCURS element is kept",
    "keep_blanks": "Blank OCCURS elements are not removed",
}


def _swap_day_month(fmt: str) -> Optional[str]:
    if "%d" not in fmt or "%m" not in fmt:
        return None
    return fmt.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m")


def _nearest_same_type(layout: Layout, field: Field) -> Optional[Field]:
    candidates = [
        f
        for f in layout.fields
        if f.name != field.name and f.kind == field.kind and f.occurs == field.occurs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.offset - field.offset))


def generate_mutants(layout: Layout, mapping: Mapping) -> List[Mutant]:
    mutants: List[Mutant] = []
    used_ids = set()

    def add(rule: Rule, operator: str, detail: str, new_rule: Optional[Rule] = None,
            new_layout: Optional[Layout] = None) -> None:
        base_id = f"{rule.id}:{operator}"
        mutant_id, n = base_id, 2
        while mutant_id in used_ids:
            mutant_id, n = f"{base_id}#{n}", n + 1
        used_ids.add(mutant_id)
        mutants.append(
            Mutant(
                id=mutant_id,
                rule_id=rule.id,
                operator=operator,
                description=detail,
                layout=new_layout or layout,
                mapping=mapping.with_rule(new_rule) if new_rule else mapping,
            )
        )

    for rule in mapping.rules:
        field = layout.field(rule.source)
        steps = list(rule.steps)
        ops = [op for op, _ in steps]

        for index, (op, arg) in enumerate(steps):
            dropped = tuple(steps[:index] + steps[index + 1 :])
            add(rule, "drop_step", f"step '{op}' removed", replace(rule, steps=dropped))
            if op in ("upper", "lower"):
                flipped = list(steps)
                flipped[index] = ("lower" if op == "upper" else "upper", None)
                add(rule, "case_flip", f"'{op}' replaced by its opposite",
                    replace(rule, steps=tuple(flipped)))
            if op == "date":
                swapped_fmt = _swap_day_month(arg["in"])
                if swapped_fmt:
                    swapped = list(steps)
                    swapped[index] = ("date", {"in": swapped_fmt, "out": arg["out"]})
                    add(rule, "date_swap", f"input format {arg['in']} read as {swapped_fmt}",
                        replace(rule, steps=tuple(swapped)))
            if op == "map" and arg["values"]:
                broken = list(steps)
                broken[index] = ("map", {"values": {}, "default": arg["default"]})
                add(rule, "map_default", "lookup table ignored", replace(rule, steps=tuple(broken)))

        if field.kind == "X" and field.item_length > 1:
            add(rule, "truncate", f"value cut to {field.item_length - 1} characters",
                replace(rule, steps=(("_truncate", field.item_length - 1),) + tuple(steps)))
        if field.is_numeric and field.scale > 0:
            add(rule, "scale_loss", f"{field.describe()} read with no implied decimal",
                new_layout=layout.with_field(field.name, int_digits=field.total_digits, scale=0))
        if field.is_numeric and field.signed:
            add(rule, "sign_loss", "sign dropped",
                replace(rule, steps=(("_abs", None),) + tuple(steps)))
        if field.offset + field.length < layout.record_length:
            add(rule, "offset_shift", f"read at offset {field.offset + 1} instead of {field.offset}",
                new_layout=layout.with_field(field.name, offset=field.offset + 1))
        if field.offset > 0:
            add(rule, "offset_shift", f"read at offset {field.offset - 1} instead of {field.offset}",
                new_layout=layout.with_field(field.name, offset=field.offset - 1))
        neighbour = _nearest_same_type(layout, field)
        if neighbour is not None:
            add(rule, "wrong_source", f"reads {neighbour.name} instead of {field.name}",
                replace(rule, source=neighbour.name))
        if "null_if_blank" in ops or "date" in ops:
            add(rule, "blank_as_empty", "blank becomes \"\" instead of null",
                replace(rule, steps=tuple(steps) + (("_blank_as_empty", None),)))
        if field.occurs > 1:
            add(rule, "array_drop_last", "last element lost", replace(rule, array_op="drop_last"))
            add(rule, "array_first_only", "only first element kept",
                replace(rule, array_op="first_only"))
            if rule.drop_blank:
                add(rule, "keep_blanks", "blank elements kept", replace(rule, drop_blank=False))

    return mutants
