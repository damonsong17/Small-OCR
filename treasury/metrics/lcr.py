"""Liquidity Coverage Ratio, computed from source positions — not re-keyed.

Every weighted figure names the rule that produced it, so the ratio can be
argued with. The factor table is ``treasury/regs/lcr_basel.json``; point
``lcr_table`` in settings.json at your own copy to run a jurisdiction's overlay.

Where a reported LCR workbook is also loaded, the panel reconciles the two.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..rules import load_regs
from . import metric, spec


def hqla_stock(book, table) -> Tuple[float, Dict[str, float], List[Dict[str, Any]]]:
    """Post-haircut HQLA by level, then Basel's two caps."""
    rules = table.rules("hqla")
    levels = {"L1": 0.0, "L2A": 0.0, "L2B": 0.0}
    lines: List[Dict[str, Any]] = []

    candidates: List[Dict[str, Any]] = []
    for row in book.rows("securities"):
        if row.get("encumbered"):
            continue
        candidates.append({**row, "_label": row.get("name") or row.get("security_id") or
                           row.get("issuer") or "security",
                           "_amount": row.get("base_amount") or 0.0})
    for row in book.rows("positions"):
        if row.get("product") in {"CASH", "CENTRAL_BANK_RESERVE"}:
            candidates.append({**row, "_label": row.get("product").replace("_", " ").title(),
                               "_amount": row.get("base_amount") or 0.0})

    for row in candidates:
        level = (row.get("hqla_level") or "NONE").upper()
        if level not in levels:
            continue
        rule = rules.match(row)
        weighted = row["_amount"] * rule.factor
        levels[level] += weighted
        lines.append({"item": row["_label"], "level": level,
                      "currency": row.get("currency"),
                      "amount": row["_amount"], "factor": rule.factor,
                      "weighted": weighted, "rule": rule.label})

    l1, l2a, l2b = levels["L1"], levels["L2A"], levels["L2B"]
    # BCBS LCR30: the 15% Level 2B cap, then the 40% Level 2 cap.
    adj_15 = max(l2b - (15.0 / 85.0) * (l1 + l2a), l2b - (15.0 / 60.0) * l1, 0.0)
    adj_40 = max((l2a + l2b - adj_15) - (2.0 / 3.0) * l1, 0.0)
    stock = l1 + l2a + l2b - adj_15 - adj_40
    levels["cap_adjustment"] = -(adj_15 + adj_40)
    return stock, levels, lines


def _outflows(book, table, horizon: int) -> Tuple[float, List[Dict[str, Any]]]:
    rules = table.rules("outflows")
    facilities = table.rules("commitments")
    lines: List[Dict[str, Any]] = []
    total = 0.0

    for row in book.rows("positions"):
        if row.get("side") != "LIABILITY":
            continue
        if not row.get("is_demand") and (row.get("residual_days") or 0) > horizon:
            continue
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = rules.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("counterparty") or row.get("product") or "liability",
                      "currency": row.get("currency"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted,
                      "rule": rule.label, "rule_id": rule.id,
                      "days": row.get("residual_days")})

    for row in book.rows("commitments"):
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = facilities.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": f"Undrawn — {row.get('counterparty') or 'facility'}",
                      "currency": row.get("currency"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted,
                      "rule": rule.label, "rule_id": rule.id,
                      "days": row.get("residual_days")})
    return total, lines


def _inflows(book, table, horizon: int) -> Tuple[float, List[Dict[str, Any]]]:
    rules = table.rules("inflows")
    lines: List[Dict[str, Any]] = []
    total = 0.0
    for row in book.rows("positions"):
        if row.get("side") != "ASSET":
            continue
        if row.get("product") in {"CASH", "CENTRAL_BANK_RESERVE"}:
            continue      # already counted in the HQLA stock
        days = row.get("residual_days")
        if row.get("is_demand") or days is None or days > horizon:
            continue
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = rules.match(row)
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("counterparty") or row.get("product") or "asset",
                      "currency": row.get("currency"), "amount": amount,
                      "factor": rule.factor, "weighted": weighted,
                      "rule": rule.label, "rule_id": rule.id, "days": days})

    for row in book.rows("securities"):
        days = row.get("residual_days")
        if (row.get("hqla_level") or "NONE") != "NONE" or days is None or days > horizon:
            continue
        amount = row.get("base_amount") or 0.0
        if not amount:
            continue
        rule = rules.match({**row, "product": "BOND"})
        weighted = amount * rule.factor
        total += weighted
        lines.append({"item": row.get("name") or "security", "currency": row.get("currency"),
                      "amount": amount, "factor": rule.factor, "weighted": weighted,
                      "rule": rule.label, "rule_id": rule.id, "days": days})
    return total, lines


def _reported(book, names: Tuple[str, ...]) -> Optional[float]:
    """A ratio from a loaded report workbook, if the desk supplied one."""
    for row in book.rows("report_lines"):
        item = str(row.get("line_item") or "").strip().lower()
        report = str(row.get("report") or "").strip().lower()
        if any(n in item or n in report for n in names):
            value = row.get("weighted") if row.get("weighted") is not None else row.get("amount")
            if value is not None:
                return float(value)
    return None


@metric("lcr", title="Liquidity Coverage Ratio", group="Liquidity", order=40,
        needs=("positions",),
        subtitle="HQLA against 30-day stressed net outflows, computed from positions")
