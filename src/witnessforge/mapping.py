"""Mapping specifications: how each source field becomes a target JSON path.

A mapping is a YAML file::

    name: claim-header
    rules:
      - id: R1
        source: CLAIM-ID          # copybook field
        target: claim.id          # dotted JSON path in the output document
        steps: [strip]            # ordered transformation steps
      - id: R2
        source: SERVICE-DATE
        target: service.date
        steps:
          - date: {in: "%Y%m%d", out: "%Y-%m-%d"}

This module also holds the reference implementation of the mapping, which is
what WitnessForge treats as the specification of correct behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from .copybook import Field, Layout

PUBLIC_STEPS = {"strip", "rstrip", "upper", "lower", "number", "null_if_blank", "date", "map", "default"}
INTERNAL_STEPS = {"_truncate", "_abs", "_blank_as_empty"}  # used only by mutants
ARRAY_OPS = {None, "drop_last", "first_only"}

Step = Tuple[str, Any]


class MappingError(ValueError):
    """Raised for an invalid mapping specification or a failing transformation."""


@dataclass(frozen=True)
class Rule:
    id: str
    source: str
    target: str
    steps: Tuple[Step, ...] = ()
    drop_blank: bool = False
    array_op: Optional[str] = None  # internal, set only by array mutants


@dataclass(frozen=True)
class Mapping:
    name: str
    rules: Tuple[Rule, ...]
    key: Optional[str] = None  # id of the rule whose output identifies a document

    @property
    def key_rule(self) -> Optional[Rule]:
        return self.rule(self.key) if self.key else None

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    def with_rule(self, new_rule: Rule) -> "Mapping":
        return replace(
            self, rules=tuple(new_rule if r.id == new_rule.id else r for r in self.rules)
        )


def _normalize_step(rule_id: str, raw: Any) -> Step:
    if isinstance(raw, str):
        op, arg = raw, None
    elif isinstance(raw, dict) and len(raw) == 1:
        op, arg = next(iter(raw.items()))
    else:
        raise MappingError(f"{rule_id}: each step must be a name or a one-key mapping, got {raw!r}")
    if op not in PUBLIC_STEPS:
        raise MappingError(f"{rule_id}: unknown step {op!r} (known: {sorted(PUBLIC_STEPS)})")
    if op == "date" and not (isinstance(arg, dict) and "in" in arg and "out" in arg):
        raise MappingError(f"{rule_id}: date step needs 'in' and 'out' formats")
    if op == "map":
        if not (isinstance(arg, dict) and isinstance(arg.get("values"), dict)):
            raise MappingError(f"{rule_id}: map step needs a 'values' dictionary")
        arg = {"values": {str(k): v for k, v in arg["values"].items()}, "default": arg.get("default")}
    return op, arg


def mapping_from_dict(data: Dict[str, Any], layout: Optional[Layout] = None) -> Mapping:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise MappingError("Mapping must contain a non-empty 'rules' list")
    rules = []
    ids, targets = set(), set()
    for index, raw in enumerate(data["rules"], start=1):
        if not isinstance(raw, dict):
            raise MappingError(f"Rule #{index} must be a mapping")
        rule_id = str(raw.get("id") or f"R{index}")
        for key in ("source", "target"):
            if not raw.get(key):
                raise MappingError(f"{rule_id}: missing '{key}'")
        source, target = str(raw["source"]).upper(), str(raw["target"])
        if rule_id in ids:
            raise MappingError(f"Duplicate rule id {rule_id}")
        if target in targets:
            raise MappingError(f"{rule_id}: target {target} is mapped twice")
        if layout is not None and source not in layout.names():
            raise MappingError(f"{rule_id}: source field {source} is not in the copybook")
        steps = raw.get("steps") or []
        if not isinstance(steps, list):
            raise MappingError(f"{rule_id}: 'steps' must be a list")
        ids.add(rule_id)
        targets.add(target)
        rules.append(
            Rule(
                id=rule_id,
                source=source,
                target=target,
                steps=tuple(_normalize_step(rule_id, s) for s in steps),
                drop_blank=bool(raw.get("drop_blank", False)),
            )
        )
    key = data.get("key")
    if key is not None:
        key = str(key)
        if key not in ids:
            raise MappingError(f"key {key!r} is not a rule id")
        key_source = next(r.source for r in rules if r.id == key)
        if layout is not None and layout.field(key_source).occurs > 1:
            raise MappingError(f"key rule {key} cannot read an OCCURS field")
    return Mapping(name=str(data.get("name") or "mapping"), rules=tuple(rules), key=key)


def load_mapping(path, layout: Optional[Layout] = None) -> Mapping:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return mapping_from_dict(data, layout)


# --------------------------------------------------------------------------- reference engine


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def apply_step(value: Any, op: str, arg: Any, field: Field) -> Any:
    if op in ("strip", "rstrip", "upper", "lower"):
        return getattr(value, op)() if isinstance(value, str) else value
    if op == "null_if_blank":
        return None if _is_blank(value) else value
    if op == "number":
        if _is_blank(value):
            return None
        number = Decimal(value.strip() if isinstance(value, str) else value)
        if field.is_numeric and field.scale > 0:
            return float(number)
        return int(number) if number == number.to_integral_value() else float(number)
    if op == "date":
        if _is_blank(value):
            return None
        return datetime.strptime(str(value).strip(), arg["in"]).strftime(arg["out"])
    if op == "map":
        key = value.strip() if isinstance(value, str) else str(value)
        return arg["values"].get(key, arg["default"])
    if op == "default":
        return arg if value is None else value
    if op == "_truncate":
        return value[:arg] if isinstance(value, str) else value
    if op == "_abs":
        return abs(value) if isinstance(value, (Decimal, int, float)) else value
    if op == "_blank_as_empty":
        return "" if value is None else value
    raise MappingError(f"Unknown step {op}")


def _run_steps(value: Any, rule: Rule, field: Field) -> Any:
    for op, arg in rule.steps:
        value = apply_step(value, op, arg, field)
    return value


def _set_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise MappingError(f"Target path {path} collides with a value at {part}")
        node = child
    if parts[-1] in node:
        raise MappingError(f"Target path {path} is written twice")
    node[parts[-1]] = value


def apply_mapping(record: Dict[str, Any], mapping: Mapping, layout: Layout) -> Dict[str, Any]:
    """Transform one decoded source record into a target document."""
    doc: Dict[str, Any] = {}
    for rule in mapping.rules:
        field = layout.field(rule.source)
        raw = record[rule.source]
        if field.occurs > 1:
            items = [_run_steps(item, rule, field) for item in raw]
            if rule.drop_blank:
                items = [item for item in items if item not in (None, "")]
            if rule.array_op == "drop_last":
                items = items[:-1]
            elif rule.array_op == "first_only":
                items = items[:1]
            value: Any = items
        else:
            value = _run_steps(raw, rule, field)
        _set_path(doc, rule.target, value)
    return doc
