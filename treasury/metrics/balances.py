"""Client balances — who funds us, who we lend to, and how concentrated it is.

For a bank this size, depositor concentration *is* the liquidity risk: two
clients leaving can matter more than a curve move. The panel leads with the
top-N share of funding and flags limit breaches per counterparty.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import metric, spec
from .common import top_n


@metric("client_balances", title="Client balances", group="Clients", order=20,
        needs=("positions",),
        subtitle="Funding and lending by counterparty, with concentration and limits")
def client_balances(book, **params) -> Dict[str, Any]:
    base = book.settings.base_currency
    settings = book.settings

    clients: Dict[str, Dict[str, Any]] = {}
    for row in book.rows("positions"):
        if row.get("side") == "CAPITAL":
            continue                      # equity is not a client relationship
        name = str(row.get("counterparty") or "").strip() or "(unnamed)"
        entry = clients.setdefault(name, {
            "name": name, "type": row.get("counterparty_type") or "",
            "country": row.get("iso2") or "", "deposits": 0.0, "loans": 0.0,
            "net": 0.0, "currencies": set(), "weighted_rate": 0.0, "rate_weight": 0.0,
            "next_maturity": "", "deals": 0,
        })
        amount = row.get("base_amount")
        if amount is None:
            continue
        if row.get("side") == "LIABILITY":
            entry["deposits"] += amount
        else:
            entry["loans"] += amount
        entry["deals"] += 1
        entry["currencies"].add(row.get("currency") or "")
        if row.get("rate"):
            entry["weighted_rate"] += float(row["rate"]) * amount
            entry["rate_weight"] += amount
        maturity = row.get("maturity_date")
        if maturity and (not entry["next_maturity"] or maturity < entry["next_maturity"]):
            entry["next_maturity"] = maturity
        if not entry["type"]:
            entry["type"] = row.get("counterparty_type") or ""

    undrawn: Dict[str, float] = {}
    for row in book.rows("commitments"):
        name = str(row.get("counterparty") or "").strip()
        if name:
            undrawn[name] = undrawn.get(name, 0.0) + (row.get("base_amount") or 0.0)

    limits = {str(c.get("name") or "").strip(): c for c in book.rows("clients")}
    rows: List[Dict[str, Any]] = []
    for entry in clients.values():
        entry["net"] = entry["loans"] - entry["deposits"]
        entry["rate"] = (entry["weighted_rate"] / entry["rate_weight"]
                         if entry["rate_weight"] else None)
        entry["ccy"] = ", ".join(sorted(c for c in entry["currencies"] if c))
        client = limits.get(entry["name"], {})
        limit = client.get("limit")
        entry["limit"] = book.fx.to_base(limit, client.get("limit_currency")
                                         or base) if limit else None
        entry["undrawn"] = undrawn.get(entry["name"], 0.0)
        # Credit limits bind on drawn plus committed-undrawn, which is also what
        # the client map reports — the two panels must not disagree.
        exposure = max(entry["loans"], 0.0) + entry["undrawn"]
        entry["exposure"] = exposure
        entry["utilisation"] = (exposure / entry["limit"]
                                if entry["limit"] else None)
        entry["breach"] = bool(entry["limit"] and exposure > entry["limit"])
        entry["country"] = entry["country"] or client.get("iso2", "")
        entry.pop("currencies")
        entry.pop("weighted_rate")
        entry.pop("rate_weight")
        rows.append(entry)

    total_deposits = sum(r["deposits"] for r in rows)
    total_loans = sum(r["loans"] for r in rows)
    n = settings.concentration_top_n
    by_deposit = sorted(rows, key=lambda r: r["deposits"], reverse=True)
    top_share = (sum(r["deposits"] for r in by_deposit[:n]) / total_deposits
                 if total_deposits else None)
    largest = by_deposit[0] if by_deposit else None
    largest_share = (largest["deposits"] / total_deposits
                     if largest and total_deposits else None)

    breaches = [r for r in rows if r["breach"]]
    concentration_status = spec.status_for(
        top_share, settings.depositor_concentration_warn * 0.8,
        settings.depositor_concentration_warn, higher_is_better=False)

    head, rest = top_n(by_deposit, "deposits", 12, label="name")
    chart_rows = [r for r in head if r["deposits"] > 0]
    if rest["deposits"] > 0:
        chart_rows.append(rest)

    by_type: Dict[str, float] = {}
    for row in rows:
        by_type[row["type"] or "UNCLASSIFIED"] = (
            by_type.get(row["type"] or "UNCLASSIFIED", 0.0) + row["deposits"])
    type_labels = sorted(by_type, key=lambda k: by_type[k], reverse=True)

    return {
        "hero": spec.hero(f"Top {n} depositor share", top_share, "pct",
                          concentration_status,
                          f"of {_money(total_deposits)} {base} client funding"),
        "kpis": [
            spec.kpi("Client funding", total_deposits, "money",
                     hint="All liability positions, converted to " + base),
            spec.kpi("Client lending", total_loans, "money"),
            spec.kpi("Net client position", total_loans - total_deposits, "money",
                     hint="Positive = we lend more than we take"),
            spec.kpi("Largest single depositor", largest_share, "pct",
                     status=spec.status_for(largest_share, 0.10, 0.15,
                                            higher_is_better=False),
                     hint=largest["name"] if largest else ""),
            spec.kpi("Limit breaches", len(breaches), "number",
                     status="critical" if breaches else "good"),
        ],
        "charts": [
            spec.hbar(f"Largest depositors ({base})",
                      [r["name"] for r in chart_rows],
                      [spec.series("Funding", [round(r["deposits"], 2)
                                               for r in chart_rows], slot=1)],
                      format="money"),
            spec.bar("Funding by counterparty type", type_labels,
                     [spec.series("Funding", [round(by_type[t], 2) for t in type_labels],
                                  slot=1)], format="money"),
        ],
        "tables": [
            spec.table("Counterparties", [
                spec.col("name", "Counterparty"),
                spec.col("type", "Type"),
                spec.col("country", "Country"),
                spec.col("ccy", "Currencies"),
                spec.col("deposits", f"Funding ({base})", "money"),
                spec.col("loans", f"Lending ({base})", "money"),
                spec.col("net", f"Net ({base})", "money"),
                spec.col("undrawn", f"Undrawn ({base})", "money"),
                spec.col("rate", "Avg rate", "rate"),
                spec.col("limit", f"Limit ({base})", "money"),
                spec.col("utilisation", "Limit used", "pct"),
                spec.col("next_maturity", "Next maturity", "date"),
                spec.col("deals", "Deals", "number"),
            ], spec.round_all(sorted(rows, key=lambda r: r["deposits"] + r["loans"],
                                     reverse=True),
                              ["deposits", "loans", "net", "limit", "undrawn"]),
                total={"name": "Total", "deposits": round(total_deposits, 2),
                       "loans": round(total_loans, 2),
                       "net": round(total_loans - total_deposits, 2)},
                sort="deposits"),
        ],
        "notes": ([f"{len(breaches)} counterparty limit breach(es): "
                   + ", ".join(r["name"] for r in breaches[:5])] if breaches else [])
        + ["Limit utilisation is drawn lending plus committed-undrawn against the "
           "client master's limit column; counterparties without a limit show blank."],
    }


def _money(value: float) -> str:
    for unit, size in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= size:
            return f"{value / size:,.1f}{unit}"
    return f"{value:,.0f}"
