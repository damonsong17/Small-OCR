"""A tiny, auditable first-match rule engine for regulatory factor tables.

Run-off rates, ASF/RSF factors and HQLA haircuts are **data**, not code: they
live in ``treasury/regs/*.json``, they differ by jurisdiction, and they change
when the regulator says so. Every weighted number the dashboard shows can name
the rule that produced it, so a reviewer can trace a figure back to a line in a
JSON file rather than into Python.

Condition grammar (all keys must hold — implicit AND)::

    "when": {
      "side": "LIABILITY",                 exact match (case-insensitive)
      "counterparty_type": ["FI", "SME"],  membership
      "operational": true,                 boolean
      "residual_days": {"lte": 30},        lte / lt / gte / gt
      "product": {"not": ["REPO"]},        negation
      "rating": null                       field must be empty
    }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

REGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regs")


@dataclass
class Rule:
    id: str
    label: str
    factor: float
    when: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def matches(self, row: Dict[str, Any]) -> bool:
        return all(_test(row.get(key), condition) for key, condition in self.when.items())


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().upper()
    return value


def _test(value: Any, condition: Any) -> bool:
    if condition is None:
        return value in (None, "", 0) or value is False
    if isinstance(condition, bool):
        return bool(value) is condition
    if isinstance(condition, (int, float)) and not isinstance(condition, bool):
        return value is not None and float(value) == float(condition)
    if isinstance(condition, str):
        return _norm(value) == _norm(condition)
    if isinstance(condition, list):
        return _norm(value) in {_norm(c) for c in condition}
    if isinstance(condition, dict):
        for op, operand in condition.items():
            if op == "not":
                if _test(value, operand):
                    return False
                continue
            if op == "any":
                if not any(_test(value, c) for c in operand):
                    return False
                continue
            if op == "present":
                if bool(value not in (None, "")) is not bool(operand):
                    return False
                continue
            if value is None:
                return False
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            if op == "lte" and not number <= operand:
                return False
            if op == "lt" and not number < operand:
                return False
            if op == "gte" and not number >= operand:
                return False
            if op == "gt" and not number > operand:
                return False
        return True
    return False


@dataclass
class RuleSet:
    name: str
    rules: List[Rule] = field(default_factory=list)
    default_factor: float = 0.0
    default_label: str = "Unmatched"

    def match(self, row: Dict[str, Any]) -> Rule:
        for rule in self.rules:
            if rule.matches(row):
                return rule
        return Rule(id="unmatched", label=self.default_label,
                    factor=self.default_factor,
                    note="no rule matched — review the factor table")

    def apply(self, row: Dict[str, Any], amount: float) -> Dict[str, Any]:
        rule = self.match(row)
        return {
            "rule_id": rule.id, "rule": rule.label, "factor": rule.factor,
            "amount": amount, "weighted": amount * rule.factor,
        }


@dataclass
class FactorTable:
    """One regulation file: several named rule sets plus its own parameters."""
    name: str
    version: str
    source: str
    params: Dict[str, Any] = field(default_factory=dict)
    sets: Dict[str, RuleSet] = field(default_factory=dict)

    def rules(self, key: str) -> RuleSet:
        if key not in self.sets:
            raise KeyError(f"{self.name}: no rule set {key!r}; have {sorted(self.sets)}")
        return self.sets[key]


def _ruleset(name: str, blob: Any) -> RuleSet:
    if isinstance(blob, dict):
        entries = blob.get("rules", [])
        default = float(blob.get("default_factor", 0.0))
        default_label = blob.get("default_label", "Unmatched")
    else:
        entries, default, default_label = blob, 0.0, "Unmatched"
    rules = [
        Rule(id=e.get("id", f"{name}_{i}"), label=e.get("label", e.get("id", "")),
             factor=float(e.get("factor", 0.0)), when=e.get("when", {}),
             note=e.get("note", ""))
        for i, e in enumerate(entries)
    ]
    return RuleSet(name=name, rules=rules, default_factor=default,
                   default_label=default_label)


def load_table(path: str) -> FactorTable:
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    sets = {
        key: _ruleset(key, value)
        for key, value in blob.items()
        if key not in {"name", "version", "source", "params", "_note"}
    }
    return FactorTable(
        name=blob.get("name", os.path.basename(path)),
        version=blob.get("version", ""),
        source=blob.get("source", ""),
        params=blob.get("params", {}),
        sets=sets,
    )


def load_regs(name: str, search: Sequence[str] = ()) -> FactorTable:
    """Load ``<name>.json``; a folder in ``search`` shadows the built-in copy."""
    filename = name if name.endswith(".json") else f"{name}.json"
    for folder in [*search, REGS_DIR]:
        if not folder:
            continue
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            return load_table(path)
    raise FileNotFoundError(f"no factor table {filename!r} in {[*search, REGS_DIR]}")


def available(search: Sequence[str] = ()) -> List[str]:
    names = []
    for folder in [*search, REGS_DIR]:
        if folder and os.path.isdir(folder):
            names += [f[:-5] for f in sorted(os.listdir(folder)) if f.endswith(".json")]
    return sorted(set(names))
