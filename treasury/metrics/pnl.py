"""P&L — what the book earns while it sits there, and what FX did to it.

Net interest is accrued from the contracts themselves (rate x balance x days),
so the run-rate is available the moment positions load. FX revaluation compares
today's rates against the last stored snapshot, which is why the dashboard keeps
its own history file.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict

from . import metric, spec
from .common import accrual_per_day, as_date


def _period_start(book) -> str:
    if book.settings.pnl_period_start:
        return book.settings.pnl_period_start
    today = as_date(book.as_of) or _dt.date.today()
    return today.replace(day=1).isoformat()


def _accrued(row: Dict[str, Any], start: str, end: str) -> float:
    """Interest earned or paid on one position between two dates."""
    daily = accrual_per_day(row)
    if not daily:
        return 0.0
    first = max(as_date(row.get("start_date")) or as_date(start), as_date(start))
    last = as_date(end)
    maturity = as_date(row.get("maturity_date"))
    if maturity and maturity < last:
        last = maturity
    if first is None or last is None or last <= first:
        return 0.0
    return daily * (last - first).days


@metric("pnl", title="P&L", group="Performance", order=60, needs=("positions",),
        subtitle="Net interest accrual, spread and FX revaluation")
def pnl(book, **params) -> Dict[str, Any]:
    settings = book.settings
    base = settings.base_currency
    start = _period_start(book)

    asset_income = liability_cost = 0.0
    asset_balance = liability_balance = 0.0
    asset_period = liability_period = 0.0
    daily_net = 0.0
    period_net = 0.0
    by_currency: Dict[str, Dict[str, float]] = {}
    by_product: Dict[str, float] = {}

    for row in book.rows("positions"):
        base_amount = row.get("base_amount") or 0.0
        rate = row.get("rate") or 0.0
        currency = row.get("currency") or ""
        daily = accrual_per_day(row)
        daily_base = book.fx.to_base(daily, currency) or 0.0
        accrued_base = book.fx.to_base(_accrued(row, start, book.as_of), currency) or 0.0
        daily_net += daily_base
        period_net += accrued_base

        entry = by_currency.setdefault(currency, {
            "currency": currency, "assets": 0.0, "liabilities": 0.0,
            "asset_income": 0.0, "funding_cost": 0.0, "period": 0.0})
        if row.get("side") == "ASSET":
            asset_income += base_amount * rate
            asset_balance += base_amount
            asset_period += accrued_base
            entry["assets"] += base_amount
            entry["asset_income"] += base_amount * rate
        else:
            liability_cost += base_amount * rate
            liability_balance += base_amount
            liability_period -= accrued_base      # accrual is already negative
            entry["liabilities"] += base_amount
            entry["funding_cost"] += base_amount * rate
        entry["period"] += accrued_base
        product = row.get("product") or "OTHER"
        by_product[product] = by_product.get(product, 0.0) + accrued_base

    asset_yield = spec.safe_div(asset_income, asset_balance)
    funding_rate = spec.safe_div(liability_cost, liability_balance)
    spread = (asset_yield - funding_rate
              if asset_yield is not None and funding_rate is not None else None)
    nim = spec.safe_div(asset_income - liability_cost, asset_balance)

    for entry in by_currency.values():
        entry["yield"] = spec.safe_div(entry["asset_income"], entry["assets"])
        entry["cost"] = spec.safe_div(entry["funding_cost"], entry["liabilities"])
        entry["spread"] = (entry["yield"] - entry["cost"]
                           if entry["yield"] is not None and entry["cost"] is not None
                           else None)
        entry["net_interest"] = entry["asset_income"] - entry["funding_cost"]

    fx_pnl, fx_lines, fx_note = _fx_revaluation(book)

    # Fees and other P&L items that have actually settled in the period. A
    # future-dated flow belongs in the cash ladder, not in P&L to date.
    realised = 0.0
    for row in book.rows("cashflows"):
        category = str(row.get("category") or "").lower()
        date = str(row.get("date") or "")
        if not (start <= date <= book.as_of):
            continue
        if any(word in category for word in ("pnl", "p&l", "fee", "commission",
                                             "realised", "realized")):
            realised += book.fx.to_base(row.get("amount") or 0.0,
                                        row.get("currency")) or 0.0

    total = period_net + fx_pnl + realised
    products = sorted(by_product.items(), key=lambda kv: abs(kv[1]), reverse=True)[:8]

    return {
        "hero": spec.hero(f"P&L since {start}", total, "money",
                          "good" if total >= 0 else "critical",
                          f"net interest {period_net:,.0f} · FX {fx_pnl:,.0f} · "
                          f"other {realised:,.0f} {base}"),
        "kpis": [
            spec.kpi("Net interest, run rate per day", daily_net, "money",
                     status="good" if daily_net >= 0 else "critical",
                     hint=f"Annualised: {daily_net * 365:,.0f} {base}"),
            spec.kpi("Net interest, period to date", period_net, "money"),
            spec.kpi("Asset yield", asset_yield, "rate",
                     hint=f"On {asset_balance:,.0f} {base} of assets"),
            spec.kpi("Cost of funds", funding_rate, "rate",
                     hint=f"On {liability_balance:,.0f} {base} of liabilities"),
            spec.kpi("Net interest spread", spread, "rate",
                     status="good" if (spread or 0) > 0 else "critical"),
            spec.kpi("Net interest margin", nim, "rate",
                     hint="Net interest income over interest-earning assets"),
            spec.kpi("FX revaluation", fx_pnl, "money",
                     status="good" if fx_pnl >= 0 else "warning", hint=fx_note),
        ],
        "charts": [
            spec.waterfall("P&L bridge",
                           ["Interest income", "Funding cost", "FX revaluation",
                            "Fees and other", "Total"],
                           [round(v, 2) for v in [asset_period, -liability_period,
                                                  fx_pnl, realised, total]],
                           kinds=["delta", "delta", "delta", "delta", "total"],
                           format="money"),
            spec.bar("Net interest by currency",
                     [e["currency"] for e in sorted(by_currency.values(),
                                                    key=lambda e: abs(e["net_interest"]),
                                                    reverse=True)[:10]],
                     [spec.series("Annualised net interest",
                                  [round(e["net_interest"], 2) for e in
                                   sorted(by_currency.values(),
                                          key=lambda e: abs(e["net_interest"]),
                                          reverse=True)[:10]], slot=1)],
                     format="money"),
            spec.hbar("Period accrual by product", [p[0] for p in products],
                      [spec.series("Accrued", [round(p[1], 2) for p in products],
                                   slot=1)], format="money"),
        ],
        "tables": [
            spec.table("By currency", [
                spec.col("currency", "Ccy"),
                spec.col("assets", f"Assets ({base})", "money"),
                spec.col("liabilities", f"Liabilities ({base})", "money"),
                spec.col("yield", "Asset yield", "rate"),
                spec.col("cost", "Funding cost", "rate"),
                spec.col("spread", "Spread", "rate"),
                spec.col("net_interest", f"Net interest p.a. ({base})", "money"),
                spec.col("period", f"Accrued to date ({base})", "money"),
            ], spec.round_all(sorted(by_currency.values(),
                                     key=lambda e: e["assets"] + e["liabilities"],
                                     reverse=True),
                              ["assets", "liabilities", "net_interest", "period"]),
                total={"currency": "Total",
                       "assets": round(asset_balance, 2),
                       "liabilities": round(liability_balance, 2),
                       "net_interest": round(asset_income - liability_cost, 2),
                       "period": round(period_net, 2)}),
        ] + ([spec.table("FX revaluation by currency", [
            spec.col("currency", "Ccy"),
            spec.col("position", "Net position", "money_ccy"),
            spec.col("prior_rate", "Prior rate", "price"),
            spec.col("rate", "Current rate", "price"),
            spec.col("move", "Move", "pct"),
            spec.col("pnl", f"P&L ({base})", "money"),
        ], spec.round_all(fx_lines, ["position", "pnl"]),
            total={"currency": "Total", "pnl": round(fx_pnl, 2)})] if fx_lines else []),
        "notes": [
            f"Accrual period runs from {start} to {book.as_of}. Set pnl_period_start "
            "in settings.json to change it.",
            "Interest is simple accrual on the contract rate — no amortisation, no "
            "fair-value moves on the securities book.",
            fx_note,
        ],
    }


def _fx_revaluation(book):
    """Mark the open position against the previously stored rate set."""
    from ..store import open_store

    store = open_store(book.settings)
    if store is None:
        return 0.0, [], "No snapshot store — FX revaluation needs a prior mark."
    try:
        prior_date, prior = store.previous_fx(book.as_of, book.fx.base)
    finally:
        store.close()
    if not prior:
        return 0.0, [], ("No earlier FX snapshot yet. Run the dashboard with --snapshot "
                         "on each reporting date and revaluation starts from the next one.")

    positions: Dict[str, float] = {}
    for row in book.rows("positions"):
        currency = row.get("currency") or ""
        positions[currency] = positions.get(currency, 0.0) + (row.get("signed_amount") or 0.0)
    for row in book.rows("securities"):
        currency = row.get("currency") or ""
        positions[currency] = positions.get(currency, 0.0) + (row.get("market_value") or 0.0)
    for row in book.rows("fx_trades"):
        for currency, amount in ((row.get("buy_currency"), abs(row.get("buy_amount") or 0)),
                                 (row.get("sell_currency"), -abs(row.get("sell_amount") or 0))):
            if currency:
                positions[currency] = positions.get(currency, 0.0) + amount

    lines, total = [], 0.0
    for currency, position in sorted(positions.items()):
        if not currency or currency == book.fx.base or not position:
            continue
        now = book.fx.rate(currency, book.fx.base)
        was = prior.get(currency)
        if now is None or was is None:
            continue
        pnl_value = position * (now - was)
        total += pnl_value
        lines.append({"currency": currency, "position": position, "prior_rate": was,
                      "rate": now, "move": (now - was) / was if was else None,
                      "pnl": pnl_value})
    return total, lines, f"FX revaluation marked against the snapshot of {prior_date}."
