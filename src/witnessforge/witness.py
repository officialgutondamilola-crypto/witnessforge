"""Witness generation and minimization.

1. Build a pool of candidate source records. Every candidate is valid for the
   copybook by construction (it is produced by the encoder) and valid for the
   specification (the reference pipeline must transform it without error).
2. Run the reference pipeline and every mutant on every candidate. A candidate
   "kills" a mutant when the two outputs differ.
3. Select the smallest set of candidates that kills every killable mutant
   (greedy set cover). Those are the witness records.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Set

from .codec import CodecError, decode_record, encode_record
from .copybook import Field, Layout
from .mapping import Mapping, Rule
from .mutants import Mutant, generate_mutants
from .pipeline import canonical, is_error, run_pipeline

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SAMPLE_DATES = (date(2024, 1, 31), date(2024, 3, 5), date(1999, 12, 9))


def _rotated_text(length: int, seed: int) -> str:
    shift = seed % len(_ALPHABET)
    letters = _ALPHABET[shift:] + _ALPHABET[:shift]
    return (letters * (length // len(letters) + 1))[:length]


def _numeric_pattern(field: Field, seed: int) -> Decimal:
    whole = "".join(str((seed + i) % 9 + 1) for i in range(field.int_digits)) or "0"
    frac = "".join(str((seed + 4 + i) % 9 + 1) for i in range(field.scale))
    return Decimal(f"{whole}.{frac}" if frac else whole)


def _dedupe(values: Sequence[Any]) -> List[Any]:
    seen, out = set(), []
    for value in values:
        key = repr(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def item_candidates(field: Field, rules: Sequence[Rule], seed: int) -> List[Any]:
    """Candidate values for one occurrence of a field; the first is the primary value."""
    if field.kind == "X":
        size = field.item_length
        special: List[str] = []
        for rule in rules:
            for op, arg in rule.steps:
                if op == "date":
                    special += [d.strftime(arg["in"]) for d in _SAMPLE_DATES]
                elif op == "map":
                    special += list(arg["values"])
                    special.append(_rotated_text(size, seed + 11))  # an unmapped code
        primary = _rotated_text(size, seed)
        generic = [primary, primary.lower(), _rotated_text(max(1, size // 2), seed + 7), ""]
        values = special + generic
        return _dedupe([v for v in values if len(v) <= size])

    quantum = Decimal(1).scaleb(-field.scale)
    pattern = _numeric_pattern(field, seed).quantize(quantum)
    largest = (Decimal(10) ** field.int_digits - quantum).quantize(quantum)
    smallest = (Decimal(1) if field.int_digits else quantum).quantize(quantum)
    values = [pattern, Decimal(0).quantize(quantum), smallest, largest]
    if field.signed:
        values.insert(1, -pattern)
    return _dedupe(values)


def value_candidates(field: Field, rules: Sequence[Rule], seed: int) -> List[Any]:
    """Candidate values for a whole field (lists for OCCURS fields)."""
    items = item_candidates(field, rules, seed)
    if field.occurs == 1:
        return items
    blank: Any = "" if field.kind == "X" else Decimal(0).quantize(Decimal(1).scaleb(-field.scale))
    distinct = [item_candidates(field, rules, seed + 5 * (i + 1))[0] for i in range(field.occurs)]
    lists = [distinct, distinct[:-1] + [blank], [blank] * field.occurs]
    lists += [[item] * field.occurs for item in items]
    return _dedupe(lists)


def _uniquify(field: Field, value: Any, index: int) -> Any:
    """Make a key value unique per record while keeping its shape (case, padding, sign)."""
    if field.kind == "X":
        core = str(value or "").rstrip()
        if not core.strip():
            return None  # blank keys are not usable
        tag = f"{index:03d}"
        if len(core) > len(tag):
            return core[: len(core) - len(tag)] + tag
        return (core + tag)[: field.item_length]
    return value + index if value >= 0 else value - index


@dataclass
class Candidate:
    record: Dict[str, Any]
    data: bytes
    expected: Dict[str, Any]
    kills: Set[str] = dc_field(default_factory=set)


@dataclass
class ForgeResult:
    layout: Layout
    mapping: Mapping
    mutants: List[Mutant]
    witnesses: List[Candidate]
    survivors: List[Mutant]
    pool_size: int

    @property
    def killed(self) -> Set[str]:
        return set().union(*(w.kills for w in self.witnesses)) if self.witnesses else set()

    @property
    def score(self) -> float:
        return len(self.killed) / len(self.mutants) if self.mutants else 1.0


def build_pool(layout: Layout, mapping: Mapping) -> List[Candidate]:
    rules_by_field: Dict[str, List[Rule]] = defaultdict(list)
    for rule in mapping.rules:
        rules_by_field[rule.source].append(rule)

    candidates = {
        f.name: value_candidates(f, rules_by_field[f.name], seed=index * 7)
        for index, f in enumerate(layout.fields)
    }
    base = {name: values[0] for name, values in candidates.items()}
    records: List[Dict[str, Any]] = [base]
    width = max(len(values) for values in candidates.values())
    for k in range(1, width):  # "rich" records that change every field at once
        records.append({name: values[k % len(values)] for name, values in candidates.items()})
    for name, values in candidates.items():  # one-field variations of the base record
        for value in values[1:]:
            variant = dict(base)
            variant[name] = value
            records.append(variant)

    key_field = layout.field(mapping.key_rule.source) if mapping.key_rule else None
    pool: List[Candidate] = []
    seen = set()
    used_keys = set()
    for index, record in enumerate(records):
        if key_field is not None:
            record = dict(record)
            record[key_field.name] = _uniquify(key_field, record[key_field.name], index)
            if record[key_field.name] is None or record[key_field.name] in used_keys:
                continue
        try:
            data = encode_record(layout, record)
        except CodecError:
            continue
        if data in seen:
            continue
        expected = run_pipeline(data, layout, mapping)
        if is_error(expected):  # input the specification itself rejects
            continue
        seen.add(data)
        if key_field is not None:
            used_keys.add(record[key_field.name])
        pool.append(Candidate(decode_record(layout, data), data, expected))
    return pool


def forge(layout: Layout, mapping: Mapping) -> ForgeResult:
    """Generate the minimal witness set for a copybook and mapping."""
    mutants = generate_mutants(layout, mapping)
    pool = build_pool(layout, mapping)
    for candidate in pool:
        reference = canonical(candidate.expected)
        for mutant in mutants:
            if canonical(run_pipeline(candidate.data, mutant.layout, mutant.mapping)) != reference:
                candidate.kills.add(mutant.id)

    remaining = set().union(*(c.kills for c in pool)) if pool else set()
    selected: List[Candidate] = []
    while remaining:
        best = max(pool, key=lambda c: len(c.kills & remaining))
        gained = best.kills & remaining
        if not gained:
            break
        selected.append(best)
        remaining -= gained

    killed = set().union(*(c.kills for c in selected)) if selected else set()
    survivors = [m for m in mutants if m.id not in killed]
    return ForgeResult(layout, mapping, mutants, selected, survivors, len(pool))
