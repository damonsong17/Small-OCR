"""The metric registry — the extension point.

A metric is a function that turns a :class:`~treasury.book.Book` into a
**panel**: a plain dict of KPIs, charts and tables. The front end renders panels
generically, so adding a metric is one decorated function in this package and
nothing else — no HTML, no JavaScript, no route:

    from treasury.metrics import metric
    from treasury.metrics import spec

    @metric("duration", title="Portfolio duration", group="Market risk",
            needs=("securities",))
    def duration(book, **params):
        ...
        return {"kpis": [spec.kpi("Modified duration", 4.2, "number")],
                "charts": [spec.bar("By bucket", labels,
                                    [spec.series("Duration", values)])]}

Import your module anywhere before the server starts (or drop it in this
package) and it appears in the navigation.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..schema import BY_NAME

MetricFn = Callable[..., Dict[str, Any]]


@dataclass
class Metric:
    id: str
    title: str
    group: str = "Other"
    subtitle: str = ""
    needs: Tuple[str, ...] = ()
    order: int = 100
    fn: Optional[MetricFn] = None

    def missing(self, book) -> List[str]:
        return [d for d in self.needs if not book.rows(d)]


REGISTRY: Dict[str, Metric] = {}


def metric(id: str, title: str, group: str = "Other", subtitle: str = "",
           needs: Sequence[str] = (), order: int = 100) -> Callable[[MetricFn], MetricFn]:
    for dataset in needs:
        if dataset not in BY_NAME:      # fail at import, not at request time
            raise KeyError(f"metric {id!r} needs unknown dataset {dataset!r}")

    def decorate(fn: MetricFn) -> MetricFn:
        REGISTRY[id] = Metric(id=id, title=title, group=group, subtitle=subtitle,
                              needs=tuple(needs), order=order, fn=fn)
        return fn
    return decorate


def compute(book, id: str, **params: Any) -> Dict[str, Any]:
    """Run one metric, always returning a renderable panel."""
    spec = REGISTRY.get(id)
    if spec is None:
        return {"id": id, "title": id, "error": f"no metric named {id!r}"}
    panel: Dict[str, Any] = {
        "id": spec.id, "title": spec.title, "group": spec.group,
        "subtitle": spec.subtitle, "as_of": book.as_of,
        "base_currency": book.settings.base_currency,
        "kpis": [], "charts": [], "tables": [], "notes": [], "error": "",
    }
    missing = spec.missing(book)
    if missing:
        titles = ", ".join(BY_NAME[m].title for m in missing)
        panel["error"] = f"needs data that wasn't loaded: {titles}"
        panel["notes"].append(
            "Drop a workbook with those columns into the data folder, or pin it "
            "in mapping.json, and reload.")
        return panel
    try:
        panel.update(spec.fn(book, **params) or {})
    except Exception as exc:            # one broken metric must not blank the board
        panel["error"] = f"{type(exc).__name__}: {exc}"
        panel["traceback"] = traceback.format_exc(limit=6)
    return panel


def catalogue(book) -> List[Dict[str, Any]]:
    """Everything registered, in navigation order, with availability flags."""
    out = []
    for spec in sorted(REGISTRY.values(), key=lambda m: (m.order, m.title)):
        missing = spec.missing(book)
        out.append({
            "id": spec.id, "title": spec.title, "group": spec.group,
            "subtitle": spec.subtitle, "available": not missing,
            "missing": [BY_NAME[m].title for m in missing],
        })
    return out


# Importing the modules is what registers them.
from . import (          # noqa: E402,F401  (side-effect imports, order = nav order)
    cash, balances, fx_risk, lcr, nsfr, pnl, clients_map, sources,
)
