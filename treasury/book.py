"""The Book: everything the desk fed in, cleaned, joined and ready to measure.

Loading is deliberately forgiving — a folder of spreadsheets goes in, and what
comes out is canonical records plus a load log that says, per sheet, what it was
taken to be and which columns went unused. Metrics never touch a cell; they ask
the Book for enriched rows.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import excelio, geo, mapping
from .config import Settings, load_settings
from .schema import DATASETS, PRODUCT_SIDE

# Products with no contractual maturity — callable by the client at any time, so
# they sit in the overnight bucket for liquidity purposes.
DEMAND_PRODUCTS = {"CURRENT_ACCOUNT", "CALL_ACCOUNT", "NOSTRO", "VOSTRO",
                   "CASH", "CENTRAL_BANK_RESERVE"}


@dataclass
class LoadEntry:
    """One sheet's journey into the Book — shown in the Data sources panel."""
    source: str
    sheet: str
    dataset: str = ""
    rows: int = 0
    score: float = 0.0
    explicit: bool = False
    matched: List[str] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)
    problem: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": os.path.basename(self.source), "sheet": self.sheet,
            "dataset": self.dataset, "rows": self.rows,
            "score": round(self.score, 1), "explicit": self.explicit,
            "matched": self.matched, "unmapped": self.unmapped,
            "problem": self.problem,
        }


# --- FX -----------------------------------------------------------------------
class FxBook:
    """Spot rates, quoted the way a market person writes them.

    A ``pair`` of ``USDCHF`` with rate 0.88 means *1 USD = 0.88 CHF*. A row with
    only ``currency`` and ``rate`` is read as *1 currency = rate base-currency*,
    which is what a revaluation sheet normally holds.
    """

    def __init__(self, base: str, rows: Sequence[Dict[str, Any]] = ()):
        self.base = (base or "USD").upper()
        self.direct: Dict[Tuple[str, str], float] = {}
        self.vols: Dict[str, float] = {}
        self.as_of: str = ""
        for row in rows:
            self._add(row)

    def _add(self, row: Dict[str, Any]) -> None:
        rate = row.get("rate")
        pair = (row.get("pair") or "").upper().replace("/", "").replace(" ", "")
        currency = (row.get("currency") or "").upper()
        quote = (row.get("base") or "").upper()
        if len(pair) == 6:
            currency, quote = pair[:3], pair[3:]
        if not quote:
            quote = self.base
        if row.get("volatility") is not None and currency:
            self.vols[currency] = float(row["volatility"])
        if not currency or rate in (None, 0):
            return
        if row.get("date") and str(row["date"]) > self.as_of:
            self.as_of = str(row["date"])
        self.direct[(currency, quote)] = float(rate)
        self.direct[(quote, currency)] = 1.0 / float(rate)

    def rate(self, currency: str, quote: str = "") -> Optional[float]:
        """Units of ``quote`` per 1 unit of ``currency``."""
        currency = (currency or "").upper()
        quote = (quote or self.base).upper()
        if not currency:
            return None
        if currency == quote:
            return 1.0
        if (currency, quote) in self.direct:
            return self.direct[(currency, quote)]
        # Triangulate through the base currency.
        left = self.direct.get((currency, self.base))
        right = self.direct.get((self.base, quote))
        if left is not None and right is not None:
            return left * right
        return None

    def to_base(self, amount: Optional[float], currency: str) -> Optional[float]:
        if amount is None:
            return None
        rate = self.rate(currency, self.base)
        return None if rate is None else amount * rate

    def missing(self, currencies: Iterable[str]) -> List[str]:
        return sorted({c for c in currencies
                       if c and self.rate(c, self.base) is None})

    def as_dict(self) -> Dict[str, float]:
        return {c: r for (c, q), r in sorted(self.direct.items())
                if q == self.base and c != self.base}


