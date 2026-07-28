"""Net Stable Funding Ratio — one-year structural funding, from the same book.

ASF from liabilities and capital, RSF from assets and undrawn commitments, both
driven by ``treasury/regs/nsfr_basel.json``. Same contract as the LCR panel:
every weighted line names its rule.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..rules import load_regs
from . import metric, spec
from .lcr import _reported


def _asf(book, table) -> Tuple[float, List[Dict[str, Any]]]:
    rules = table.rules("asf")
    lines: List[Dict[str, Any]] = []
    total = 0.0
    for row in book.rows("positions"):
        if row.get("side") == "ASSET":
            continue
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        # A demand balance has no residual maturity — it is short funding.
        candidate = {**row, "residual_days": 0 if row.get("is_demand")
                     else row.get("residual_days")}
        rule = rules.match(candidate)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("counterparty") or row.get("product") or "liability",
                      "type": row.get("counterparty_type"), "currency": row.get("currency"),
                      "days": candidate["residual_days"], "amount": amount,
                      "factor": rule.factor, "weighted": weighted, "rule": rule.label})
    return total, lines


def _rsf(book, table) -> Tuple[float, List[Dict[str, Any]]]:
    rules = table.rules("rsf")
    off_balance = table.rules("rsf_offbalance")
    lines: List[Dict[str, Any]] = []
    total = 0.0

    for row in book.rows("positions"):
        if row.get("side") != "ASSET":
            continue
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = rules.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("counterparty") or row.get("product") or "asset",
                      "type": row.get("counterparty_type"), "currency": row.get("currency"),
                      "days": row.get("residual_days"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted, "rule": rule.label})

    for row in book.rows("securities"):
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = rules.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("name") or row.get("security_id") or "security",
                      "type": row.get("issuer_type"), "currency": row.get("currency"),
                      "days": row.get("residual_days"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted, "rule": rule.label})

    for row in book.rows("commitments"):
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = off_balance.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": f"Undrawn — {row.get('counterparty') or 'facility'}",
                      "type": row.get("counterparty_type"), "currency": row.get("currency"),
                      "days": row.get("residual_days"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted, "rule": rule.label})
    return total, lines


def _group(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        entry = grouped.setdefault(line["rule"], {
            "rule": line["rule"], "factor": line["factor"],
            "amount": 0.0, "weighted": 0.0, "count": 0})
        entry["amount"] += line["amount"]
        entry["weighted"] += line["weighted"]
        entry["count"] += 1
    return sorted(grouped.values(), key=lambda r: r["weighted"], reverse=True)


@metric("nsfr", title="Net Stable Funding Ratio", group="Liquidity", order=50,
        needs=("positions",),
        subtitle="Available against required stable funding over a one-year horizon")
def nsfr(book, **params) -> Dict[str, Any]:
    settings = book.settings
    base = settings.base_currency
    table = load_regs(settings.nsfr_table, [settings.regs_dir])
    minimum = float(table.params.get("minimum_ratio", settings.nsfr_minimum))

    asf_total, asf_lines = _asf(book, table)
    rsf_total, rsf_lines = _rsf(book, table)
    ratio = asf_total / rsf_total if rsf_total else None
    status = spec.status_for(ratio, settings.nsfr_warn, minimum)
    reported = _reported(book, ("nsfr", "net stable funding"))

    asf_groups = _group(asf_lines)
    rsf_groups = _group(rsf_lines)
    shortfall = rsf_total * minimum - asf_total

    return {
        "hero": spec.hero("NSFR", ratio, "ratio", status,
                          f"minimum {minimum:.0%} · {table.name}"),
        "kpis": [
            spec.kpi("Available stable funding", asf_total, "money"),
            spec.kpi("Required stable funding", rsf_total, "money"),
            spec.kpi("Headroom", -shortfall, "money",
                     status="good" if shortfall <= 0 else "critical",
                     hint="Stable funding above the minimum requirement"),
            spec.kpi("Weighted ASF factor",
                     spec.safe_div(asf_total, sum(l["amount"] for l in asf_lines)),
                     "pct", hint="Blended factor across all funding"),
            spec.kpi("Weighted RSF factor",
                     spec.safe_div(rsf_total, sum(l["amount"] for l in rsf_lines)),
                     "pct"),
        ] + ([spec.kpi("Reported NSFR", reported, "ratio",
                       status="warning" if ratio and abs(reported - ratio) > 0.02
                       else "good")] if reported is not None else []),
        "charts": [
            spec.bar(f"Available vs required ({base})", ["ASF", "RSF"],
                     [spec.series("Stable funding",
                                  [round(asf_total, 2), round(rsf_total, 2)], slot=1)],
                     format="money",
                     note="The ratio is the left bar over the right bar."),
            spec.hbar("Available stable funding by rule",
                      [g["rule"] for g in asf_groups[:10]],
                      [spec.series("Weighted",
                                   [round(g["weighted"], 2) for g in asf_groups[:10]],
                                   slot=1)], format="money"),
            spec.hbar("Required stable funding by rule",
                      [g["rule"] for g in rsf_groups[:10]],
                      [spec.series("Weighted",
                                   [round(g["weighted"], 2) for g in rsf_groups[:10]],
                                   slot=2)], format="money"),
        ],
        "tables": [
            spec.table("Available stable funding", [
                spec.col("rule", "Rule"), spec.col("factor", "ASF factor", "pct"),
                spec.col("amount", f"Carrying value ({base})", "money"),
                spec.col("weighted", f"ASF ({base})", "money"),
                spec.col("count", "Positions", "number"),
            ], spec.round_all(asf_groups, ["amount", "weighted"]),
                total={"rule": "Total", "weighted": round(asf_total, 2)}),
            spec.table("Required stable funding", [
                spec.col("rule", "Rule"), spec.col("factor", "RSF factor", "pct"),
                spec.col("amount", f"Carrying value ({base})", "money"),
                spec.col("weighted", f"RSF ({base})", "money"),
                spec.col("count", "Positions", "number"),
            ], spec.round_all(rsf_groups, ["amount", "weighted"]),
                total={"rule": "Total", "weighted": round(rsf_total, 2)}),
            spec.table("Largest RSF contributors", [
                spec.col("item", "Item"), spec.col("type", "Counterparty type"),
                spec.col("currency", "Ccy"), spec.col("days", "Days", "number"),
                spec.col("amount", f"Value ({base})", "money"),
                spec.col("factor", "Factor", "pct"),
                spec.col("weighted", f"RSF ({base})", "money"),
                spec.col("rule", "Rule applied"),
            ], spec.round_all(sorted(rsf_lines, key=lambda r: r["weighted"],
                                     reverse=True)[:40], ["amount", "weighted"])),
        ],
        "notes": [
            f"Factor table: {table.name} (version {table.version}). {table.source}",
            "Demand balances are treated as under six months, which is the "
            "conservative reading — a stable retail current account still earns its 90-95% "
            "ASF because the rule keys off counterparty type, not maturity.",
            "Derivative assets and liabilities are not modelled; add them as positions "
            "with an explicit side if they are material for the desk.",
        ],
    }
