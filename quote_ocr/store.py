"""SQLite store for quotes + an incremental file manifest.

One database file is the whole history (a time series) and the same-day source
of truth for pricing. A ``files`` manifest records the SHA-1 of every processed
image so re-runs skip unchanged files instead of rebuilding history.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
from typing import List

from .models import Quote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    source TEXT, date TEXT, segment TEXT, currency TEXT,
    benchmark TEXT, benchmark_rate TEXT, tenor TEXT,
    bid TEXT, offer TEXT, mid TEXT, confidence REAL,
    source_file TEXT, page INTEGER, raw TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, source TEXT, date TEXT,
    sha1 TEXT, n_rows INTEGER, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_key   ON quotes(source, date);
CREATE INDEX IF NOT EXISTS ix_quotes_slice ON quotes(date, currency, tenor);
"""

_COLS = [
    "source", "date", "segment", "currency", "benchmark", "benchmark_rate",
    "tenor", "bid", "offer", "mid", "confidence", "source_file", "page", "raw",
    "ingested_at",
]

# Columns added after the first release. A database already sitting on the
# offline machine must keep working without being rebuilt, so missing columns
# are added in place instead of requiring a fresh ingest.
_ADDED_COLS = {"mid": "TEXT"}


def sha1_of(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class QuoteStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(quotes)")}
        for col, decl in _ADDED_COLS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {decl}")
                print(f"  (quotes.db: added missing column '{col}')")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- incremental manifest ----------------------------------------------
    def is_processed(self, path: str, sha1: str) -> bool:
        row = self.conn.execute(
            "SELECT sha1 FROM files WHERE path = ?", (path,)
        ).fetchone()
        return bool(row and row["sha1"] == sha1)

    def record_file(self, path, source, date, sha1, n_rows) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files(path, source, date, sha1, n_rows, processed_at) "
            "VALUES (?,?,?,?,?,?)",
            (path, source, date, sha1, n_rows, _now()),
        )
        self.conn.commit()

    # -- quotes ------------------------------------------------------------
    def replace_quotes(self, source: str, date: str, quotes: List[Quote]) -> None:
        """Idempotent: replace all rows for this (source, date)."""
        now = _now()
        self.conn.execute(
            "DELETE FROM quotes WHERE source = ? AND date = ?", (source, date)
        )
        self.conn.executemany(
            f"INSERT INTO quotes({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
            [
                (
                    source, date, q.segment, q.currency, q.benchmark, q.benchmark_rate,
                    q.tenor, q.bid, q.offer, getattr(q, "mid", ""), q.confidence,
                    q.source_file, q.page, q.raw, now,
                )
                for q in quotes
            ],
        )
        self.conn.commit()

    # -- read helpers (for pricing / reporting) ----------------------------
    def by_currency(self, date: str, currency: str,
                    source: str = None) -> List[sqlite3.Row]:
        """Quotes for one currency on one date, optionally from one source."""
        sql = ("SELECT source, segment, tenor, bid, offer, mid, benchmark, "
               "benchmark_rate FROM quotes WHERE date = ? AND currency = ?")
        args = [date, currency]
        if source:
            sql += " AND source = ?"
            args.append(source)
        return self.conn.execute(sql + " ORDER BY tenor, source", args).fetchall()

    def sources(self, date: str = None) -> List[str]:
        """Distinct quote sources, optionally for one date."""
        sql = "SELECT DISTINCT source FROM quotes"
        args = ()
        if date:
            sql += " WHERE date = ?"
            args = (date,)
        return [r["source"] for r in self.conn.execute(sql + " ORDER BY source", args)]

    def dates(self) -> List[str]:
        return [
            r["date"]
            for r in self.conn.execute(
                "SELECT DISTINCT date FROM quotes ORDER BY date"
            ).fetchall()
        ]

    def summary(self) -> dict:
        q = self.conn.execute("SELECT COUNT(*) n FROM quotes").fetchone()["n"]
        f = self.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        return {"quotes": q, "files": f}
