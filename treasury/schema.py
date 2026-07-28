"""The datasets the dashboard understands, and how to recognise them.

Everything the desk feeds in is a spreadsheet with somebody's column names on
it. A :class:`Dataset` declares the *canonical* field set for one kind of input
plus the synonyms seen in the wild, so an unmodified export usually classifies
itself. When it doesn't, ``mapping.json`` pins it explicitly (see
``mapping.py``) — no code change, ever, to onboard a new source.

Adding a metric input is: add a Dataset here, use it from a metric. Nothing
else in the stack needs to know it exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class Field:
    name: str                       # canonical name used by every metric
    kind: str = "text"              # text | number | date | bool
    synonyms: Tuple[str, ...] = ()  # normalised header spellings seen in exports
    enum: str = ""                  # value-normaliser key (see VALUE_MAPS)
    # "rate" means the sheet may write 3.85 for 3.85%; the loader normalises it
    # to a decimal fraction. Never set this on a price or an FX rate.
    unit: str = ""
    note: str = ""


@dataclass(frozen=True)
class Dataset:
    name: str
    title: str
    fields: Tuple[Field, ...]
    # Fields that must resolve, or the table isn't this dataset. A nested tuple
    # means any-of: ("currency", ("undrawn", "limit")) reads as
    # currency AND (undrawn OR limit).
    key_fields: Tuple[Any, ...] = ()
    name_hints: Tuple[str, ...] = ()   # words in the sheet/file name
    note: str = ""

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


# --- value normalisers --------------------------------------------------------
# Free-text categories from a spreadsheet, folded onto the vocabulary the
# regulatory factor tables key off. Unknown values pass through upper-cased,
# so a new product never silently becomes something it isn't.
VALUE_MAPS: Dict[str, Dict[str, str]] = {
    "product": {
        "deposit": "DEPOSIT", "customer deposit": "DEPOSIT", "td": "DEPOSIT",
        "time deposit": "DEPOSIT", "term deposit": "DEPOSIT",
        "current account": "CURRENT_ACCOUNT", "ca": "CURRENT_ACCOUNT",
        "savings": "CURRENT_ACCOUNT", "casa": "CURRENT_ACCOUNT",
        "call": "CALL_ACCOUNT", "call account": "CALL_ACCOUNT",
        "notice": "CALL_ACCOUNT",
        "loan": "LOAN", "advance": "LOAN", "credit": "LOAN",
        "mortgage": "MORTGAGE",
        "placement": "PLACEMENT", "mm placement": "PLACEMENT",
        "interbank placement": "PLACEMENT", "nostro": "NOSTRO",
        "vostro": "VOSTRO",
        "borrowing": "BORROWING", "mm borrowing": "BORROWING",
        "interbank borrowing": "BORROWING", "taking": "BORROWING",
        "repo": "REPO", "repurchase": "REPO",
        "reverse repo": "REVERSE_REPO", "reverse": "REVERSE_REPO",
        "bond": "BOND", "security": "BOND", "note": "BOND",
        "cash": "CASH", "central bank": "CENTRAL_BANK_RESERVE",
        "reserve": "CENTRAL_BANK_RESERVE",
        "capital": "CAPITAL", "equity": "CAPITAL", "share capital": "CAPITAL",
        "retained earnings": "CAPITAL",
    },
    "counterparty_type": {
        "fi": "FI", "bank": "FI", "financial": "FI",
        "financial institution": "FI", "nbfi": "FI", "fund": "FI",
        "insurance": "FI", "insurer": "FI",
        "corporate": "CORPORATE", "corp": "CORPORATE", "company": "CORPORATE",
        "non-financial corporate": "CORPORATE", "sme": "SME",
        "small business": "SME",
        "retail": "RETAIL", "individual": "RETAIL", "personal": "RETAIL",
        "sovereign": "SOVEREIGN", "government": "SOVEREIGN", "govt": "SOVEREIGN",
        "pse": "PSE", "public sector": "PSE",
        "central bank": "CENTRAL_BANK", "cb": "CENTRAL_BANK",
        "internal": "INTERNAL", "intragroup": "INTERNAL", "hq": "INTERNAL",
        "head office": "INTERNAL",
    },
    "side": {
        "asset": "ASSET", "a": "ASSET", "lending": "ASSET", "lend": "ASSET",
        "long": "ASSET", "receivable": "ASSET", "dr": "ASSET", "debit": "ASSET",
        "liability": "LIABILITY", "l": "LIABILITY", "borrowing": "LIABILITY",
        "borrow": "LIABILITY", "short": "LIABILITY", "payable": "LIABILITY",
        "cr": "LIABILITY", "credit": "LIABILITY", "funding": "LIABILITY",
        "equity": "CAPITAL", "capital": "CAPITAL", "tier 1": "CAPITAL",
    },
    "hqla_level": {
        "l1": "L1", "level 1": "L1", "1": "L1", "hqla1": "L1",
        "l2a": "L2A", "level 2a": "L2A", "2a": "L2A",
        "l2b": "L2B", "level 2b": "L2B", "2b": "L2B",
        "none": "NONE", "non-hqla": "NONE", "n": "NONE", "0": "NONE",
    },
    "facility_type": {
        "credit": "CREDIT", "credit line": "CREDIT", "revolver": "CREDIT",
        "rcf": "CREDIT", "overdraft": "CREDIT",
        "liquidity": "LIQUIDITY", "backstop": "LIQUIDITY", "cp backstop": "LIQUIDITY",
        "trade": "TRADE_FINANCE", "trade finance": "TRADE_FINANCE",
        "guarantee": "GUARANTEE", "lc": "GUARANTEE", "letter of credit": "GUARANTEE",
    },
    "fx_kind": {
        "spot": "SPOT", "fx spot": "SPOT",
        "forward": "FORWARD", "fwd": "FORWARD", "outright": "FORWARD",
        "swap": "SWAP", "fx swap": "SWAP",
        "ndf": "NDF", "option": "OPTION", "option delta": "OPTION",
    },
}

# Products whose economic side is unambiguous, used when the sheet has no
# asset/liability column. Anything not listed must state its own side.
PRODUCT_SIDE: Dict[str, str] = {
    "DEPOSIT": "LIABILITY", "CURRENT_ACCOUNT": "LIABILITY",
    "CALL_ACCOUNT": "LIABILITY", "BORROWING": "LIABILITY",
    "REPO": "LIABILITY", "VOSTRO": "LIABILITY",
    "LOAN": "ASSET", "MORTGAGE": "ASSET", "PLACEMENT": "ASSET",
    "REVERSE_REPO": "ASSET", "BOND": "ASSET", "NOSTRO": "ASSET",
    "CASH": "ASSET", "CENTRAL_BANK_RESERVE": "ASSET",
}


def _f(name, kind="text", *synonyms, enum="", unit="", note=""):
    return Field(name=name, kind=kind, synonyms=tuple(synonyms), enum=enum,
                 unit=unit, note=note)


# --- the datasets -------------------------------------------------------------
POSITIONS = Dataset(
    name="positions",
    title="Balance-sheet positions",
    note="One row per deal/contract. Feeds cash projection, balances, LCR, NSFR and NII.",
    key_fields=("currency", "amount"),
    name_hints=("position", "deal", "contract", "balance", "deposit", "loan",
                "money_market", "mm", "portfolio", "book"),
    fields=(
        _f("deal_id", "text", "deal", "id", "ref", "reference", "trade_id",
           "contract_id", "account", "account_no", "account_number"),
        _f("book", "text", "portfolio", "desk", "business_line", "entity", "unit"),
        _f("counterparty", "text", "client", "customer", "name", "counterparty_name",
           "cpty", "obligor", "issuer", "client_name", "counterpart"),
        _f("counterparty_id", "text", "client_id", "customer_id", "cpty_id", "cif"),
        _f("counterparty_type", "text", "client_type", "customer_type", "cpty_type",
           "segment", "sector", "category", "type_of_counterparty", enum="counterparty_type"),
        _f("product", "text", "product_type", "instrument", "deal_type", "type",
           "transaction_type", enum="product"),
        _f("side", "text", "asset_liability", "a_l", "direction", "dr_cr",
           "position_type", enum="side"),
        _f("currency", "text", "ccy", "cur", "currency_code", "curr"),
        _f("amount", "number", "notional", "principal", "balance", "outstanding",
           "amount_ccy", "nominal", "face_value", "outstanding_balance", "volume"),
        _f("rate", "number", "interest_rate", "coupon", "rate_pct", "yield",
           "all_in_rate", "pricing", unit="rate"),
        _f("start_date", "date", "value_date", "trade_date", "effective_date",
           "settlement_date", "deal_date", "start"),
        _f("maturity_date", "date", "maturity", "end_date", "expiry", "maturity_dt",
           "repayment_date", "due_date", "end"),
        _f("next_reset", "date", "reset_date", "repricing_date", "next_repricing"),
        _f("notice_days", "number", "notice_period", "notice", "call_notice_days"),
        _f("operational", "bool", "operational_deposit", "is_operational", "op_deposit"),
        _f("insured", "bool", "covered", "deposit_insured", "dgs_covered"),
        _f("stable", "bool", "is_stable", "stable_deposit"),
        _f("secured", "bool", "is_secured", "collateralised", "collateralized"),
        _f("collateral_level", "text", "collateral", "collateral_quality",
           "collateral_hqla", enum="hqla_level"),
        _f("risk_weight", "number", "rw", "risk_weight_pct", unit="rate"),
        _f("country", "text", "domicile", "country_code", "jurisdiction",
           "country_of_risk", "location"),
        _f("day_count", "text", "basis", "day_count_basis", "dcc"),
    ),
)

SECURITIES = Dataset(
    name="securities",
    title="Securities portfolio",
    note="HQLA buffer and investment book. Market values drive LCR stock and NSFR RSF.",
    key_fields=("currency", "market_value"),
    name_hints=("securit", "hqla", "bond", "investment", "liquid_asset", "buffer",
                "afs", "htm"),
    fields=(
        _f("security_id", "text", "isin", "cusip", "id", "sec_id", "ticker"),
        _f("name", "text", "security", "description", "instrument", "security_name"),
        _f("issuer", "text", "issuer_name", "obligor"),
        _f("issuer_type", "text", "issuer_category", "sector", "counterparty_type",
           enum="counterparty_type"),
        _f("currency", "text", "ccy", "cur", "currency_code"),
        _f("market_value", "number", "mv", "market_val", "fair_value", "value",
           "amount", "market_value_ccy", "clean_market_value"),
        _f("book_value", "number", "carrying_value", "amortised_cost", "cost"),
        _f("nominal", "number", "notional", "face_value", "par"),
        _f("hqla_level", "text", "hqla", "level", "liquidity_level", "lcr_level",
           enum="hqla_level"),
        _f("rating", "text", "credit_rating", "external_rating", "sp_rating"),
        _f("encumbered", "bool", "is_encumbered", "pledged", "repo_pledged"),
        _f("encumbrance_days", "number", "encumbrance_maturity_days", "pledge_days"),
        _f("maturity_date", "date", "maturity", "redemption_date", "expiry"),
        _f("coupon", "number", "coupon_rate", "rate", "interest_rate", unit="rate"),
        _f("risk_weight", "number", "rw", "risk_weight_pct", unit="rate"),
        _f("country", "text", "domicile", "country_code", "issuer_country"),
    ),
)

FX_TRADES = Dataset(
    name="fx_trades",
    title="FX trades",
    note="Buy/sell legs. Net forward position per currency, added to the structural NOP.",
    key_fields=("buy_currency", "sell_currency"),
    name_hints=("fx", "forward", "swap", "currency_trade", "fx_deal", "fx_position"),
    fields=(
        _f("trade_id", "text", "deal_id", "id", "ref", "reference", "ticket"),
        _f("kind", "text", "type", "trade_type", "product", "instrument",
           enum="fx_kind"),
        _f("book", "text", "portfolio", "desk", "trader"),
        _f("counterparty", "text", "cpty", "client", "customer", "name"),
        _f("buy_currency", "text", "buy_ccy", "bought_currency", "ccy_bought",
           "base_currency", "buy_cur"),
        _f("buy_amount", "number", "buy_amt", "bought_amount", "amount_bought",
           "base_amount", "buy_notional"),
        _f("sell_currency", "text", "sell_ccy", "sold_currency", "ccy_sold",
           "quote_currency", "sell_cur"),
        _f("sell_amount", "number", "sell_amt", "sold_amount", "amount_sold",
           "quote_amount", "sell_notional"),
        _f("rate", "number", "deal_rate", "contract_rate", "fx_rate", "all_in_rate"),
        _f("trade_date", "date", "deal_date", "date"),
        _f("value_date", "date", "settlement_date", "maturity_date", "maturity",
           "delivery_date"),
        _f("country", "text", "counterparty_country", "domicile"),
    ),
)

COMMITMENTS = Dataset(
    name="commitments",
    title="Undrawn commitments",
    note="Committed but undrawn facilities — an LCR outflow and an NSFR RSF add-on.",
    key_fields=("currency", ("undrawn", "limit", "drawn")),
    name_hints=("commitment", "undrawn", "facility", "facilities", "limit_line",
                "off_balance", "contingent"),
    fields=(
        _f("facility_id", "text", "id", "ref", "facility", "line_id"),
        _f("counterparty", "text", "client", "customer", "name", "borrower", "cpty"),
        _f("counterparty_id", "text", "client_id", "customer_id", "cif"),
        _f("counterparty_type", "text", "client_type", "segment", "sector",
           enum="counterparty_type"),
        _f("facility_type", "text", "type", "line_type", "commitment_type",
           enum="facility_type"),
        _f("currency", "text", "ccy", "cur"),
        _f("limit", "number", "facility_limit", "committed", "total_limit",
           "commitment", "line_amount"),
        _f("drawn", "number", "utilised", "utilized", "outstanding", "used"),
        _f("undrawn", "number", "available", "unutilised", "unutilized",
           "undrawn_amount", "headroom"),
        _f("expiry_date", "date", "expiry", "maturity_date", "maturity", "end_date"),
        _f("country", "text", "domicile", "country_code"),
    ),
)

CLIENTS = Dataset(
    name="clients",
    title="Client master",
    note="Static data for the map: where a counterparty sits, what it is, its quota.",
    key_fields=("name", ("country", "city", "counterparty_type", "limit", "sector")),
    name_hints=("client", "counterparty", "customer", "cpty", "master", "static",
                "relationship"),
    fields=(
        _f("counterparty_id", "text", "client_id", "customer_id", "cif", "id",
           "cpty_id"),
        _f("name", "text", "client", "customer", "counterparty", "client_name",
           "counterparty_name", "legal_name"),
        _f("counterparty_type", "text", "client_type", "type", "segment", "category",
           enum="counterparty_type"),
        _f("sector", "text", "industry", "gics", "business"),
        _f("country", "text", "domicile", "country_code", "jurisdiction",
           "country_of_incorporation", "location"),
        _f("city", "text", "town", "place", "office", "branch_city"),
        _f("lat", "number", "latitude", "y"),
        _f("lon", "number", "longitude", "lng", "long", "x"),
        _f("rating", "text", "credit_rating", "internal_rating", "external_rating"),
        _f("limit", "number", "credit_limit", "quota", "exposure_limit",
           "approved_limit", "line"),
        _f("limit_currency", "text", "limit_ccy", "quota_currency"),
        _f("relationship_manager", "text", "rm", "owner", "coverage", "manager"),
        _f("onboarded", "date", "onboarding_date", "since", "start_date"),
        _f("notes", "text", "comment", "comments", "remark", "remarks"),
    ),
)

CASHFLOWS = Dataset(
    name="cashflows",
    title="Known cashflows",
    note="Anything not derivable from a contract: tax, dividends, capex, forecasts.",
    key_fields=("date", "currency", "amount"),
    name_hints=("cashflow", "cash_flow", "flow", "projection", "forecast",
                "cash_projection", "expected"),
    fields=(
        _f("date", "date", "value_date", "flow_date", "settlement_date", "as_of"),
        _f("currency", "text", "ccy", "cur"),
        _f("amount", "number", "cash_flow", "flow", "net_amount", "value", "cashflow"),
        _f("category", "text", "type", "flow_type", "bucket", "class"),
        _f("certainty", "text", "confidence", "status", "probability"),
        _f("counterparty", "text", "client", "customer", "name"),
        _f("description", "text", "narrative", "comment", "detail", "remark"),
    ),
)

FX_RATES = Dataset(
    name="fx_rates",
    title="FX rates",
    note="Spot rates for base-currency conversion. Accepts a pair or a ccy + rate.",
    key_fields=("rate",),
    name_hints=("fx_rate", "spot", "rates_fx", "exchange_rate", "fx_spot", "revaluation"),
    fields=(
        _f("pair", "text", "currency_pair", "ccy_pair", "symbol", "cross"),
        _f("currency", "text", "ccy", "cur", "currency_code", "from_currency"),
        _f("base", "text", "base_currency", "to_currency", "quote_currency",
           "against"),
        _f("rate", "number", "spot", "spot_rate", "fx_rate", "px_last", "mid",
           "closing_rate", "value"),
        _f("date", "date", "as_of", "rate_date", "valuation_date"),
        _f("volatility", "number", "vol", "annual_vol", "sigma", "annualised_vol",
           unit="rate"),
    ),
)

MARKET_RATES = Dataset(
    name="market_rates",
    title="Money-market rates",
    note="Funding curve by tenor. The OCR pipeline's quotes.db loads into this shape.",
    key_fields=("currency", "tenor"),
    name_hints=("market_rate", "curve", "money_market", "funding", "quote",
                "deposit_rate", "yield_curve", "libor", "hibor", "sofr"),
    fields=(
        _f("currency", "text", "ccy", "cur", "currency_code"),
        _f("tenor", "text", "term", "period", "maturity", "bucket"),
        _f("bid", "number", "bid_rate", "bid_price", "borrow", unit="rate"),
        _f("offer", "number", "ask", "offer_rate", "ask_rate", "offer_price", "lend",
           unit="rate"),
        _f("benchmark", "text", "index", "reference", "reference_rate"),
        _f("date", "date", "as_of", "quote_date", "rate_date"),
        _f("source", "text", "supplier", "broker", "provider", "channel"),
    ),
)

REPORT_LINES = Dataset(
    name="report_lines",
    title="Reported figures",
    note=("Already-computed report workbooks (an LCR/NSFR return, a PnL pack). "
          "Loaded as-is so computed numbers can be reconciled against them."),
    key_fields=("report", "line_item", "amount"),
    name_hints=("report", "return", "regulatory", "submission", "reported",
                "lcr_report", "nsfr_report", "summary"),
    fields=(
        _f("report", "text", "report_name", "statement", "schedule", "metric"),
        _f("as_of", "date", "date", "reporting_date", "period", "as_at"),
        _f("section", "text", "part", "block", "group", "category"),
        _f("line_item", "text", "item", "line", "description", "caption", "label"),
        _f("currency", "text", "ccy", "cur"),
        _f("amount", "number", "value", "balance", "unweighted", "unweighted_amount"),
        _f("factor", "number", "weight", "run_off", "runoff_factor", "haircut",
           "asf_factor", "rsf_factor", unit="rate"),
        _f("weighted", "number", "weighted_amount", "weighted_value", "after_factor"),
    ),
)

DATASETS: Tuple[Dataset, ...] = (
    POSITIONS, SECURITIES, FX_TRADES, COMMITMENTS, CLIENTS, CASHFLOWS,
    FX_RATES, MARKET_RATES, REPORT_LINES,
)

BY_NAME: Dict[str, Dataset] = {d.name: d for d in DATASETS}


def dataset(name: str) -> Dataset:
    try:
        return BY_NAME[name]
    except KeyError:                # pragma: no cover - programmer error
        raise KeyError(f"unknown dataset {name!r}; known: {sorted(BY_NAME)}") from None


def normalise_value(enum: str, value: str) -> str:
    """Fold a free-text category onto the canonical vocabulary.

    Exact match first. Failing that, the longest synonym that appears in the
    value wins — but a short synonym has to appear as a whole word, or "ca"
    (current account) would swallow "capital".
    """
    if not value:
        return ""
    table = VALUE_MAPS.get(enum)
    if not table:
        return value.upper()
    key = " ".join(value.strip().lower().replace("_", " ").split())
    if key in table:
        return table[key]
    tokens = set(re.split(r"[^a-z0-9]+", key))
    hits = [(k, v) for k, v in table.items()
            if (k in key if (" " in k or len(k) >= 5) else k in tokens)]
    if hits:
        return max(hits, key=lambda kv: len(kv[0]))[1]
    return value.strip().upper().replace(" ", "_")
