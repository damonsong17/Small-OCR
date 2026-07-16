"""Coordinate-aware parser for wide matrix quote sheets.

Real broker sheets put several currencies (or markets) side by side: one tenor
row carries many BID/OFFER pairs at once, e.g.

    Tenor | SOFR  BID  OFFER | EURIBOR BID OFFER | CNH HIBOR BID OFFER | ...
    o/n   | 3.53  3.60 3.65  | 2.182   2.16 2.30 | 1.42848   1.30 1.50 | ...

The section parser (parser.py) only reads one currency per row. This parser
instead reconstructs *columns* from the header band and reads every cell by its
x-position, so all currencies are captured and missing cells (shown as '-')
stay blank without breaking alignment. Multiple stacked tables on one page are
handled: each time a new header row appears, the column model is rebuilt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .models import Quote
from .ocr_engine import TextItem
from .parser import QuoteParser, _NUMBER_RE, _TENOR_RE, _clean_number, canonical_tenor

_BID_RE = re.compile(r"(?i)\bbid\b")
_OFFER_RE = re.compile(r"(?i)\b(offer|ask)\b")
_DASHES = {"-", "--", "–", "—", "―", ""}


@dataclass
class Column:
    x: float                # x-centre of the header cell
    kind: str               # 'tenor' | 'index' | 'bid' | 'offer'
    text: str               # raw header text
    currency: str = ""      # currency parsed from the header cell, if any


@dataclass
class Group:
    """One currency/market block: an optional index column + bid/offer."""

    columns: List[Column] = field(default_factory=list)
    segment: str = ""
    currency: str = ""
    benchmark: str = ""

    def col(self, kind: str) -> Optional[Column]:
        for c in self.columns:
            if c.kind == kind:
                return c
        return None

    def index_text(self) -> str:
        return " ".join(c.text for c in self.columns if c.kind == "index").strip()


class TableParser:
    def __init__(self, config: Config):
        self.config = config
        self._helper = QuoteParser(config)  # reuse currency / date detection
        self._codes = {c.upper() for c in config.currencies}

    # -- public API ---------------------------------------------------------
    def parse(
        self,
        items: List[TextItem],
        source_file: str = "",
        page: int = 1,
        supplier: str = "",
    ) -> List[Quote]:
        rows = self._helper.group_rows(items)
        if not rows:
            return []

        full_text = " ".join(it.text for it in items)
        date = self._helper.find_date(full_text) or ""
        supplier = supplier or self.config.supplier

        quotes: List[Quote] = []
        groups: List[Group] = []
        col_tol = 30.0

        for i, row in enumerate(rows):
            if self._is_header_row(row):
                groups, col_tol = self._build_groups(rows, i)
                continue
            if not groups:
                continue
            quotes.extend(
                self._emit_row(row, groups, col_tol, date, supplier, source_file, page)
            )
        return quotes

    # -- header handling ----------------------------------------------------
    @staticmethod
    def _is_header_row(row: List[TextItem]) -> bool:
        """A field-header row carries at least one BID/OFFER label and no prices.

        Requiring 'no price numbers' distinguishes the header from data rows and
        lets single-sided sheets (offer-only or bid-only) still be recognised.
        """
        has_field = any(
            _BID_RE.search(it.text) or _OFFER_RE.search(it.text) for it in row
        )
        has_price = any(_NUMBER_RE.fullmatch(it.text.strip()) for it in row)
        return has_field and not has_price

    def _classify(self, text: str) -> Column:
        raw = text.strip()
        # BID / OFFER, possibly glued to a currency code by OCR on small images
        # (e.g. 'USD BID' -> 'USDBID', 'EUR BID' -> 'EURBID', 'USDOFFER').
        u = raw.upper().replace(" ", "")
        m = re.search(r"(OFFER|ASK|BID)$", u)
        if m:
            kind = "bid" if m.group(1) == "BID" else "offer"
            prefix = u[: m.start()]
            currency = prefix if prefix in self._codes else (self._helper.find_currency(raw) or "")
            return Column(0.0, kind, text, currency)
        if _TENOR_RE.fullmatch(raw) or raw.lower() in {"tenor", "term"}:
            return Column(0.0, "tenor", text, "")
        # otherwise a benchmark / index label, e.g. SOFR / EURIBOR / CNH HIBOR
        return Column(0.0, "index", text, self._helper.find_currency(raw) or "")

    def _build_groups(self, rows: List[List[TextItem]], header_idx: int):
        header = rows[header_idx]
        cols: List[Column] = []
        for it in sorted(header, key=lambda x: x.cx):
            c = self._classify(it.text)
            c.x = it.cx
            cols.append(c)

        cols = self._merge_currency_qualifiers(cols)

        gaps = [b.x - a.x for a, b in zip(cols, cols[1:]) if b.x - a.x > 0]
        gaps.sort()
        col_tol = (gaps[len(gaps) // 2] * 0.6) if gaps else 30.0

        # Upper header rows: currency/market group labels + optional category.
        group_labels = self._row_tokens(rows, header_idx - 1)
        category = self._single_label(rows, header_idx - 2)

        # Structurally split the ordered value columns into groups. A new group
        # starts when a completed block ends: a bid/offer after a bid/offer, or
        # an index after a bid/offer. Consecutive index tokens stay together so
        # a two-word benchmark ("CNH HIBOR") is not split into two groups.
        groups: List[Group] = []
        cur: Optional[Group] = None
        last_kind = None
        for c in cols:
            if c.kind == "tenor":
                continue
            new = (
                cur is None
                or (c.kind == "bid" and last_kind in ("offer", "bid"))
                or (c.kind == "offer" and last_kind == "offer")
                or (c.kind == "index" and last_kind in ("bid", "offer"))
            )
            if new:
                cur = Group()
                groups.append(cur)
            cur.columns.append(c)
            last_kind = c.kind

        self._label_groups(groups, group_labels, category)
        return groups, col_tol

    def _merge_currency_qualifiers(self, cols: List[Column]) -> List[Column]:
        """Fold a bare currency token into the following BID/OFFER column.

        OCR often splits a 'USD BID' header into two boxes ('USD', 'BID'). Left
        alone, the bare 'USD' looks like a benchmark and splits the pair. Here
        we attach its currency to the price column and adopt its (left) x, which
        is where the numbers actually sit.
        """
        out: List[Column] = []
        i = 0
        while i < len(cols):
            c = cols[i]
            nxt = cols[i + 1] if i + 1 < len(cols) else None
            is_bare_ccy = c.kind == "index" and c.text.strip().upper() in self._codes
            if is_bare_ccy and nxt and nxt.kind in ("bid", "offer") and not nxt.currency:
                nxt.currency = c.text.strip().upper()
                nxt.x = c.x
                out.append(nxt)
                i += 2
            else:
                out.append(c)
                i += 1
        return out

    def _label_groups(self, groups, group_labels, category):
        labels = [t for t in group_labels]
        for idx, g in enumerate(groups):
            label = labels[idx] if idx < len(labels) else ""
            label_ccy = self._as_currency(label)
            col_ccy = next((c.currency for c in g.columns if c.currency), "")

            g.currency = col_ccy or label_ccy or self.config.default_currency
            g.benchmark = g.index_text()
            # Segment = the group label when it is not itself a currency,
            # otherwise the page-level category banner (e.g. 'Chinese').
            g.segment = "" if label_ccy else label
            if not g.segment and category and label_ccy:
                g.segment = category

    # -- row emission -------------------------------------------------------
    def _emit_row(self, row, groups, col_tol, date, supplier, source_file, page):
        tenor, _tenor_item = self._row_tenor(row)
        if not tenor:
            return []
        row_text = " ".join(it.text for it in row)

        out: List[Quote] = []
        for g in groups:
            bid_c, offer_c, idx_c = g.col("bid"), g.col("offer"), g.col("index")
            bid = self._value_at(row, bid_c, col_tol) if bid_c else ""
            offer = self._value_at(row, offer_c, col_tol) if offer_c else ""
            rate = self._value_at(row, idx_c, col_tol) if idx_c else ""
            # Keep the row if it has any data: a benchmark fixing with no
            # tradeable bid/offer (e.g. HKD o/n) is still a real quote.
            if not bid and not offer and not rate:
                continue
            used = [
                self._nearest(row, c.x, col_tol)
                for c in (bid_c, offer_c) if c
            ]
            conf = min((it.score for it in used if it), default=0.0)
            out.append(
                Quote(
                    date=date,
                    supplier=supplier,
                    segment=g.segment,
                    currency=g.currency,
                    benchmark=g.benchmark,
                    benchmark_rate=rate,
                    tenor=tenor,
                    bid=bid,
                    offer=offer,
                    source_file=source_file,
                    page=page,
                    confidence=round(conf, 4),
                    raw=row_text,
                )
            )
        return out

    def _row_tenor(self, row):
        """Find the tenor in a row: leftmost cell that *starts* with a tenor.

        Applies configured tenor_fixups to the leftmost cell first (to recover
        OCR misreads like '35' -> '3M'), then an anchored regex match (not
        fullmatch, so a tenor glued to another token is still recognised). Pure
        numbers never match, so values are not mistaken for tenors.
        """
        items = sorted(row, key=lambda x: x.cx)
        fixups = self.config.tenor_fixups
        if items and fixups:
            left = items[0].text.strip()
            fixed = fixups.get(left) or fixups.get(left.lower())
            if fixed:
                return fixed, items[0]
        for it in items:
            m = _TENOR_RE.match(it.text.strip())
            if m:
                return canonical_tenor(m.group(1), self.config.month_units), it
        return "", None

    def _value_at(self, row, column: Optional[Column], tol: float) -> str:
        if column is None:
            return ""
        it = self._nearest(row, column.x, tol)
        if it is None:
            return ""
        text = it.text.strip()
        if text in _DASHES:
            return ""
        m = _NUMBER_RE.search(text)
        return _clean_number(m.group(0)) if m else ""

    @staticmethod
    def _nearest(row, x: float, tol: float) -> Optional[TextItem]:
        best, best_d = None, tol
        for it in row:
            d = abs(it.cx - x)
            if d <= best_d:
                best, best_d = it, d
        return best

    # -- header row helpers -------------------------------------------------
    def _row_tokens(self, rows, idx: int) -> List[str]:
        if idx < 0 or idx >= len(rows):
            return []
        row = rows[idx]
        if self._is_header_row(row):  # guard against grabbing another field row
            return []
        return [it.text.strip() for it in sorted(row, key=lambda x: x.cx)]

    def _single_label(self, rows, idx: int) -> str:
        if idx < 0 or idx >= len(rows):
            return ""
        row = rows[idx]
        if len(row) == 1 and not _NUMBER_RE.search(row[0].text):
            return row[0].text.strip()
        return ""

    def _as_currency(self, text: str) -> str:
        t = text.strip().upper()
        if t in self._codes:
            return t
        return self._helper.find_currency(text) or ""
