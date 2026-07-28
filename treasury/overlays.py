"""What happens when you click a country on the map.

Clicking a country asks the server for that jurisdiction's panel, and the panel
is assembled from **overlays**: small functions that each contribute one
section. Adding an interaction — a sanctions check, a settlement calendar, a
nostro balance, a limit request form — is one decorated function here:

    @overlay("nostro", title="Nostro balances", order=45)
    def nostro(book, iso2, context):
        rows = ...
        return section_table([col("bank", "Bank"), ...], rows)

Return ``None`` and the section is skipped for that country, so overlays stay
quiet where they have nothing to say.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import geo

OverlayFn = Callable[[Any, str, Dict[str, Any]], Optional[Dict[str, Any]]]


@dataclass
class Overlay:
    id: str
    title: str
    order: int
    fn: OverlayFn


REGISTRY: Dict[str, Overlay] = {}


def overlay(id: str, title: str, order: int = 100) -> Callable[[OverlayFn], OverlayFn]:
    def decorate(fn: OverlayFn) -> OverlayFn:
        REGISTRY[id] = Overlay(id=id, title=title, order=order, fn=fn)
        return fn
    return decorate


# --- section shapes the front end knows how to draw ---------------------------
def section_kv(pairs: Sequence[Dict[str, Any]], note: str = "") -> Dict[str, Any]:
    return {"kind": "kv", "pairs": list(pairs), "note": note}


def section_list(items: Sequence[str], note: str = "") -> Dict[str, Any]:
    return {"kind": "list", "items": [str(i) for i in items], "note": note}


def section_table(columns: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]],
                  note: str = "") -> Dict[str, Any]:
    return {"kind": "table", "columns": list(columns), "rows": list(rows), "note": note}


def section_meter(label: str, used: Optional[float], limit: Optional[float],
                  format: str = "money", note: str = "") -> Dict[str, Any]:
    ratio = (used / limit) if (limit and used is not None) else None
    return {"kind": "meter", "label": label, "used": used, "limit": limit,
            "ratio": ratio, "format": format, "note": note}


def col(key: str, label: str, format: str = "text") -> Dict[str, Any]:
    return {"key": key, "label": label, "format": format}


# --- jurisdiction notes -------------------------------------------------------
def load_country_notes(data_dir: str = "") -> Dict[str, Any]:
    """Shipped template, with the desk's own copy layered over it."""
    merged: Dict[str, Any] = {}
    paths = [os.path.join(geo.DATA_DIR, "country_overlays.json")]
    if data_dir:
        paths.append(os.path.join(data_dir, "country_overlays.json"))
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        for key, value in blob.items():
            if key.startswith("_"):
                continue
            merged[key.upper()] = {**merged.get(key.upper(), {}), **value}
    return merged


# --- the built-in overlays ----------------------------------------------------
@overlay("exposure", title="Exposure", order=10)
def _exposure(book, iso2: str, context: Dict[str, Any]):
    stats = context["stats"]
    base = book.settings.base_currency
    if not stats:
        return None
    return section_kv([
        {"label": "Clients", "value": stats["clients"], "format": "number"},
        {"label": f"Funding taken ({base})", "value": stats["funding"], "format": "money"},
        {"label": f"Lending placed ({base})", "value": stats["lending"], "format": "money"},
        {"label": f"Net position ({base})", "value": stats["lending"] - stats["funding"],
         "format": "money"},
        {"label": "Undrawn commitments", "value": stats["undrawn"], "format": "money"},
        {"label": "Live deals", "value": stats["deals"], "format": "number"},
    ])


@overlay("quota", title="Quota and limits", order=20)
def _quota(book, iso2: str, context: Dict[str, Any]):
    notes = context["notes"]
    stats = context["stats"]
    quota = (notes or {}).get("quota") or {}
    limit = quota.get("limit")
    currency = quota.get("currency") or book.settings.base_currency
    used = stats["lending"] + stats["undrawn"] if stats else 0.0
    if limit:
        limit = book.fx.to_base(float(limit), currency)
    breaches = context.get("breaches") or []
    section = section_meter(
        quota.get("label") or "Country exposure", used, limit,
        note=("No country limit set — add a quota block for this jurisdiction in "
              "country_overlays.json." if not limit else
              f"Limit stated as {quota.get('limit'):,.0f} {currency}."))
    if breaches:
        section["note"] += (" Counterparty limit breaches here: "
                            + ", ".join(breaches))
    return section


