"""Panel building blocks.

A metric returns data, never markup. These helpers produce the small JSON
shapes the front end knows how to draw, so every panel looks like it came from
the same place and a new metric inherits the styling for free.

Formats (the browser does the formatting, the server sends numbers):
``money`` base-currency amount · ``money_ccy`` amount in the row's own currency
· ``ratio`` 1.23x · ``pct`` 0.05 -> 5.0% · ``bps`` · ``rate`` 0.0385 -> 3.850%
· ``days`` · ``number`` · ``date`` · ``text``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

STATUSES = ("good", "warning", "serious", "critical", "neutral")


# --- KPIs ---------------------------------------------------------------------
def kpi(label: str, value: Any, format: str = "money", status: str = "neutral",
        delta: Optional[float] = None, delta_label: str = "",
        delta_good: str = "up", hint: str = "") -> Dict[str, Any]:
    return {
        "label": label, "value": value, "format": format, "status": status,
        "delta": delta, "delta_label": delta_label, "delta_good": delta_good,
        "hint": hint,
    }


def hero(label: str, value: Any, format: str = "ratio", status: str = "neutral",
         caption: str = "") -> Dict[str, Any]:
    return {"label": label, "value": value, "format": format,
            "status": status, "caption": caption}


def status_for(value: Optional[float], warn: float, floor: float,
               higher_is_better: bool = True) -> str:
    """Traffic light against a limit and a warning level."""
    if value is None:
        return "neutral"
    if higher_is_better:
        if value < floor:
            return "critical"
        return "good" if value >= warn else "warning"
    if value > floor:
        return "critical"
    return "good" if value <= warn else "warning"


# --- charts -------------------------------------------------------------------
def _series(name: str, values: Sequence[Any], slot: int = 1,
            role: str = "") -> Dict[str, Any]:
    return {"name": name, "values": list(values), "slot": slot, "role": role}


def series(name: str, values: Sequence[Any], slot: int = 1,
           role: str = "") -> Dict[str, Any]:
    """One named line/bar series. ``slot`` is a categorical palette slot (1-8);
    ``role`` overrides it with a status colour (good/critical/...)."""
    return _series(name, values, slot, role)


def _chart(kind: str, title: str, categories: Sequence[Any],
           series_list: Sequence[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    spec = {
        "type": kind, "title": title, "categories": list(categories),
        "series": list(series_list), "format": extra.pop("format", "money"),
        "axis_label": extra.pop("axis_label", ""),
        "reference": extra.pop("reference", []),
        "note": extra.pop("note", ""),
    }
    spec.update(extra)
    return spec


def bar(title: str, categories: Sequence[Any], series_list: Sequence[Dict[str, Any]],
        stacked: bool = False, **extra: Any) -> Dict[str, Any]:
    """Vertical columns. Diverging values (a gap ladder) render around zero."""
    return _chart("bar", title, categories, series_list, stacked=stacked, **extra)


def hbar(title: str, categories: Sequence[Any], series_list: Sequence[Dict[str, Any]],
         **extra: Any) -> Dict[str, Any]:
    """Horizontal bars — the right form for ranked names (top depositors)."""
    return _chart("hbar", title, categories, series_list, **extra)


def line(title: str, categories: Sequence[Any], series_list: Sequence[Dict[str, Any]],
         **extra: Any) -> Dict[str, Any]:
    return _chart("line", title, categories, series_list, **extra)


def waterfall(title: str, categories: Sequence[str], values: Sequence[float],
              kinds: Sequence[str] = (), **extra: Any) -> Dict[str, Any]:
    """Bridge chart. ``kinds`` marks each step ``delta`` (default) or ``total``."""
    spec = _chart("waterfall", title, categories,
                  [_series("value", values)], **extra)
    spec["kinds"] = list(kinds) or ["delta"] * len(values)
    return spec


def reference(value: float, label: str, role: str = "critical") -> Dict[str, Any]:
    """A threshold rule drawn across a chart (a regulatory minimum, a limit)."""
    return {"value": value, "label": label, "role": role}


# --- tables -------------------------------------------------------------------
def col(key: str, label: str, format: str = "text", width: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "format": format, "width": width}


def table(title: str, columns: Sequence[Dict[str, Any]],
          rows: Sequence[Dict[str, Any]], total: Optional[Dict[str, Any]] = None,
          note: str = "", sort: str = "") -> Dict[str, Any]:
    return {"title": title, "columns": list(columns), "rows": list(rows),
            "total": total, "note": note, "sort": sort}


# --- small numeric helpers ----------------------------------------------------
def safe_div(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def round_all(rows: Sequence[Dict[str, Any]], keys: Sequence[str],
              places: int = 2) -> List[Dict[str, Any]]:
    for row in rows:
        for key in keys:
            if isinstance(row.get(key), float):
                row[key] = round(row[key], places)
    return list(rows)
