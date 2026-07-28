"""Cash projection — will we be short, in which currency, and on what day.

Two views of the same flows: a day-by-day ladder for the next month (the one
the dealer watches before pricing anything) and a maturity-bucket gap ladder out
to a year (the one ALCO reads).
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import metric, spec
from .common import (bucket_index, contractual_flows, date_range,
                     demand_liabilities, opening_cash)


@metric("cash_projection", title="Cash projection", group="Liquidity", order=10,
        needs=("positions",),
        subtitle="Contractual flows against today's cash, by day and by bucket")
def cash_projection(book, currency: str = "ALL", **params) -> Dict[str, Any]:
    settings = book.settings
    base = settings.base_currency
    currency = (currency or "ALL").upper()
    in_scope = (lambda ccy: True) if currency == "ALL" else (lambda ccy: ccy == currency)

    def convert(amount: float, ccy: str) -> float:
        if currency == "ALL":
            return book.fx.to_base(amount, ccy) or 0.0
        return amount

    flows = [f for f in contractual_flows(book) if in_scope(f.currency)]
    cash_now = opening_cash(book)
    callable_by_ccy = demand_liabilities(book)
    opening = sum(convert(v, k) for k, v in cash_now.items() if in_scope(k))
    callable_now = sum(convert(v, k) for k, v in callable_by_ccy.items() if in_scope(k))

    # --- daily ladder --------------------------------------------------------
    horizon = settings.cash_daily_days
    days = date_range(book.as_of, horizon)
    inflow = {d: 0.0 for d in days}
    outflow = {d: 0.0 for d in days}
    for flow in flows:
        if flow.date < days[0] or flow.date > days[-1]:
            continue
        value = convert(flow.amount, flow.currency)
        if value >= 0:
            inflow[flow.date] += value
        else:
            outflow[flow.date] += value

    balance, running = [], opening
    for day in days:
        running += inflow[day] + outflow[day]
        balance.append(running)

    trough = min(balance) if balance else opening
    trough_day = days[balance.index(trough)] if balance else book.as_of
    first_negative = next((d for d, b in zip(days, balance) if b < 0), "")

    # --- bucket ladder -------------------------------------------------------
    buckets = settings.cash_buckets
    labels = [b[0] for b in buckets]
    gap = [0.0] * len(buckets)
    for flow in flows:
        delta = _days_between(book.as_of, flow.date)
        if delta is None or delta < 0:
            continue
        gap[bucket_index(delta, buckets)] += convert(flow.amount, flow.currency)
    cumulative, total = [], opening
    for value in gap:
        total += value
        cumulative.append(total)

    # --- per-currency summary ------------------------------------------------
    rows: List[Dict[str, Any]] = []
    for ccy in sorted({f.currency for f in flows} | set(cash_now)):
        if not in_scope(ccy):
            continue
        ccy_flows = [f for f in flows if f.currency == ccy]
        open_ccy = cash_now.get(ccy, 0.0)
        rows.append({
            "currency": ccy,
            "opening": open_ccy,
            "d7": sum(f.amount for f in ccy_flows if _within(book.as_of, f.date, 7)),
            "d30": sum(f.amount for f in ccy_flows if _within(book.as_of, f.date, 30)),
            "d90": sum(f.amount for f in ccy_flows if _within(book.as_of, f.date, 90)),
            "callable": -callable_by_ccy.get(ccy, 0.0),
            "base_opening": book.fx.to_base(open_ccy, ccy),
        })
    for row in rows:
        row["projected_30"] = row["opening"] + row["d30"]

    status = "critical" if first_negative else (
        "warning" if trough < opening * 0.25 else "good")

    return {
        "hero": spec.hero(
            f"Lowest projected balance ({horizon}d)", trough, "money",
            status, f"on {trough_day}" + (f" · first negative {first_negative}"
                                          if first_negative else "")),
        "kpis": [
            spec.kpi("Cash available now", opening, "money",
                     hint="Vault cash, central-bank reserves and nostro balances"),
            spec.kpi("Net contractual flow, 30 days",
                     sum(inflow.values()) + sum(outflow.values()), "money",
                     status="good" if sum(inflow.values()) + sum(outflow.values()) >= 0
                     else "warning"),
            spec.kpi("Callable on demand", -callable_now, "money", status="neutral",
                     hint="Current, call and vostro balances a client can withdraw today. "
                          "Not in the contractual ladder — this is the behavioural overlay."),
            spec.kpi("Days of cash cover", _cover_days(opening, outflow, days),
                     "days", hint="Days before cumulative outflows exhaust today's cash, "
                                  "ignoring inflows"),
        ],
        "charts": [
            spec.bar(f"Daily flows — next {horizon} days", [d[5:] for d in days], [
                spec.series("Inflows", [round(inflow[d], 2) for d in days], slot=3),
                spec.series("Outflows", [round(outflow[d], 2) for d in days], slot=2),
            ], stacked=True, format="money",
                axis_label=base if currency == "ALL" else currency),
            spec.line("Projected cash balance", [d[5:] for d in days], [
                spec.series("Projected balance", [round(b, 2) for b in balance], slot=1),
            ], format="money", reference=[spec.reference(0, "Zero balance", "critical")],
                axis_label=base if currency == "ALL" else currency),
            spec.bar("Contractual gap by maturity bucket", labels, [
                spec.series("Net gap", [round(g, 2) for g in gap], slot=1),
            ], format="money", note="Positive = cash in. Demand balances are excluded; "
                                    "see the callable-on-demand figure above."),
            spec.line("Cumulative position by bucket", labels, [
                spec.series("Cumulative", [round(c, 2) for c in cumulative], slot=1),
            ], format="money", reference=[spec.reference(0, "Zero", "critical")]),
        ],
        "tables": [
            spec.table("By currency", [
                spec.col("currency", "Currency"),
                spec.col("opening", "Cash now", "money_ccy"),
                spec.col("d7", "Net 7d", "money_ccy"),
                spec.col("d30", "Net 30d", "money_ccy"),
                spec.col("d90", "Net 90d", "money_ccy"),
                spec.col("projected_30", "Projected 30d", "money_ccy"),
                spec.col("callable", "Callable now", "money_ccy"),
                spec.col("base_opening", f"Cash now ({base})", "money"),
            ], spec.round_all(rows, ["opening", "d7", "d30", "d90", "projected_30",
                                     "callable", "base_opening"]),
                note="Amounts in each row's own currency except the last column."),
            spec.table("Largest movements in the horizon", [
                spec.col("date", "Date", "date"),
                spec.col("currency", "Ccy"),
                spec.col("amount", "Amount", "money_ccy"),
                spec.col("category", "Type"),
                spec.col("counterparty", "Counterparty"),
                spec.col("detail", "Detail"),
            ], [f.as_dict() for f in sorted(
                [f for f in flows if _within(book.as_of, f.date, horizon)],
                key=lambda f: abs(f.base_amount), reverse=True)[:25]]),
        ],
        "notes": [
            "Interest is projected as settled with principal, which is how money-market "
            "deals pay. Contracts longer than a year show principal only — their coupon "
            "schedule isn't in the source data.",
            "Add a cashflow sheet (date, currency, amount, category) for anything the "
            "contracts don't know about: tax, dividends, capex, forecast client flows.",
        ],
        "params": {"currency": {"label": "Currency", "value": currency,
                                "options": ["ALL"] + book.currencies()}},
    }


def _days_between(start: str, end: str):
    from .common import as_date
    first, last = as_date(start), as_date(end)
    if first is None or last is None:
        return None
    return (last - first).days


def _within(as_of: str, date: str, days: int) -> bool:
    delta = _days_between(as_of, date)
    return delta is not None and 0 <= delta <= days


def _cover_days(opening: float, outflow: Dict[str, float], days: List[str]):
    """How long today's cash survives the outflow schedule alone."""
    if opening <= 0:
        return 0
    remaining = opening
    for index, day in enumerate(days):
        remaining += outflow[day]
        if remaining < 0:
            return index
    return len(days)
