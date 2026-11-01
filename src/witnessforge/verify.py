"""Check a real pipeline's output for the witness records against the specification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .codec import split_records
from .copybook import Layout
from .mapping import Mapping
from .pipeline import json_ready, run_pipeline


@dataclass
class Mismatch:
    record: int
    path: str
    rule_id: Optional[str]
    expected: Any
    actual: Any


@dataclass
class VerifyResult:
    records: int
    mismatches: List[Mismatch] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.problems


_MISSING = object()


def _flatten(doc: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(doc, dict):
        out: Dict[str, Any] = {}
        for key, value in doc.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: doc}


def _same(expected: Any, actual: Any) -> bool:
    """Equal values of compatible types (12 == 12.0 is fine; "12" vs 12 or True vs 1 is not)."""
    numeric = (int, float)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, numeric) and isinstance(actual, numeric):
        return expected == actual
    return type(expected) is type(actual) and expected == actual


def _rule_for(path: str, mapping: Mapping) -> Optional[str]:
    best = None
    for rule in mapping.rules:
        if path == rule.target or path.startswith(rule.target + "."):
            if best is None or len(rule.target) > len(mapping.rule(best).target):
                best = rule.id
    return best


def verify(
    layout: Layout,
    mapping: Mapping,
    data: bytes,
    actual_docs: List[Dict[str, Any]],
    key: Optional[str] = None,
) -> VerifyResult:
    """Compare actual pipeline documents with the specification's expected documents.

    Documents are matched by the dotted ``key`` path (default: the target of the
    mapping's ``key`` rule), or by position when no key is available.
    """
    if key is None and mapping.key_rule is not None:
        key = mapping.key_rule.target
    records = split_records(layout, data)
    expected_docs = [json_ready(run_pipeline(r, layout, mapping)) for r in records]
    result = VerifyResult(records=len(records))

    if key:
        index: Dict[Any, Dict[str, Any]] = {}
        for doc in actual_docs:
            value = _flatten(doc).get(key)
            if value in index:
                result.problems.append(f"output key {key} = {value!r} appears more than once")
            index[value] = doc
        pairs = []
        for i, expected in enumerate(expected_docs):
            wanted = _flatten(expected).get(key)
            if wanted not in index:
                result.problems.append(f"record {i}: no output document with {key} = {wanted!r}")
                continue
            pairs.append((i, expected, index[wanted]))
    else:
        if len(actual_docs) != len(expected_docs):
            result.problems.append(
                f"expected {len(expected_docs)} output documents, got {len(actual_docs)}"
            )
        pairs = [(i, e, a) for i, (e, a) in enumerate(zip(expected_docs, actual_docs))]

    for i, expected, actual in pairs:
        exp_flat, act_flat = _flatten(expected), _flatten(actual)
        for path in sorted(set(exp_flat) | set(act_flat)):
            e, a = exp_flat.get(path, _MISSING), act_flat.get(path, _MISSING)
            if not _same(e, a):
                result.mismatches.append(
                    Mismatch(
                        record=i,
                        path=path,
                        rule_id=_rule_for(path, mapping),
                        expected=None if e is _MISSING else e,
                        actual="<missing>" if a is _MISSING else a,
                    )
                )
    return result
