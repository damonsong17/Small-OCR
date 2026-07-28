"""FX risk — the net open position, what a move costs, and limit headroom.

The NOP is built from everything, not just the trading book: on-balance-sheet
assets and liabilities, the securities portfolio, and the net forward position
from FX trades. A structural position hiding in the deposit book is exactly the
one that hurts.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from . import metric, spec

# Two-tailed normal quantiles, so no scipy on an air-gapped box.
_Z = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263, 0.995: 2.5758}


def _z_score(confidence: float) -> float:
    if confidence in _Z:
        return _Z[confidence]
    return min(_Z.items(), key=lambda kv: abs(kv[0] - confidence))[1]


@metric("fx_risk", title="FX risk", group="Market risk", order=30,
        needs=("positions",),
        subtitle="Net open position by currency, sensitivity, VaR and limit use")
def fx_risk(book, **params) -> Dict[str, Any]:
    settings = book.settings
    base = settings.base_currency

    legs: Dict[str, Dict[str, float]] = {}

    def add(currency: str, amount: float, kind: str) -> None:
        currency = (currency or "").upper()
        if not currency or not amount:
            return
        entry = legs.setdefault(currency, {"balance_sheet": 0.0, "securities": 0.0,
                                           "forward": 0.0})
        entry[kind] += amount

    for row in book.rows("positions"):
        add(row.get("currency"), row.get("signed_amount") or 0.0, "balance_sheet")
    for row in book.rows("securities"):
        add(row.get("currency"), row.get("market_value") or 0.0, "securities")
    for row in book.rows("fx_trades"):
        add(row.get("buy_currency"), abs(row.get("buy_amount") or 0.0), "forward")
        add(row.get("sell_currency"), -abs(row.get("sell_amount") or 0.0), "forward")

    z = _z_score(settings.var_confidence)
    horizon = math.sqrt(settings.var_horizon_days / 252.0)

    rows: List[Dict[str, Any]] = []
    for currency, parts in sorted(legs.items()):
        net = parts["balance_sheet"] + parts["securities"] + parts["forward"]
        if currency == base:
            continue
        base_net = book.fx.to_base(net, currency)
        vol = book.fx.vols.get(currency, settings.volatility_for(currency))
        var = abs(base_net) * vol * z * horizon if base_net is not None else None
        limit = (settings.capital_base * settings.nop_limit_pct_per_currency
                 if settings.capital_base else None)
        rows.append({
            "currency": currency,
            "balance_sheet": parts["balance_sheet"],
            "securities": parts["securities"],
            "forward": parts["forward"],
            "net": net,
            "base_net": base_net,
            "rate": book.fx.rate(currency, base),
            "volatility": vol,
            "shock": (-abs(base_net) * settings.fx_shock_pct
                      if base_net is not None else None),
            "var": var,
            "utilisation": (abs(base_net) / limit if limit and base_net is not None
                            else None),
            "breach": bool(limit and base_net is not None and abs(base_net) > limit),
        })

    priced = [r for r in rows if r["base_net"] is not None]
    longs = sum(r["base_net"] for r in priced if r["base_net"] > 0)
    shorts = sum(-r["base_net"] for r in priced if r["base_net"] < 0)
    aggregate = max(longs, shorts)                    # Basel "shorthand" NOP
    undiversified = sum(r["var"] or 0.0 for r in priced)
    diversified = math.sqrt(sum((r["var"] or 0.0) ** 2 for r in priced))
    net_shock = sum(r["shock"] or 0.0 for r in priced)

    limit_agg = (settings.capital_base * settings.nop_limit_pct_aggregate
                 if settings.capital_base else None)
    limit_ccy = (settings.capital_base * settings.nop_limit_pct_per_currency
                 if settings.capital_base else None)
    aggregate_use = aggregate / limit_agg if limit_agg else None
    breaches = [r for r in rows if r["breach"]]

    ranked = sorted(priced, key=lambda r: abs(r["base_net"]), reverse=True)[:12]
    by_var = sorted(priced, key=lambda r: r["var"] or 0, reverse=True)[:12]

    return {
        "hero": spec.hero("Aggregate net open position", aggregate, "money",
                          spec.status_for(aggregate_use, 0.75, 1.0,
                                          higher_is_better=False),
                          (f"{aggregate_use:.0%} of the "
                           f"{settings.nop_limit_pct_aggregate:.0%}-of-capital limit"
                           if aggregate_use is not None
                           else "set capital_base in settings.json to see limit use")),
        "kpis": [
            spec.kpi("Long positions", longs, "money"),
            spec.kpi("Short positions", -shorts, "money"),
            spec.kpi(f"VaR {settings.var_confidence:.0%} / "
                     f"{settings.var_horizon_days}d", diversified, "money",
                     hint=f"Parametric, uncorrelated across currencies. "
                          f"Undiversified sum: {undiversified:,.0f} {base}"),
            spec.kpi(f"P&L on a {settings.fx_shock_pct:.0%} adverse move",
                     net_shock, "money",
                     status="warning" if net_shock < 0 else "neutral",
                     hint="Every currency moved against the position at once"),
            spec.kpi("Currency limit breaches", len(breaches), "number",
                     status="critical" if breaches else "good"),
        ],
        "charts": [
            spec.bar(f"Net open position by currency ({base})",
                     [r["currency"] for r in ranked],
                     [spec.series("Net position",
                                  [round(r["base_net"], 2) for r in ranked], slot=1)],
                     format="money",
                     reference=([spec.reference(limit_ccy, "Per-currency limit",
                                                "critical"),
                                 spec.reference(-limit_ccy, "", "critical")]
                                if limit_ccy else []),
                     note="Positive = long the currency."),
            spec.bar("Where the position comes from",
                     [r["currency"] for r in ranked], [
                         spec.series("Balance sheet",
                                     [round(book.fx.to_base(r["balance_sheet"],
                                                            r["currency"]) or 0, 2)
                                      for r in ranked], slot=1),
                         spec.series("Securities",
                                     [round(book.fx.to_base(r["securities"],
                                                            r["currency"]) or 0, 2)
                                      for r in ranked], slot=3),
                         spec.series("FX forwards",
                                     [round(book.fx.to_base(r["forward"],
                                                            r["currency"]) or 0, 2)
                                      for r in ranked], slot=2),
                     ], stacked=True, format="money"),
            spec.hbar(f"Value at risk by currency ({base})",
                      [r["currency"] for r in by_var],
                      [spec.series("VaR", [round(r["var"] or 0, 2) for r in by_var],
                                   slot=1)],
                      format="money"),
        ],
        "tables": [
            spec.table("Net open position", [
                spec.col("currency", "Ccy"),
                spec.col("balance_sheet", "Balance sheet", "money_ccy"),
                spec.col("securities", "Securities", "money_ccy"),
                spec.col("forward", "FX forwards", "money_ccy"),
                spec.col("net", "Net position", "money_ccy"),
                spec.col("rate", f"Spot vs {base}", "price"),
                spec.col("base_net", f"Net ({base})", "money"),
                spec.col("volatility", "Vol (ann.)", "pct"),
                spec.col("var", f"VaR ({base})", "money"),
                spec.col("utilisation", "Limit used", "pct"),
            ], spec.round_all(sorted(rows, key=lambda r: abs(r["base_net"] or 0),
                                     reverse=True),
                              ["balance_sheet", "securities", "forward", "net",
                               "base_net", "var"]),
                total={"currency": "Total", "base_net": round(longs - shorts, 2),
                       "var": round(undiversified, 2)}),
        ],
        "notes": [
            f"VaR is parametric: |position| × σ × {z:.2f} × √({settings.var_horizon_days}/252), "
            f"with σ per currency from the FX sheet's volatility column or "
            f"default_fx_volatility ({settings.default_fx_volatility:.0%}).",
            "Correlations are not modelled. The diversified figure assumes independence "
            "and the undiversified sum assumes perfect correlation — the truth is between.",
        ] + ([f"Over the per-currency limit: "
              f"{', '.join(r['currency'] for r in breaches)}"] if breaches else []),
    }
