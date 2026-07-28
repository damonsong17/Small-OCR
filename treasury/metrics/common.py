"""Shared derivations several metrics agree on.

Keeping these in one place is what stops the cash ladder and the P&L page from
quietly disagreeing about what a deal is worth.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

MONEY_MARKET = {"DEPOSIT", "PLACEMENT", "BORROWING", "LOAN", "REPO",
                "REVERSE_REPO", "CALL_ACCOUNT", "CURRENT_ACCOUNT"}


def as_date(value: Optional[str]) -> Optional[_dt.date]:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def term_days(row: Dict[str, Any], as_of: str) -> int:
    """Days from value date (or today) to maturity — the accrual period."""
    start = as_date(row.get("start_date")) or as_date(as_of)
    end = as_date(row.get("maturity_date"))
    if start is None or end is None:
        return 0
    return max((end - start).days, 0)


def interest_at_maturity(row: Dict[str, Any], as_of: str) -> float:
    """Simple interest settled with principal.

    Money-market deals pay interest at maturity, which is what the desk's book
    mostly is. Contracts longer than a year pay periodically and we have no
    schedule for them, so only principal is projected — see the panel note.
    """
    rate = row.get("rate")
    if not rate:
        return 0.0
    days = term_days(row, as_of)
    if days <= 0 or days > 366:
        return 0.0
    return abs(row.get("amount") or 0.0) * float(rate) * days / (row.get("basis") or 360.0)


def accrual_per_day(row: Dict[str, Any]) -> float:
    """Signed daily interest accrual: positive earns, negative costs."""
    rate = row.get("rate")
    if not rate:
        return 0.0
    sign = 1.0 if row.get("side") == "ASSET" else -1.0
    return sign * abs(row.get("amount") or 0.0) * float(rate) / (row.get("basis") or 360.0)


@dataclass
class Flow:
    date: str
    currency: str
    amount: float               # signed: + inflow, - outflow
    base_amount: float
    category: str
    counterparty: str = ""
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"date": self.date, "currency": self.currency, "amount": self.amount,
                "base_amount": self.base_amount, "category": self.category,
                "counterparty": self.counterparty, "detail": self.detail}


def contractual_flows(book) -> List[Flow]:
    """Every dated cash movement the book can see, signed from our side."""
    flows: List[Flow] = []
    as_of = book.as_of

    for row in book.rows("positions"):
        if row.get("is_demand") or not row.get("maturity_date"):
            continue
        sign = 1.0 if row.get("side") == "ASSET" else -1.0
        principal = abs(row.get("amount") or 0.0)
        interest = interest_at_maturity(row, as_of)
        amount = sign * (principal + interest)
        currency = row.get("currency") or ""
        flows.append(Flow(
            date=str(row["maturity_date"]), currency=currency, amount=amount,
            base_amount=book.fx.to_base(amount, currency) or 0.0,
            category="Maturing asset" if sign > 0 else "Maturing liability",
            counterparty=str(row.get("counterparty") or ""),
            detail=f"{row.get('product') or 'position'} {row.get('deal_id') or ''}".strip(),
        ))

    for row in book.rows("securities"):
        maturity = row.get("maturity_date")
        if not maturity or (row.get("residual_days") or 0) > 3650:
            continue
        amount = abs(row.get("nominal") or row.get("market_value") or 0.0)
        currency = row.get("currency") or ""
        flows.append(Flow(
            date=str(maturity), currency=currency, amount=amount,
            base_amount=book.fx.to_base(amount, currency) or 0.0,
            category="Security redemption",
            counterparty=str(row.get("issuer") or ""),
            detail=str(row.get("name") or row.get("security_id") or ""),
        ))

    for row in book.rows("fx_trades"):
        value_date = row.get("value_date")
        if not value_date:
            continue
        for currency, amount, kind in (
            (row.get("buy_currency"), row.get("buy_amount"), "buy"),
            (row.get("sell_currency"), -abs(row.get("sell_amount") or 0.0), "sell"),
        ):
            if not currency or not amount:
                continue
            amount = abs(amount) if kind == "buy" else amount
            flows.append(Flow(
                date=str(value_date), currency=currency, amount=float(amount),
                base_amount=book.fx.to_base(float(amount), currency) or 0.0,
                category="FX settlement",
                counterparty=str(row.get("counterparty") or ""),
                detail=f"{row.get('kind') or 'FX'} {row.get('trade_id') or ''}".strip(),
            ))

    for row in book.rows("cashflows"):
        date, amount = row.get("date"), row.get("amount")
        if not date or amount is None:
            continue
        currency = row.get("currency") or ""
        flows.append(Flow(
            date=str(date), currency=currency, amount=float(amount),
            base_amount=book.fx.to_base(float(amount), currency) or 0.0,
            category=str(row.get("category") or "Known cashflow"),
            counterparty=str(row.get("counterparty") or ""),
            detail=str(row.get("description") or ""),
        ))

    flows.sort(key=lambda f: f.date)
    return flows


def opening_cash(book) -> Dict[str, float]:
    """Currency -> cash actually available now (vault, central bank, nostro)."""
    balances: Dict[str, float] = {}
    for row in book.rows("positions"):
        if row.get("product") not in {"CASH", "CENTRAL_BANK_RESERVE", "NOSTRO"}:
            continue
        currency = row.get("currency") or ""
        balances[currency] = balances.get(currency, 0.0) + (row.get("signed_amount") or 0.0)
    return balances


def demand_liabilities(book) -> Dict[str, float]:
    """Currency -> balances the client can walk away with today."""
    balances: Dict[str, float] = {}
    for row in book.rows("positions"):
        if row.get("side") != "LIABILITY" or not row.get("is_demand"):
            continue
        currency = row.get("currency") or ""
        balances[currency] = balances.get(currency, 0.0) + abs(row.get("amount") or 0.0)
    return balances


def bucket_index(days: int, buckets: Sequence[Tuple[str, int]]) -> int:
    for index, (_, limit) in enumerate(buckets):
        if days <= limit:
            return index
    return len(buckets) - 1


def date_range(start: str, count: int) -> List[str]:
    first = as_date(start) or _dt.date.today()
    return [(first + _dt.timedelta(days=i)).isoformat() for i in range(count + 1)]


def top_n(rows: Sequence[Dict[str, Any]], key: str, n: int,
          label: str = "name") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Biggest ``n`` rows plus an aggregated remainder (never a 9th hue)."""
    ordered = sorted(rows, key=lambda r: abs(r.get(key) or 0.0), reverse=True)
    head, tail = ordered[:n], ordered[n:]
    rest = {label: f"Other ({len(tail)})", key: sum(r.get(key) or 0.0 for r in tail)}
    return head, rest
