"""Desk settings. Everything a four-person team would argue about lives here.

Defaults are Basel-ish and deliberately conservative. Override any of them with
a ``settings.json`` in the data folder — no code change, and the file travels
with the data on the USB stick:

    {"base_currency": "USD", "capital_base": 250000000,
     "lcr_table": "lcr_hkma", "nop_limit_pct_per_currency": 0.05}
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Tuple

SETTINGS_FILENAMES = ("settings.json", "treasury_settings.json")

# Money-market day-count basis. Most of the world is ACT/360; the sterling bloc
# and much of Asia is ACT/365.
BASIS_365 = ("GBP", "HKD", "SGD", "AUD", "NZD", "CNY", "CNH", "THB", "TWD",
             "ZAR", "MYR", "INR", "PHP", "ILS", "PLN")


@dataclass
class Settings:
    # --- reporting -----------------------------------------------------------
    base_currency: str = "USD"
    as_of: str = ""                       # blank = latest date found in the data
    entity: str = "Treasury"

    # --- capital & limits ----------------------------------------------------
    capital_base: float = 0.0             # in base currency; enables % -of-capital limits
    nop_limit_pct_per_currency: float = 0.10
    nop_limit_pct_aggregate: float = 0.20
    concentration_top_n: int = 10
    depositor_concentration_warn: float = 0.30   # top-N share of total funding

    # --- liquidity -----------------------------------------------------------
    lcr_table: str = "lcr_basel"
    nsfr_table: str = "nsfr_basel"
    lcr_minimum: float = 1.0
    lcr_warn: float = 1.10                # amber below this, green above
    nsfr_minimum: float = 1.0
    nsfr_warn: float = 1.05
    survival_horizon_days: int = 30

    # --- cash projection -----------------------------------------------------
    cash_daily_days: int = 30             # length of the day-by-day ladder
    cash_buckets: Tuple[Tuple[str, int], ...] = (
        ("O/N", 1), ("2-7D", 7), ("8D-1M", 30), ("1-3M", 90),
        ("3-6M", 180), ("6-12M", 365), (">1Y", 36500),
    )

    # --- FX risk -------------------------------------------------------------
    var_confidence: float = 0.99
    var_horizon_days: int = 1
    default_fx_volatility: float = 0.08   # annualised, used when no vol is supplied
    fx_volatility: Dict[str, float] = field(default_factory=dict)
    fx_shock_pct: float = 0.01            # sensitivity: adverse move per currency

    # --- P&L -----------------------------------------------------------------
    pnl_period_start: str = ""            # blank = first day of the as-of month

    # How interest-rate columns are stored in the source sheets:
    # "auto" trusts Excel's percent formatting and otherwise reads a plain
    # number as percent (3.85 -> 3.85%); "fraction" if your export already
    # writes 0.0385.
    rate_unit: str = "auto"

    # --- plumbing ------------------------------------------------------------
    data_dir: str = "data/treasury"
    quotes_db: str = ""                   # optional: OCR pipeline's quotes.db
    regs_dir: str = ""                    # optional: your own factor tables
    host: str = "127.0.0.1"
    port: int = 8787

    # --- provenance ----------------------------------------------------------
    loaded_from: str = ""

    # -- helpers --------------------------------------------------------------
    def basis_for(self, currency: str) -> float:
        return 365.0 if (currency or "").upper() in BASIS_365 else 360.0

    def volatility_for(self, currency: str) -> float:
        return self.fx_volatility.get((currency or "").upper(), self.default_fx_volatility)

    def bucket_labels(self) -> List[str]:
        return [label for label, _ in self.cash_buckets]

    def as_dict(self) -> Dict[str, Any]:
        blob = asdict(self)
        blob["cash_buckets"] = [list(b) for b in self.cash_buckets]
        return blob


def load_settings(root: str = "", **overrides: Any) -> Settings:
    """Settings from ``<root>/settings.json``, then keyword overrides."""
    values: Dict[str, Any] = {}
    source = ""
    if root:
        folder = root if os.path.isdir(root) else os.path.dirname(os.path.abspath(root))
        for filename in SETTINGS_FILENAMES:
            path = os.path.join(folder, filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    values = {k: v for k, v in json.load(fh).items()
                              if not k.startswith("_")}
                source = path
                break
    values.update({k: v for k, v in overrides.items() if v not in (None, "")})
    if "cash_buckets" in values:
        values["cash_buckets"] = tuple(tuple(b) for b in values["cash_buckets"])

    known = {f for f in Settings.__dataclass_fields__}
    unknown = sorted(set(values) - known)
    settings = Settings(**{k: v for k, v in values.items() if k in known})
    settings.loaded_from = source
    if root and not overrides.get("data_dir"):
        settings.data_dir = root
    if unknown:                            # loud, but never fatal on the desk
        print(f"[treasury] ignoring unknown settings: {', '.join(unknown)}")
    return settings
