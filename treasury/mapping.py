"""Classify a spreadsheet table onto a Dataset and map its columns.

Two layers, in order:

1. **Automatic** — score every dataset against the table's headers and sheet
   name; the best-scoring one wins if it clears a floor and its key fields
   resolved. Most exports land here with nothing to configure.
2. **Explicit** — a ``mapping.json`` beside the data pins file/sheet patterns to
   a dataset, overrides individual columns, and supplies constants for columns
   the sheet simply doesn't have (a deposits workbook with no "product" column).

The explicit layer always wins, and it is data, not code — onboarding a new
source never means editing Python.
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import excelio
from .excelio import Table, normalise_header
from .schema import DATASETS, Dataset, Field, dataset as get_dataset, normalise_value

# Bank exports abbreviate relentlessly: "Mat. Dt", "Notional Amt", "Cpty Type".
# Headers are matched twice — as written and with these expanded — so a synonym
# list stays readable instead of enumerating every contraction.
ABBREVIATIONS = {
    "mat": "maturity", "dt": "date", "amt": "amount", "ccy": "currency",
    "cur": "currency", "curr": "currency", "cpty": "counterparty",
    "cty": "country", "cntry": "country", "val": "value", "int": "interest",
    "pct": "percent", "bal": "balance", "mkt": "market", "sec": "security",
    "cust": "customer", "cust_no": "customer", "fac": "facility",
    "util": "utilised", "exp": "expiry", "mgr": "manager", "no": "number",
    "qty": "quantity", "nom": "nominal", "px": "price", "yr": "year",
    "eff": "effective", "sett": "settlement", "settl": "settlement",
    "cp": "counterparty", "id": "id", "ref": "reference", "desc": "description",
}

MAPPING_FILENAMES = ("mapping.json", "treasury_mapping.json")


@dataclass
class ColumnMap:
    """How one canonical field was resolved (kept for the audit panel)."""
    field: str
    header: str
    how: str            # exact | synonym | fuzzy | explicit | constant


@dataclass
class Mapping:
    dataset: str
    columns: Dict[str, str] = field(default_factory=dict)     # canonical -> header
    constants: Dict[str, Any] = field(default_factory=dict)
    trace: List[ColumnMap] = field(default_factory=list)
    score: float = 0.0
    unmapped: List[str] = field(default_factory=list)         # source headers ignored
    explicit: bool = False


# --- header resolution --------------------------------------------------------
def _candidates(f: Field) -> List[str]:
    return [f.name, *f.synonyms]


def expand(header: str) -> str:
    """``mat_date`` -> ``maturity_date``; unknown tokens pass through."""
    return "_".join(ABBREVIATIONS.get(token, token) for token in header.split("_"))


def _match(header: str, candidate: str) -> Optional[Tuple[float, str]]:
    if header == candidate:
        return (10.0, "exact")
    if header.startswith(candidate + "_") or header.endswith("_" + candidate):
        return (5.0, "fuzzy")
    if len(candidate) >= 5 and candidate in header:
        return (3.0, "fuzzy")
    return None


def _score_field(f: Field, headers: Sequence[str]) -> Optional[Tuple[float, str, str]]:
    """Best (score, header, how) for one canonical field, or None."""
    best: Optional[Tuple[float, str, str]] = None
    for header in headers:
        forms = [header]
        expanded = expand(header)
        if expanded != header:
            forms.append(expanded)
        for rank, candidate in enumerate(_candidates(f)):
            for index, form in enumerate(forms):
                hit = _match(form, candidate)
                if hit is None:
                    continue
                score, how = hit
                if rank:                       # a synonym scores just under the name
                    score = min(score, 8.0) - min(rank, 5) * 0.1
                    how = "synonym" if how == "exact" else how
                score -= index * 0.5           # prefer the header as actually written
                if best is None or score > best[0]:
                    best = (score, header, how)
                break
    return best


def resolve_columns(ds: Dataset, headers: Sequence[str]) -> Tuple[Dict[str, str], List[ColumnMap], float]:
    """Greedy best-first assignment so one header never fills two fields."""
    proposals = []
    for f in ds.fields:
        hit = _score_field(f, headers)
        if hit:
            proposals.append((hit[0], f.name, hit[1], hit[2]))
    proposals.sort(key=lambda p: (-p[0], p[1]))

    columns: Dict[str, str] = {}
    trace: List[ColumnMap] = []
    used: set = set()
    total = 0.0
    for score, field_name, header, how in proposals:
        if header in used or field_name in columns:
            continue
        columns[field_name] = header
        used.add(header)
        trace.append(ColumnMap(field_name, header, how))
        total += score
    return columns, trace, total


def _name_bonus(ds: Dataset, table: Table) -> float:
    haystack = f"{normalise_header(table.name)}_{normalise_header(os.path.basename(table.source))}"
    return sum(4.0 for hint in ds.name_hints if hint in haystack)


def _keys_present(ds: Dataset, columns: Dict[str, str]) -> bool:
    for key in ds.key_fields:
        if isinstance(key, (tuple, list)):
            if not any(k in columns for k in key):
                return False
        elif key not in columns:
            return False
    return True


def classify(table: Table, floor: float = 14.0) -> Optional[Mapping]:
    """Pick the dataset this table most likely is, or None if nothing fits."""
    best: Optional[Mapping] = None
    for ds in DATASETS:
        columns, trace, score = resolve_columns(ds, table.headers)
        if not _keys_present(ds, columns):
            continue
        # Reward coverage of the sheet's own columns: a table whose headers are
        # mostly understood is a better match than one with two lucky hits.
        coverage = len(columns) / max(len(table.headers), 1)
        total = score + _name_bonus(ds, table) + coverage * 10.0
        if total < floor:
            continue
        if best is None or total > best.score:
            best = Mapping(dataset=ds.name, columns=columns, trace=trace, score=total,
                           unmapped=[h for h in table.headers if h not in columns.values()])
    return best


# --- explicit profiles --------------------------------------------------------
@dataclass
class Profile:
    """One rule from ``mapping.json``."""
    dataset: str = ""
    file_pattern: str = "*"
    sheet_pattern: str = "*"
    columns: Dict[str, str] = field(default_factory=dict)
    constants: Dict[str, Any] = field(default_factory=dict)
    skip: bool = False

    def matches(self, table: Table) -> bool:
        filename = os.path.basename(table.source).lower()
        return (fnmatch.fnmatch(filename, self.file_pattern.lower())
                and fnmatch.fnmatch(table.name.lower(), self.sheet_pattern.lower()))


def load_profiles(root: str) -> List[Profile]:
    """Read ``mapping.json`` from a data folder (or the file's own folder)."""
    folder = root if os.path.isdir(root) else os.path.dirname(os.path.abspath(root))
    for filename in MAPPING_FILENAMES:
        path = os.path.join(folder, filename)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        profiles = []
        for entry in blob.get("sources", []):
            match = entry.get("match", {})
            profiles.append(Profile(
                dataset=entry.get("dataset", ""),
                file_pattern=match.get("file", "*"),
                sheet_pattern=match.get("sheet", "*"),
                columns={k: normalise_header(v) for k, v in entry.get("columns", {}).items()},
                constants=entry.get("constants", {}),
                skip=bool(entry.get("skip")),
            ))
        return profiles
    return []


def map_table(table: Table, profiles: Sequence[Profile] = ()) -> Optional[Mapping]:
    """Explicit profile first, automatic classification second."""
    for profile in profiles:
        if not profile.matches(table):
            continue
        if profile.skip:
            return None
        if not profile.dataset:
            continue
        ds = get_dataset(profile.dataset)
        columns, trace, score = resolve_columns(ds, table.headers)
        for field_name, header in profile.columns.items():
            columns[field_name] = header
            trace = [t for t in trace if t.field != field_name]
            trace.append(ColumnMap(field_name, header, "explicit"))
        return Mapping(dataset=ds.name, columns=columns, constants=dict(profile.constants),
                       trace=trace, score=score + 100.0, explicit=True,
                       unmapped=[h for h in table.headers if h not in columns.values()])
    return classify(table)


# --- coercion -----------------------------------------------------------------
def _coerce(f: Field, raw: Any, percent: bool = False, rate_unit: str = "auto") -> Any:
    """Convert one cell. ``percent`` = the column was percent-formatted in Excel.

    Interest rates are the one genuinely ambiguous case: a cell holding ``1.0``
    is 1% on most exports and 100% on a few. The rule, in order:
      * a percent-formatted cell, or text ending in ``%``, is already a fraction;
      * otherwise a plain number is read as percent (3.85 -> 0.0385);
      * ``rate_unit`` in settings.json forces either reading.
    """
    if f.kind == "number":
        value = excelio.to_number(raw)
        if value is None:
            return None
        if f.unit == "rate":
            already_fraction = (
                percent
                or (isinstance(raw, str) and "%" in raw)
                or rate_unit == "fraction"
            )
            if not already_fraction and rate_unit != "fraction":
                value /= 100.0
        return value
    if f.kind == "date":
        return excelio.to_date(raw)
    if f.kind == "bool":
        return excelio.to_bool(raw)
    text = excelio.to_text(raw)
    return normalise_value(f.enum, text) if f.enum else text


def apply(table: Table, mapping: Mapping, rate_unit: str = "auto") -> List[Dict[str, Any]]:
    """Coerced canonical records, each tagged with where it came from."""
    ds = get_dataset(mapping.dataset)
    origin = f"{os.path.basename(table.source)}:{table.name}"
    records: List[Dict[str, Any]] = []
    for index, row in enumerate(table.records):
        record: Dict[str, Any] = {}
        for f in ds.fields:
            if f.name in mapping.columns:
                header = mapping.columns[f.name]
                record[f.name] = _coerce(f, row.get(header),
                                         header in table.percent_columns, rate_unit)
            else:
                record[f.name] = None
        for field_name, constant in mapping.constants.items():
            f = ds.field(field_name)
            if f is not None and record.get(field_name) in (None, ""):
                record[field_name] = _coerce(f, constant, False, rate_unit)
        record["_origin"] = origin
        record["_row"] = table.header_row + index + 1
        records.append(record)
    return records