# --- the Book -----------------------------------------------------------------
class Book:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()
        self.datasets: Dict[str, List[Dict[str, Any]]] = {d.name: [] for d in DATASETS}
        self.log: List[LoadEntry] = []
        self.warnings: List[str] = []
        self.fx = FxBook(self.settings.base_currency)
        self.as_of: str = self.settings.as_of or ""

    # -- ingest ---------------------------------------------------------------
    def add_table(self, table: excelio.Table,
                  profiles: Sequence[mapping.Profile] = ()) -> LoadEntry:
        entry = LoadEntry(source=table.source, sheet=table.name)
        try:
            resolved = mapping.map_table(table, profiles)
        except Exception as exc:                      # a bad profile shouldn't kill the load
            entry.problem = f"mapping failed: {exc}"
            self.log.append(entry)
            return entry
        if resolved is None:
            entry.problem = "not recognised as a treasury dataset"
            self.log.append(entry)
            return entry
        records = mapping.apply(table, resolved, self.settings.rate_unit)
        self.datasets[resolved.dataset].extend(records)
        entry.dataset = resolved.dataset
        entry.rows = len(records)
        entry.score = resolved.score
        entry.explicit = resolved.explicit
        entry.matched = [f"{t.field} ← {t.header}" for t in sorted(resolved.trace,
                                                                   key=lambda t: t.field)]
        entry.unmapped = resolved.unmapped
        self.log.append(entry)
        return entry

    def load_folder(self, root: str) -> "Book":
        profiles = mapping.load_profiles(root)
        found = False
        for table in excelio.read_tables(root):
            found = True
            self.add_table(table, profiles)
        if not found:
            self.warnings.append(f"no .xlsx/.csv files found under {root!r}")
        return self

    def load_quotes_db(self, path: str, date: str = "") -> "Book":
        """Pull the OCR pipeline's quote history in as money-market rates."""
        if not path or not os.path.exists(path):
            if path:
                self.warnings.append(f"quotes.db not found at {path!r}")
            return self
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "quotes" not in names:
                self.warnings.append(f"{os.path.basename(path)} has no quotes table")
                return self
            if not date:
                row = conn.execute("SELECT MAX(date) d FROM quotes").fetchone()
                date = row["d"] if row and row["d"] else ""
            rows = conn.execute(
                "SELECT source, date, currency, tenor, bid, offer, benchmark "
                "FROM quotes WHERE date = ?", (date,)).fetchall()
        finally:
            conn.close()
        added = 0
        for row in rows:
            bid, offer = excelio.to_number(row["bid"]), excelio.to_number(row["offer"])
            if bid is None and offer is None:
                continue
            self.datasets["market_rates"].append({
                "currency": (row["currency"] or "").upper(),
                "tenor": row["tenor"], "benchmark": row["benchmark"],
                "bid": None if bid is None else bid / 100.0,
                "offer": None if offer is None else offer / 100.0,
                "date": row["date"], "source": row["source"],
                "_origin": f"{os.path.basename(path)}:quotes", "_row": 0,
            })
            added += 1
        self.log.append(LoadEntry(source=path, sheet="quotes", dataset="market_rates",
                                  rows=added, score=100.0, explicit=True,
                                  matched=["from the OCR quote store"]))
        return self

    # -- post-processing ------------------------------------------------------
    def finalise(self) -> "Book":
        self.fx = FxBook(self.settings.base_currency, self.datasets["fx_rates"])
        for currency, vol in self.settings.fx_volatility.items():
            self.fx.vols.setdefault(currency.upper(), vol)
        self.as_of = self.settings.as_of or self._infer_as_of()
        self._enrich_clients()
        self._enrich_positions()
        self._enrich_securities()
        self._enrich_commitments()
        self._check()
        return self

    def _infer_as_of(self) -> str:
        """The most recent date the data actually knows about."""
        candidates = [self.fx.as_of]
        for row in self.datasets["positions"]:
            if row.get("start_date"):
                candidates.append(row["start_date"])
        for row in self.datasets["report_lines"]:
            if row.get("as_of"):
                candidates.append(row["as_of"])
        for row in self.datasets["market_rates"]:
            if row.get("date"):
                candidates.append(row["date"])
        dates = [d for d in candidates if d]
        return max(dates) if dates else _dt.date.today().isoformat()

    def _client_key(self, row: Dict[str, Any]) -> str:
        return (str(row.get("counterparty_id") or "").strip().lower()
                or str(row.get("counterparty") or row.get("name") or "").strip().lower())

    def _enrich_clients(self) -> None:
        self.clients: Dict[str, Dict[str, Any]] = {}
        for row in self.datasets["clients"]:
            point = geo.resolve(row.get("country"), row.get("city"),
                                row.get("lat"), row.get("lon"))
            row["iso2"] = point.iso2 if point else ""
            row["lat"] = point.lat if point else None
            row["lon"] = point.lon if point else None
            row["geo_precision"] = point.precision if point else "unplaced"
            row["country_name"] = geo.country_name(row["iso2"]) if row["iso2"] else ""
            for key in {self._client_key(row),
                        str(row.get("name") or "").strip().lower()}:
                if key:
                    self.clients[key] = row

    def _client_for(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return self.clients.get(self._client_key(row), {})

    def _days_to(self, date: Optional[str]) -> Optional[int]:
        if not date:
            return None
        try:
            then = _dt.date.fromisoformat(str(date))
            now = _dt.date.fromisoformat(self.as_of)
        except ValueError:
            return None
        return (then - now).days

    def _enrich_positions(self) -> None:
        for row in self.datasets["positions"]:
            client = self._client_for(row)
            product = row.get("product") or ""
            side = row.get("side") or PRODUCT_SIDE.get(product, "")
            amount = row.get("amount") or 0.0
            if not side:
                side = "LIABILITY" if amount < 0 else "ASSET"
            row["side"] = side
            row["amount"] = abs(amount)
            row["signed_amount"] = abs(amount) * (1.0 if side == "ASSET" else -1.0)
            row["base_amount"] = self.fx.to_base(row["amount"], row.get("currency"))
            row["signed_base_amount"] = self.fx.to_base(row["signed_amount"],
                                                        row.get("currency"))
            if not row.get("counterparty_type"):
                row["counterparty_type"] = client.get("counterparty_type") or ""
            if not row.get("country"):
                row["country"] = client.get("country") or ""
            row["iso2"] = geo.country_code(row.get("country")) or client.get("iso2", "")
            row["is_demand"] = not row.get("maturity_date") or product in DEMAND_PRODUCTS
            days = self._days_to(row.get("maturity_date"))
            row["residual_days"] = 0 if row["is_demand"] else max(days or 0, 0)
            row["basis"] = self.settings.basis_for(row.get("currency"))
            for flag in ("operational", "insured", "stable", "secured", "encumbered"):
                if row.get(flag) is None:
                    row[flag] = False
            row["hqla_level"] = row.get("collateral_level") or (
                "L1" if product == "CENTRAL_BANK_RESERVE" or product == "CASH" else "")

    def _enrich_securities(self) -> None:
        for row in self.datasets["securities"]:
            value = row.get("market_value")
            if value is None:
                value = row.get("book_value") or row.get("nominal") or 0.0
            row["market_value"] = abs(value)
            row["base_amount"] = self.fx.to_base(row["market_value"], row.get("currency"))
            row["residual_days"] = self._days_to(row.get("maturity_date"))
            if row["residual_days"] is None:
                row["residual_days"] = 3650      # perpetual / equity-like
            row["hqla_level"] = row.get("hqla_level") or "NONE"
            row["encumbered"] = bool(row.get("encumbered"))
            row["product"] = "BOND"
            row["side"] = "ASSET"
            row["iso2"] = geo.country_code(row.get("country"))
            if not row.get("counterparty_type"):
                row["counterparty_type"] = row.get("issuer_type") or ""

    def _enrich_commitments(self) -> None:
        for row in self.datasets["commitments"]:
            undrawn = row.get("undrawn")
            if undrawn is None:
                limit, drawn = row.get("limit") or 0.0, row.get("drawn") or 0.0
                undrawn = max(limit - drawn, 0.0)
            row["undrawn"] = abs(undrawn)
            row["base_amount"] = self.fx.to_base(row["undrawn"], row.get("currency"))
            client = self._client_for(row)
            if not row.get("counterparty_type"):
                row["counterparty_type"] = client.get("counterparty_type") or ""
            row["facility_type"] = row.get("facility_type") or "CREDIT"
            row["iso2"] = geo.country_code(row.get("country")) or client.get("iso2", "")
            row["residual_days"] = self._days_to(row.get("expiry_date")) or 0

    def _check(self) -> None:
        used = {row.get("currency") for row in self.datasets["positions"]}
        used |= {row.get("currency") for row in self.datasets["securities"]}
        used |= {row.get("currency") for row in self.datasets["commitments"]}
        missing = self.fx.missing(c for c in used if c)
        if missing:
            self.warnings.append(
                "no FX rate for " + ", ".join(missing) +
                f" — those positions are excluded from {self.settings.base_currency} totals")
        unplaced = [row.get("name") for row in self.datasets["clients"]
                    if row.get("geo_precision") == "unplaced"]
        if unplaced:
            self.warnings.append(
                f"{len(unplaced)} client(s) could not be placed on the map "
                f"(add a country, city, or lat/lon): {', '.join(str(u) for u in unplaced[:5])}"
                + (" …" if len(unplaced) > 5 else ""))

    # -- access ---------------------------------------------------------------
    def rows(self, dataset: str) -> List[Dict[str, Any]]:
        return self.datasets.get(dataset, [])

    def has(self, *datasets: str) -> bool:
        return all(self.datasets.get(d) for d in datasets)

    def counts(self) -> Dict[str, int]:
        return {name: len(rows) for name, rows in self.datasets.items() if rows}

    def currencies(self) -> List[str]:
        found = {row.get("currency") for row in self.datasets["positions"]}
        found |= {row.get("currency") for row in self.datasets["securities"]}
        for row in self.datasets["fx_trades"]:
            found.add(row.get("buy_currency"))
            found.add(row.get("sell_currency"))
        return sorted(c for c in found if c)

    def metric(self, name: str, **params: Any) -> Dict[str, Any]:
        from .metrics import compute
        return compute(self, name, **params)

    def metrics(self) -> List[Dict[str, Any]]:
        from .metrics import catalogue
        return catalogue(self)


def load_sources(root: str, quotes_db: str = "", **overrides: Any) -> Book:
    """Read a folder of spreadsheets into a finalised Book."""
    settings = load_settings(root, **overrides)
    book = Book(settings)
    book.load_folder(root)
    db = quotes_db or settings.quotes_db
    if db:
        book.load_quotes_db(db)
    return book.finalise()