@overlay("regulation", title="Regulation", order=30)
def _regulation(book, iso2: str, context: Dict[str, Any]):
    notes = context["notes"]
    if not notes:
        return section_list(
            [f"No notes recorded for {geo.country_name(iso2) or iso2}.",
             "Add an entry keyed by the ISO-2 code to country_overlays.json in your "
             "data folder — it appears here on the next reload."])
    pairs = [{"label": "Regulator", "value": notes.get("regulator") or "—"}]
    pairs += [{"label": rule.get("label", ""), "value": rule.get("value", "")}
              for rule in notes.get("rules", [])]
    section = section_kv(pairs)
    text = [n for n in notes.get("notes", []) if n]
    if text:
        section["note"] = " ".join(text)
    if notes.get("review"):
        section["note"] = (section.get("note", "") +
                           f" (last reviewed {notes['review']})").strip()
    return section


@overlay("rates", title="Current bid / offer", order=40)
def _rates(book, iso2: str, context: Dict[str, Any]):
    currency = geo.currency_of(iso2)
    if not currency:
        return None
    rows = [r for r in book.rows("market_rates")
            if (r.get("currency") or "").upper() == currency]
    if not rows and currency == "CNY":
        rows = [r for r in book.rows("market_rates")
                if (r.get("currency") or "").upper() == "CNH"]
        currency = "CNH" if rows else currency
    if not rows:
        return section_list(
            [f"No {currency} quotes loaded for {book.as_of}.",
             "Point settings.json's quotes_db at the OCR pipeline's quotes.db, or drop a "
             "rates sheet (currency, tenor, bid, offer) into the data folder."])
    order = {t: i for i, t in enumerate(
        ["O/N", "T/N", "1W", "2W", "3W", "1M", "2M", "3M", "4M", "6M", "9M", "1Y"])}
    rows = sorted(rows, key=lambda r: order.get(str(r.get("tenor") or "").upper(), 99))
    table = section_table([
        col("tenor", "Tenor"), col("bid", "Bid", "rate"), col("offer", "Offer", "rate"),
        col("spread", "Spread", "bps"), col("source", "Source"),
    ], [{
        "tenor": r.get("tenor"), "bid": r.get("bid"), "offer": r.get("offer"),
        "spread": ((r.get("offer") - r.get("bid")) * 1e4
                   if r.get("offer") is not None and r.get("bid") is not None else None),
        "source": r.get("source") or r.get("benchmark") or "",
    } for r in rows[:14]], note=f"{currency} money-market quotes as at {book.as_of}.")
    return table


@overlay("clients", title="Clients here", order=50)
def _clients(book, iso2: str, context: Dict[str, Any]):
    rows = context.get("clients") or []
    if not rows:
        return None
    base = book.settings.base_currency
    # The drawer is narrow — keep this to what a dealer needs at a glance; the
    # Client balances panel carries the full detail.
    return section_table([
        col("name", "Client"), col("type", "Type"),
        col("funding", "Funding", "money"),
        col("exposure", "Exposure", "money"),
        col("utilisation", "Limit", "pct"),
    ], sorted(rows, key=lambda r: (r.get("funding") or 0) + (r.get("exposure") or 0),
              reverse=True),
        note=f"Amounts in {base}. Exposure is lending plus undrawn commitments.")


def build(book, iso2: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every overlay that has something to say about this jurisdiction."""
    sections = []
    for spec in sorted(REGISTRY.values(), key=lambda o: (o.order, o.id)):
        try:
            body = spec.fn(book, iso2, context)
        except Exception as exc:            # one bad overlay must not close the panel
            body = section_list([f"overlay failed: {type(exc).__name__}: {exc}"])
        if body:
            sections.append({"id": spec.id, "title": spec.title, **body})
    return sections
