"""Turn positioned OCR text into structured quote records.

Strategy (deliberately rule-based so it is transparent and cheap):

1. Reconstruct table rows by clustering text boxes on their vertical centre.
2. Track a "current currency" from section-header rows (a row that names a
   currency but carries no tenor / prices).
3. For each row that contains a tenor and price number(s), emit a Quote with
   bid / offer, inheriting the current currency and page-level date / supplier.

All regexes and the currency list live in config so you can tune without
touching this logic.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .config import Config
from .models import Quote
from .ocr_engine import TextItem

# --- Tenor recognition -------------------------------------------------------
# Short-dated points (O/N, T/N, S/N, S/W -- '.' '/' or nothing as separator,
# '0' a common OCR slip for 'O') plus generic N-unit tenors (1W, 2W, 1S, 6S,
# 3M, 1Y, ...). The unit is captured loosely so desk-specific buckets like
# '1s'/'6s' are not dropped.
_TENOR_RE = re.compile(
    r"""(?ix)
    (?<![A-Z0-9])                      # left boundary
    (
        [0O][./]?N | T[./]?N | S[./]?N | S[./]?W   # O/N, T/N, S/N, S/W
        |
        \d{1,2}\s?[A-Z]{1,4}           # 1W, 2W, 1S, 6S, 3M, 1Y, 12M ...
    )
    (?![A-Z0-9])                       # right boundary
    """,
)

# Canonical unit -> the set of raw unit spellings that map to it. 'M' includes
# 'S' by default because these desks write months as '1s'/'6s'; downstream
# systems (FTP, Bloomberg) expect 'M'. Override via Config.month_units if a
# source uses 'S' to mean something else.
_YEAR_UNITS = {"Y", "YR", "YEAR", "YEARS"}
_WEEK_UNITS = {"W", "WK", "WEEK", "WEEKS"}
_DAY_UNITS = {"D", "DAY", "DAYS"}
_BASE_MONTH_UNITS = {"M", "MO", "MTH", "MONTH", "MONTHS"}


def _canon_unit(unit: str, month_units) -> str:
    months = _BASE_MONTH_UNITS | {u.upper() for u in month_units}
    if unit in months:
        return "M"
    if unit in _YEAR_UNITS:
        return "Y"
    if unit in _WEEK_UNITS:
        return "W"
    if unit in _DAY_UNITS:
        return "D"
    return unit  # unknown unit kept verbatim so nothing is silently lost

# --- Number recognition ------------------------------------------------------
# Prices, forward points (may be signed), thousands separators allowed.
_NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")

# Words that mark a line as a broker / counterparty name.
_SUPPLIER_HINT_RE = re.compile(
    r"(?i)\b(brokers?|broking|capital|securities|markets?|bank|limited|ltd|llc|"
    r"inc|plc|partners|counterpart\w*)\b"
)

# --- Date recognition --------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_PATTERNS = [
    # 2026-07-13 / 2026/7/13
    re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    # 13-Jul-2026 / 13 Jul 2026
    re.compile(r"\b(\d{1,2})[-/\s]([A-Za-z]{3,4})[-/\s](20\d{2})\b"),
    # Jul-13-2026 / Jul 13, 2026
    re.compile(r"\b([A-Za-z]{3,4})[-/\s](\d{1,2})[,-/\s]+(20\d{2})\b"),
    # 13/07/2026 or 13-07-2026 (day-first)
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b"),
    # 2026年7月13日
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]


def canonical_tenor(raw: str, month_units=("M", "S")) -> str:
    """Normalise a matched tenor token, e.g. '0/n'/'o.n' -> 'O/N', '1s' -> '1M'.

    ``month_units`` are the raw unit letters that mean "month" for this source
    (default includes 'S', since these desks write '1s' for one month while
    downstream systems expect '1M'). Year/week/day are also normalised; any
    truly unknown unit is kept verbatim so nothing is silently lost.
    """
    t = raw.upper().replace(" ", "")
    if re.fullmatch(r"[0O][./]?N", t):
        return "O/N"
    if re.fullmatch(r"T[./]?N", t):
        return "T/N"
    if re.fullmatch(r"S[./]?N", t):
        return "S/N"
    if re.fullmatch(r"S[./]?W", t):
        return "S/W"
    m = re.fullmatch(r"(\d{1,2})([A-Z]+)", t)
    if m:
        num, unit = m.group(1), m.group(2)
        return f"{int(num)}{_canon_unit(unit, month_units)}"
    return t


def _clean_number(raw: str) -> str:
    return raw.replace(",", "")


class QuoteParser:
    def __init__(self, config: Config):
        self.config = config
        # Longest-first so multi-letter codes match before being split.
        codes = sorted({c.upper() for c in config.currencies}, key=len, reverse=True)
        alt = "|".join(re.escape(c) for c in codes)
        self._pair_re = re.compile(rf"\b({alt})\s*/\s*({alt})\b", re.I)
        self._concat_re = re.compile(rf"\b({alt})({alt})\b", re.I)
        self._single_re = re.compile(rf"\b({alt})\b", re.I)

    # -- currency -----------------------------------------------------------
    def find_currency(self, text: str) -> Optional[str]:
        m = self._pair_re.search(text)
        if m:
            return f"{m.group(1).upper()}/{m.group(2).upper()}"
        m = self._concat_re.search(text)
        if m:
            return f"{m.group(1).upper()}/{m.group(2).upper()}"
        m = self._single_re.search(text)
        if m:
            return m.group(1).upper()
        return None

    # -- date ---------------------------------------------------------------
    def find_date(self, text: str) -> Optional[str]:
        for i, pat in enumerate(_DATE_PATTERNS):
            m = pat.search(text)
            if not m:
                continue
            try:
                if i == 0 or i == 4:  # year-month-day
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif i == 1:  # day-mon-year
                    d, mo, y = int(m.group(1)), self._month(m.group(2)), int(m.group(3))
                elif i == 2:  # mon-day-year
                    mo, d, y = self._month(m.group(1)), int(m.group(2)), int(m.group(3))
                else:  # i == 3, day-month-year numeric
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if mo and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _month(token: str) -> Optional[int]:
        return _MONTHS.get(token.lower()[:4]) or _MONTHS.get(token.lower()[:3])

    # -- row reconstruction -------------------------------------------------
    def group_rows(self, items: List[TextItem]) -> List[List[TextItem]]:
        if not items:
            return []
        heights = sorted(it.height for it in items if it.height > 0)
        median_h = heights[len(heights) // 2] if heights else 12.0
        tol = max(median_h * self.config.row_y_tolerance, 4.0)

        rows: List[List[TextItem]] = []
        for it in sorted(items, key=lambda x: x.cy):
            placed = False
            for row in rows:
                row_cy = sum(r.cy for r in row) / len(row)
                if abs(it.cy - row_cy) <= tol:
                    row.append(it)
                    placed = True
                    break
            if not placed:
                rows.append([it])
        for row in rows:
            row.sort(key=lambda x: x.x0)
        rows.sort(key=lambda r: sum(x.cy for x in r) / len(r))
        return rows

    # -- main entry ---------------------------------------------------------
    def parse(
        self,
        items: List[TextItem],
        source_file: str = "",
        page: int = 1,
        supplier: str = "",
    ) -> List[Quote]:
        rows = self.group_rows(items)

        full_text = " ".join(it.text for it in items)
        date = self.find_date(full_text) or ""
        supplier = supplier or self.config.supplier or self._guess_supplier(rows)

        quotes: List[Quote] = []
        current_currency = self.config.default_currency

        for row in rows:
            row_text = " ".join(it.text for it in row)

            tenor_match = _TENOR_RE.search(row_text)
            remainder = row_text
            tenor = ""
            if tenor_match:
                tenor = canonical_tenor(tenor_match.group(1), self.config.month_units)
                remainder = row_text[: tenor_match.start()] + " " + row_text[tenor_match.end():]

            numbers = [_clean_number(n) for n in _NUMBER_RE.findall(remainder)]
            row_currency = self.find_currency(row_text)

            # Section header: a currency, no tenor, no prices -> set context.
            if row_currency and not tenor and not numbers:
                current_currency = row_currency
                continue

            if not tenor or len(numbers) < self.config.min_prices_for_quote:
                continue

            bid = numbers[0] if len(numbers) >= 1 else ""
            offer = numbers[1] if len(numbers) >= 2 else ""

            quotes.append(
                Quote(
                    date=date,
                    supplier=supplier,
                    currency=row_currency or current_currency,
                    tenor=tenor,
                    bid=bid,
                    offer=offer,
                    source_file=source_file,
                    page=page,
                    confidence=round(min((it.score for it in row), default=0.0), 4),
                    raw=row_text,
                )
            )
        return quotes

    # -- supplier (best effort) --------------------------------------------
    def _guess_supplier(self, rows: List[List[TextItem]]) -> str:
        """Top-of-sheet line that looks like a broker name.

        Requires a company-like keyword so generic banners (e.g. a 'Chinese'
        segment label) are not mistaken for a supplier.
        """
        for row in rows[:3]:
            text = " ".join(it.text for it in row).strip()
            if len(text) < 3:
                continue
            if _TENOR_RE.search(text) or _NUMBER_RE.search(text):
                continue
            if self.find_currency(text):
                continue
            if _SUPPLIER_HINT_RE.search(text):
                return text
        return ""
