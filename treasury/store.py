"""Headline history, so "monitoring" means something over time.

The spreadsheets are a snapshot; the dashboard keeps its own small SQLite file
of the headline numbers and the FX rates behind them. That gives trend lines on
the ratios and a prior mark for FX revaluation, without asking the desk to keep
old workbooks around.

One file, one table each, no migrations to run on an air-gapped box.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS headline (
    as_of TEXT, metric TEXT, key TEXT, label TEXT,
    value REAL, format TEXT, saved_at TEXT,
    PRIMARY KEY (as_of, metric, key)
);
CREATE TABLE IF NOT EXISTS fx_history (
    as_of TEXT, currency TEXT, rate REAL, base TEXT,
    PRIMARY KEY (as_of, currency, base)
);
CREATE INDEX IF NOT EXISTS ix_headline_metric ON headline(metric, key, as_of);
"""


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writing --------------------------------------------------------------
    def save_headline(self, as_of: str, metric: str,
                      entries: Iterable[Dict[str, Any]]) -> int:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        rows = []
        for index, entry in enumerate(entries):
            value = entry.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            key = entry.get("key") or entry.get("label") or f"kpi_{index}"
            rows.append((as_of, metric, str(key), str(entry.get("label") or key),
                         float(value), str(entry.get("format") or ""), now))
        self.conn.executemany(
            "INSERT OR REPLACE INTO headline(as_of, metric, key, label, value, format, "
            "saved_at) VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def save_fx(self, as_of: str, base: str, rates: Dict[str, float]) -> int:
        rows = [(as_of, currency, float(rate), base)
                for currency, rate in rates.items() if rate]
        self.conn.executemany(
            "INSERT OR REPLACE INTO fx_history(as_of, currency, rate, base) "
            "VALUES (?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def snapshot(self, book, panels: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
        """Persist every panel's hero and KPI values for this as-of date."""
        written = {}
        for metric_id, panel in panels.items():
            entries = []
            hero = panel.get("hero")
            if hero:
                entries.append({**hero, "key": "hero"})
            entries += panel.get("kpis") or []
            written[metric_id] = self.save_headline(book.as_of, metric_id, entries)
        written["_fx"] = self.save_fx(book.as_of, book.fx.base, book.fx.as_dict())
        return written

    # -- reading --------------------------------------------------------------
    def history(self, metric: str, key: str = "hero",
                limit: int = 60) -> List[Tuple[str, float]]:
        rows = self.conn.execute(
            "SELECT as_of, value FROM headline WHERE metric = ? AND key = ? "
            "ORDER BY as_of DESC LIMIT ?", (metric, key, limit)).fetchall()
        return [(r["as_of"], r["value"]) for r in reversed(rows)]

    def dates(self) -> List[str]:
        return [r["as_of"] for r in self.conn.execute(
            "SELECT DISTINCT as_of FROM headline ORDER BY as_of").fetchall()]

    def fx_at(self, as_of: str, base: str) -> Dict[str, float]:
        rows = self.conn.execute(
            "SELECT currency, rate FROM fx_history WHERE as_of = ? AND base = ?",
            (as_of, base)).fetchall()
        return {r["currency"]: r["rate"] for r in rows}

    def previous_fx(self, before: str, base: str) -> Tuple[str, Dict[str, float]]:
        """The most recent stored rate set strictly before ``before``."""
        row = self.conn.execute(
            "SELECT MAX(as_of) d FROM fx_history WHERE as_of < ? AND base = ?",
            (before, base)).fetchone()
        prior = row["d"] if row and row["d"] else ""
        return prior, (self.fx_at(prior, base) if prior else {})


def default_path(settings) -> str:
    """``treasury.db`` beside the data folder unless told otherwise."""
    folder = settings.data_dir if os.path.isdir(settings.data_dir) else "."
    return os.path.join(folder, "treasury.db")


def open_store(settings) -> Optional[SnapshotStore]:
    try:
        return SnapshotStore(default_path(settings))
    except sqlite3.Error:               # a read-only stick shouldn't break the board
        return None