def lcr(book, **params) -> Dict[str, Any]:
    settings = book.settings
    base = settings.base_currency
    table = load_regs(settings.lcr_table, [settings.regs_dir])
    horizon = int(table.params.get("horizon_days", settings.survival_horizon_days))
    cap = float(table.params.get("inflow_cap", 0.75))
    minimum = float(table.params.get("minimum_ratio", settings.lcr_minimum))

    stock, levels, hqla_lines = hqla_stock(book, table)
    gross_out, out_lines = _outflows(book, table, horizon)
    gross_in, in_lines = _inflows(book, table, horizon)

    capped_in = min(gross_in, cap * gross_out)
    net_out = max(gross_out - capped_in, 0.0)
    ratio = stock / net_out if net_out else None
    status = spec.status_for(ratio, settings.lcr_warn, minimum)

    reported = _reported(book, ("lcr", "liquidity coverage"))
    surplus = stock - net_out

    by_rule: Dict[str, Dict[str, Any]] = {}
    for line in out_lines:
        entry = by_rule.setdefault(line["rule"], {"rule": line["rule"], "amount": 0.0,
                                                  "weighted": 0.0,
                                                  "factor": line["factor"]})
        entry["amount"] += line["amount"]
        entry["weighted"] += line["weighted"]
    outflow_groups = sorted(by_rule.values(), key=lambda r: r["weighted"], reverse=True)

    bridge_labels = ["HQLA stock", "Outflows", "Inflows (capped)", "Surplus"]
    bridge_values = [stock, -gross_out, capped_in, surplus]

    return {
        "hero": spec.hero("LCR", ratio, "ratio", status,
                          f"minimum {minimum:.0%} · {table.name}"),
        "kpis": [
            spec.kpi("HQLA stock (after haircuts and caps)", stock, "money",
                     hint=f"L1 {levels['L1']:,.0f} · L2A {levels['L2A']:,.0f} · "
                          f"L2B {levels['L2B']:,.0f} · cap adjustment "
                          f"{levels['cap_adjustment'] + 0.0:,.0f}"),
            spec.kpi(f"Gross outflows ({horizon}d)", -gross_out, "money"),
            spec.kpi(f"Inflows after the {cap:.0%} cap", capped_in, "money",
                     hint=f"Uncapped {gross_in:,.0f} {base}"
                          + (" — capped" if gross_in > capped_in else "")),
            spec.kpi("Net cash outflows", -net_out, "money"),
            spec.kpi("Surplus over the minimum", stock - minimum * net_out, "money",
                     status="good" if stock >= minimum * net_out else "critical"),
        ] + ([spec.kpi("Reported LCR", reported, "ratio",
                       status="warning" if ratio and abs(reported - ratio) > 0.02
                       else "good",
                       hint="From the loaded report workbook — variance vs computed: "
                            f"{(ratio - reported):+.2%}" if ratio else "")]
             if reported is not None else []),
        "charts": [
            spec.waterfall("How the ratio is built", bridge_labels,
                           [round(v, 2) for v in bridge_values],
                           kinds=["total", "delta", "delta", "total"], format="money"),
            spec.bar(f"HQLA composition ({base})", ["Level 1", "Level 2A", "Level 2B"],
                     [spec.series("After haircut",
                                  [round(levels["L1"], 2), round(levels["L2A"], 2),
                                   round(levels["L2B"], 2)], slot=1)],
                     format="money",
                     note=("Level 2 is capped at 40% of the stock and Level 2B at 15%; "
                           f"the cap costs {abs(levels['cap_adjustment']):,.0f} {base}."
                           if levels["cap_adjustment"] else "")),
            spec.hbar("Outflows by run-off rule",
                      [g["rule"] for g in outflow_groups[:10]],
                      [spec.series("Weighted outflow",
                                   [round(g["weighted"], 2) for g in outflow_groups[:10]],
                                   slot=1)], format="money"),
        ],
        "tables": [
            spec.table("Outflows", [
                spec.col("item", "Item"), spec.col("currency", "Ccy"),
                spec.col("days", "Days", "number"),
                spec.col("amount", f"Balance ({base})", "money"),
                spec.col("factor", "Run-off", "pct"),
                spec.col("weighted", f"Weighted ({base})", "money"),
                spec.col("rule", "Rule applied"),
            ], spec.round_all(sorted(out_lines, key=lambda r: r["weighted"],
                                     reverse=True)[:60], ["amount", "weighted"]),
                total={"item": "Total", "weighted": round(gross_out, 2)}),
            spec.table("Inflows", [
                spec.col("item", "Item"), spec.col("currency", "Ccy"),
                spec.col("days", "Days", "number"),
                spec.col("amount", f"Balance ({base})", "money"),
                spec.col("factor", "Inflow rate", "pct"),
                spec.col("weighted", f"Weighted ({base})", "money"),
                spec.col("rule", "Rule applied"),
            ], spec.round_all(sorted(in_lines, key=lambda r: r["weighted"],
                                     reverse=True)[:40], ["amount", "weighted"]),
                total={"item": "Total", "weighted": round(gross_in, 2)}),
            spec.table("HQLA", [
                spec.col("item", "Security"), spec.col("level", "Level"),
                spec.col("currency", "Ccy"),
                spec.col("amount", f"Market value ({base})", "money"),
                spec.col("factor", "After haircut", "pct"),
                spec.col("weighted", f"Eligible ({base})", "money"),
                spec.col("rule", "Rule applied"),
            ], spec.round_all(sorted(hqla_lines, key=lambda r: r["weighted"],
                                     reverse=True), ["amount", "weighted"])),
        ],
        "notes": [
            f"Factor table: {table.name} (version {table.version}). {table.source}",
            "Liabilities count when they mature inside the horizon or are callable on "
            "demand; assets count as inflows only when they mature inside it.",
            "Anything the rules don't recognise takes the table's conservative default "
            "and is labelled 'Unmatched' — those rows are the ones to review first.",
        ],
    }
