"""The client map — where the book actually sits.

Aggregates every position, commitment and client record onto a point and a
jurisdiction, then hands the front end a plain list. Clicking a country calls
back to ``/api/country/<iso2>``, which assembles the overlay sections
(regulation, quota, live bid/offer, the clients there) from
:mod:`treasury.overlays`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import geo, overlays
from . import metric, spec


def client_rows(book) -> List[Dict[str, Any]]:
    """One row per counterparty, located and sized."""
    entries: Dict[str, Dict[str, Any]] = {}

    def entry_for(name: str) -> Dict[str, Any]:
        key = name.strip().lower()
        if key not in entries:
            master = book.clients.get(key, {})
            entries[key] = {
                "name": name.strip() or "(unnamed)",
                "type": master.get("counterparty_type") or "",
                "sector": master.get("sector") or "",
                "country": master.get("country") or "",
                "city": master.get("city") or "",
                "iso2": master.get("iso2") or "",
                "lat": master.get("lat"), "lon": master.get("lon"),
                "precision": master.get("geo_precision") or "unplaced",
                "rating": master.get("rating") or "",
                "relationship_manager": master.get("relationship_manager") or "",
                "limit": master.get("limit"),
                "limit_currency": master.get("limit_currency") or "",
                "funding": 0.0, "lending": 0.0, "undrawn": 0.0, "deals": 0,
                "currencies": set(),
            }
        return entries[key]

    for row in book.rows("positions"):
        name = str(row.get("counterparty") or "").strip()
        if not name:
            continue
        side = row.get("side")
        if side == "CAPITAL":
            continue
        entry = entry_for(name)
        amount = row.get("base_amount") or 0.0
        if side == "LIABILITY":
            entry["funding"] += amount
        else:
            entry["lending"] += amount
        entry["deals"] += 1
        entry["currencies"].add(row.get("currency") or "")
        if not entry["type"]:
            entry["type"] = row.get("counterparty_type") or ""
        if not entry["iso2"] and row.get("iso2"):
            entry["iso2"] = row["iso2"]

    for row in book.rows("commitments"):
        name = str(row.get("counterparty") or "").strip()
        if not name:
            continue
        entry = entry_for(name)
        entry["undrawn"] += row.get("base_amount") or 0.0
        if not entry["iso2"] and row.get("iso2"):
            entry["iso2"] = row["iso2"]

    for row in book.rows("clients"):        # clients with no live business still show
        name = str(row.get("name") or "").strip()
        if name:
            entry_for(name)

    out: List[Dict[str, Any]] = []
    for entry in entries.values():
        if entry["lat"] is None and entry["iso2"]:
            point = geo.resolve(entry["iso2"], entry["city"])
            if point:
                entry["lat"], entry["lon"] = point.lat, point.lon
                entry["precision"] = point.precision
        entry["country_name"] = geo.country_name(entry["iso2"]) if entry["iso2"] else ""
        entry["exposure"] = entry["lending"] + entry["undrawn"]
        entry["total"] = entry["funding"] + entry["exposure"]
        limit_base = (book.fx.to_base(entry["limit"],
                                      entry["limit_currency"] or book.settings.base_currency)
                      if entry["limit"] else None)
        entry["limit_base"] = limit_base
        entry["utilisation"] = (entry["exposure"] / limit_base) if limit_base else None
        entry["breach"] = bool(limit_base and entry["exposure"] > limit_base)
        entry["ccy"] = ", ".join(sorted(c for c in entry["currencies"] if c))
        entry.pop("currencies")
        out.append(entry)
    return sorted(out, key=lambda e: e["total"], reverse=True)


def country_stats(book, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    rows = client_rows(book) if rows is None else rows
    stats: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        iso2 = row["iso2"]
        if not iso2:
            continue
        entry = stats.setdefault(iso2, {
            "iso2": iso2, "name": geo.country_name(iso2),
            "currency": geo.currency_of(iso2),
            "clients": 0, "funding": 0.0, "lending": 0.0, "undrawn": 0.0,
            "exposure": 0.0, "deals": 0, "breaches": [],
        })
        entry["clients"] += 1
        entry["funding"] += row["funding"]
        entry["lending"] += row["lending"]
        entry["undrawn"] += row["undrawn"]
        entry["exposure"] += row["exposure"]
        entry["deals"] += row["deals"]
        if row["breach"]:
            entry["breaches"].append(row["name"])
    return stats


def country_context(book, iso2: str) -> Dict[str, Any]:
    """Everything the overlays need for one jurisdiction."""
    rows = client_rows(book)
    stats = country_stats(book, rows)
    notes = overlays.load_country_notes(book.settings.data_dir)
    here = [r for r in rows if r["iso2"] == iso2]
    return {
        "iso2": iso2,
        "name": geo.country_name(iso2) or iso2,
        "currency": geo.currency_of(iso2),
        "stats": stats.get(iso2),
        "clients": here,
        "breaches": [r["name"] for r in here if r["breach"]],
        "notes": notes.get(iso2.upper()),
    }


@metric("client_map", title="Client map", group="Clients", order=25,
        needs=("clients",),
        subtitle="Where the book sits — click a country for regulation, quota and rates")
def client_map(book, colour_by: str = "type", **params) -> Dict[str, Any]:
    base = book.settings.base_currency
    rows = client_rows(book)
    stats = country_stats(book, rows)
    placed = [r for r in rows if r["lat"] is not None]
    unplaced = [r for r in rows if r["lat"] is None]

    total_exposure = sum(r["exposure"] for r in rows)
    total_funding = sum(r["funding"] for r in rows)
    by_country = sorted(stats.values(), key=lambda s: s["funding"] + s["exposure"],
                        reverse=True)
    top = by_country[0] if by_country else None
    top_share = ((top["funding"] + top["exposure"]) /
                 (total_funding + total_exposure)
                 if top and (total_funding + total_exposure) else None)
    breaches = [r["name"] for r in rows if r["breach"]]

    types = sorted({r["type"] or "UNCLASSIFIED" for r in rows})
    return {
        "map": {
            "points": [{
                "name": r["name"], "type": r["type"] or "UNCLASSIFIED",
                "iso2": r["iso2"], "country": r["country_name"], "city": r["city"],
                "lat": r["lat"], "lon": r["lon"], "precision": r["precision"],
                "funding": round(r["funding"], 2), "lending": round(r["lending"], 2),
                "undrawn": round(r["undrawn"], 2), "exposure": round(r["exposure"], 2),
                "total": round(r["total"], 2), "deals": r["deals"],
                "utilisation": r["utilisation"], "breach": r["breach"],
                "rating": r["rating"], "ccy": r["ccy"],
                "relationship_manager": r["relationship_manager"],
            } for r in placed],
            "countries": {iso2: {
                "name": s["name"], "clients": s["clients"],
                "funding": round(s["funding"], 2), "lending": round(s["lending"], 2),
                "exposure": round(s["exposure"], 2), "currency": s["currency"],
                "breaches": s["breaches"],
            } for iso2, s in stats.items()},
            "types": types,
            "colour_by": colour_by,
            "base_currency": base,
            "geojson_url": "world.geo.json",
        },
        "kpis": [
            spec.kpi("Clients on the map", len(placed), "number",
                     hint=(f"{len(unplaced)} could not be placed" if unplaced else
                           "every client located")),
            spec.kpi("Jurisdictions", len(stats), "number"),
            spec.kpi(f"Total exposure ({base})", total_exposure, "money",
                     hint="Lending plus undrawn commitments"),
            spec.kpi("Largest country share", top_share, "pct",
                     status=spec.status_for(top_share, 0.35, 0.5, higher_is_better=False),
                     hint=top["name"] if top else ""),
            spec.kpi("Counterparty limit breaches", len(breaches), "number",
                     status="critical" if breaches else "good"),
        ],
        "charts": [
            spec.hbar(f"Exposure by jurisdiction ({base})",
                      [s["name"] for s in by_country[:12]],
                      [spec.series("Lending and undrawn",
                                   [round(s["exposure"], 2) for s in by_country[:12]],
                                   slot=1),
                       spec.series("Funding taken",
                                   [round(s["funding"], 2) for s in by_country[:12]],
                                   slot=3)],
                      format="money"),
        ],
        "tables": [
            spec.table("By jurisdiction", [
                spec.col("name", "Jurisdiction"), spec.col("iso2", "ISO"),
                spec.col("currency", "Ccy"), spec.col("clients", "Clients", "number"),
                spec.col("funding", f"Funding ({base})", "money"),
                spec.col("lending", f"Lending ({base})", "money"),
                spec.col("undrawn", f"Undrawn ({base})", "money"),
                spec.col("deals", "Deals", "number"),
            ], spec.round_all(by_country, ["funding", "lending", "undrawn"])),
        ] + ([spec.table("Clients that could not be placed", [
            spec.col("name", "Client"), spec.col("country", "Country given"),
            spec.col("city", "City given"),
            spec.col("total", f"Business ({base})", "money"),
        ], [{"name": r["name"], "country": r["country"], "city": r["city"],
             "total": round(r["total"], 2)} for r in unplaced],
            note="Add a country, city or lat/lon to the client master, or a row to "
                 "treasury/data/places.json.")] if unplaced else []),
        "notes": [
            "Bubble area is total business (funding plus exposure); colour is "
            "counterparty type. Click a country for its overlay panel.",
            "The outline is a simplified public-domain world map bundled with the app — "
            "no tile server, no network call.",
        ],
    }
