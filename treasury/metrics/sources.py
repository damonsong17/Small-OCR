"""Data sources — what was loaded, what it was taken to be, what was ignored.

A number nobody can trace is a number nobody trusts. This panel is the audit
trail: every sheet, the dataset it classified as, the column mapping, and the
columns that went unused (usually the first clue that a mapping needs pinning).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..schema import BY_NAME
from . import REGISTRY, metric, spec


@metric("sources", title="Data sources", group="Data", order=900,
        subtitle="Every sheet loaded, how it was mapped, and what went unused")
def sources(book, **params) -> Dict[str, Any]:
    entries = [entry.as_dict() for entry in book.log]
    loaded = [e for e in entries if e["dataset"]]
    skipped = [e for e in entries if not e["dataset"]]
    counts = book.counts()

    rows = []
    for entry in entries:
        rows.append({
            "source": entry["source"], "sheet": entry["sheet"],
            "dataset": BY_NAME[entry["dataset"]].title if entry["dataset"] else "—",
            "rows": entry["rows"],
            "how": "pinned in mapping.json" if entry["explicit"] else (
                "auto-classified" if entry["dataset"] else "skipped"),
            "mapped": len(entry["matched"]),
            "unused": ", ".join(entry["unmapped"]) or "—",
            "problem": entry["problem"] or "",
        })

    detail: List[Dict[str, Any]] = []
    for entry in entries:
        for pair in entry["matched"]:
            field, _, header = pair.partition(" ← ")
            detail.append({"sheet": f"{entry['source']} · {entry['sheet']}",
                           "field": field, "header": header,
                           "dataset": entry["dataset"]})

    unmapped_total = sum(len(e["unmapped"]) for e in entries)
    return {
        "kpis": [
            spec.kpi("Sheets loaded", len(loaded), "number",
                     status="good" if loaded else "critical"),
            spec.kpi("Sheets skipped", len(skipped), "number",
                     status="warning" if skipped else "good",
                     hint="Not recognised as a treasury dataset — pin them in mapping.json"),
            spec.kpi("Records", sum(counts.values()), "number"),
            spec.kpi("Columns not used", unmapped_total, "number",
                     status="warning" if unmapped_total > 12 else "neutral",
                     hint="Either genuinely irrelevant, or a synonym worth adding"),
            spec.kpi("Metrics available",
                     sum(1 for m in REGISTRY.values() if not m.missing(book)), "number",
                     hint=f"of {len(REGISTRY)} registered"),
        ],
        "charts": [
            spec.hbar("Records by dataset",
                      [BY_NAME[name].title for name in sorted(counts, key=counts.get,
                                                              reverse=True)],
                      [spec.series("Rows", [counts[name] for name in
                                            sorted(counts, key=counts.get, reverse=True)],
                                   slot=1)], format="number"),
        ],
        "tables": [
            spec.table("Sheets", [
                spec.col("source", "File"), spec.col("sheet", "Sheet"),
                spec.col("dataset", "Read as"), spec.col("rows", "Rows", "number"),
                spec.col("how", "How"), spec.col("mapped", "Columns mapped", "number"),
                spec.col("unused", "Columns ignored"), spec.col("problem", "Problem"),
            ], rows),
            spec.table("Column mapping", [
                spec.col("sheet", "Sheet"), spec.col("dataset", "Dataset"),
                spec.col("field", "Canonical field"),
                spec.col("header", "Source column"),
            ], detail, note="Pin anything wrong in mapping.json — the file wins over "
                            "automatic classification."),
        ],
        "notes": [
            f"As-of date: {book.as_of}. Base currency: {book.settings.base_currency}. "
            f"Settings: {book.settings.loaded_from or 'defaults (no settings.json found)'}.",
        ] + book.warnings,
    }
