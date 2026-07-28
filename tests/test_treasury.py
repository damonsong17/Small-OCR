"""Tests for the treasury dashboard.

Standard-library unittest only, so they run on the air-gapped box:

    python -m unittest discover tests -v

The regulatory tests use hand-computable books — small enough that the expected
LCR or NSFR can be worked out on paper from the factor tables, which is the
point: if a factor changes, the arithmetic here should be the thing that fails.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from treasury import excelio, geo, mapping                      # noqa: E402
from treasury.book import Book, FxBook                          # noqa: E402
from treasury.config import Settings                            # noqa: E402
from treasury.metrics import REGISTRY, compute                  # noqa: E402
from treasury.rules import Rule, RuleSet, load_regs             # noqa: E402
from treasury.schema import PRODUCT_SIDE, normalise_value       # noqa: E402

AS_OF = "2026-07-24"


def day(offset: int) -> str:
    return (dt.date.fromisoformat(AS_OF) + dt.timedelta(days=offset)).isoformat()


def make_book(positions=(), securities=(), commitments=(), clients=(),
              fx_rates=(), cashflows=(), fx_trades=(), report_lines=(),
              market_rates=(), **settings_kwargs):
    """A Book built straight from canonical records — no spreadsheets involved."""
    settings = Settings(as_of=AS_OF, base_currency="USD", **settings_kwargs)
    book = Book(settings)
    filled = {
        "positions": positions, "securities": securities,
        "commitments": commitments, "clients": clients, "fx_rates": fx_rates,
        "cashflows": cashflows, "fx_trades": fx_trades,
        "report_lines": report_lines, "market_rates": market_rates,
    }
    for name, rows in filled.items():
        for row in rows:
            record = {f.name: None for f in
                      __import__("treasury.schema", fromlist=["x"]).dataset(name).fields}
            record.update(row)
            book.datasets[name].append(record)
    return book.finalise()


def position(**kwargs):
    base = {"counterparty": "Acme", "counterparty_type": "CORPORATE",
            "product": "DEPOSIT", "currency": "USD", "amount": 1_000_000.0,
            "rate": 0.04, "start_date": day(-30), "maturity_date": day(30)}
    base.update(kwargs)
    return base


# --- excelio ------------------------------------------------------------------
class TestCoercion(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(excelio.to_number("1,234.50"), 1234.5)
        self.assertEqual(excelio.to_number("(2,000)"), -2000.0)
        self.assertEqual(excelio.to_number("5%"), 0.05)
        self.assertEqual(excelio.to_number("1 234"), 1234.0)
        self.assertIsNone(excelio.to_number("n/a"))
        self.assertIsNone(excelio.to_number("USD"))

    def test_dates(self):
        self.assertEqual(excelio.to_date("2026-07-24"), "2026-07-24")
        self.assertEqual(excelio.to_date("24/07/2026"), "2026-07-24")
        self.assertEqual(excelio.to_date(dt.date(2026, 7, 24)), "2026-07-24")
        self.assertEqual(excelio.to_date("24-Jul-2026"), "2026-07-24")
        self.assertIsNone(excelio.to_date("not a date"))

    def test_booleans_and_blanks(self):
        self.assertTrue(excelio.to_bool("Y"))
        self.assertFalse(excelio.to_bool("no"))
        self.assertIsNone(excelio.to_bool("maybe"))
        self.assertTrue(excelio.is_blank("-"))
        self.assertTrue(excelio.is_blank(None))
        self.assertFalse(excelio.is_blank(0))

    def test_header_detection_skips_a_title_row(self):
        rows = [
            ["Treasury deal blotter as at 24 Jul 2026"],
            [],
            ["Deal Ref", "Cpty", "CCY", "Notional Amt"],
            ["D1", "Acme", "USD", 1000],
            ["D2", "Beta", "EUR", 2000],
        ]
        table = excelio.table_from_rows(rows, "Deals", "book.xlsx")
        self.assertEqual(table.header_row, 3)
        self.assertEqual(len(table.records), 2)
        self.assertIn("notional_amt", table.headers)

    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "clients.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Client Name,Client Type,Country\nAcme,Corporate,Germany\n")
            tables = excelio.read_csv(path)
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0].records[0]["client_name"], "Acme")


# --- mapping ------------------------------------------------------------------
class TestMapping(unittest.TestCase):
    def test_abbreviations_expand(self):
        self.assertEqual(mapping.expand("mat_dt"), "maturity_date")
        self.assertEqual(mapping.expand("notional_amt"), "notional_amount")
        self.assertEqual(mapping.expand("cpty_type"), "counterparty_type")

    def test_a_deal_blotter_classifies_as_positions(self):
        rows = [["Deal Ref", "Cpty", "Cpty Type", "Product", "A/L", "CCY",
                 "Notional Amt", "Rate %", "Value Date", "Mat. Dt"],
                ["D1", "Acme", "Corporate", "Time deposit", "Liability", "USD",
                 1_000_000, 4.1, "2026-06-01", "2026-09-01"]]
        table = excelio.table_from_rows(rows, "Deals", "positions.xlsx")
        resolved = mapping.map_table(table)
        self.assertEqual(resolved.dataset, "positions")
        self.assertIn("maturity_date", resolved.columns)
        self.assertIn("amount", resolved.columns)
        self.assertEqual(resolved.unmapped, [])

    def test_a_client_master_does_not_win_over_commitments(self):
        rows = [["Facility ID", "Client", "Client Type", "Line Type", "CCY",
                 "Facility Limit", "Utilised", "Expiry"],
                ["F1", "Acme", "Corporate", "Credit line", "USD", 10_000_000,
                 4_000_000, "2027-01-01"]]
        table = excelio.table_from_rows(rows, "Undrawn", "commitments.xlsx")
        self.assertEqual(mapping.map_table(table).dataset, "commitments")

    def test_rate_columns_become_fractions(self):
        rows = [["CCY", "Notional", "Rate %"], ["USD", 1_000_000, 4.35]]
        table = excelio.table_from_rows(rows, "Deals", "positions.xlsx")
        records = mapping.apply(table, mapping.map_table(table))
        self.assertAlmostEqual(records[0]["rate"], 0.0435)

    def test_a_percent_formatted_rate_is_not_divided_twice(self):
        rows = [["CCY", "Notional", "Rate"], ["USD", 1_000_000, 0.0435]]
        formats = [["General", "General", "General"], ["General", "#,##0", "0.00%"]]
        table = excelio.table_from_rows(rows, "Deals", "positions.xlsx", formats)
        self.assertIn("rate", table.percent_columns)
        records = mapping.apply(table, mapping.map_table(table))
        self.assertAlmostEqual(records[0]["rate"], 0.0435)

    def test_an_fx_spot_is_not_treated_as_a_percentage(self):
        rows = [["Pair", "Currency", "Base", "Spot Rate"],
                ["EURUSD", "EUR", "USD", 1.085]]
        table = excelio.table_from_rows(rows, "FX Rates", "market.xlsx")
        resolved = mapping.map_table(table)
        self.assertEqual(resolved.dataset, "fx_rates")
        self.assertAlmostEqual(mapping.apply(table, resolved)[0]["rate"], 1.085)

    def test_explicit_profile_beats_classification(self):
        rows = [["Ref", "Name", "CCY", "Amount"], ["1", "Acme", "USD", 100]]
        table = excelio.table_from_rows(rows, "Sheet1", "mystery.xlsx")
        profile = mapping.Profile(dataset="positions", file_pattern="mystery.xlsx",
                                  constants={"product": "Time deposit"})
        resolved = mapping.map_table(table, [profile])
        self.assertTrue(resolved.explicit)
        records = mapping.apply(table, resolved)
        self.assertEqual(records[0]["product"], "DEPOSIT")


class TestValueNormalisation(unittest.TestCase):
    def test_short_synonyms_need_a_whole_word(self):
        self.assertEqual(normalise_value("product", "Capital"), "CAPITAL")
        self.assertEqual(normalise_value("product", "CA"), "CURRENT_ACCOUNT")

    def test_longest_match_wins(self):
        self.assertEqual(normalise_value("product", "Reverse repo"), "REVERSE_REPO")
        self.assertEqual(normalise_value("counterparty_type", "Corporate - Large"),
                         "CORPORATE")

    def test_unknown_values_survive(self):
        self.assertEqual(normalise_value("product", "Widget swap"), "WIDGET_SWAP")


# --- rules --------------------------------------------------------------------
class TestRules(unittest.TestCase):
    def test_first_match_wins(self):
        rules = RuleSet("t", [
            Rule("a", "Stable retail", 0.05,
                 {"counterparty_type": "RETAIL", "stable": True}),
            Rule("b", "Retail", 0.10, {"counterparty_type": "RETAIL"}),
        ], default_factor=1.0)
        self.assertEqual(rules.match({"counterparty_type": "RETAIL",
                                      "stable": True}).id, "a")
        self.assertEqual(rules.match({"counterparty_type": "RETAIL"}).id, "b")
        self.assertEqual(rules.match({"counterparty_type": "FI"}).factor, 1.0)

    def test_numeric_operators(self):
        rule = Rule("x", "under 30 days", 1.0, {"residual_days": {"lte": 30}})
        self.assertTrue(rule.matches({"residual_days": 30}))
        self.assertFalse(rule.matches({"residual_days": 31}))
        self.assertFalse(rule.matches({"residual_days": None}))

    def test_negation_and_membership(self):
        rule = Rule("x", "not a repo", 1.0,
                    {"product": {"not": ["REPO", "REVERSE_REPO"]}})
        self.assertTrue(rule.matches({"product": "LOAN"}))
        self.assertFalse(rule.matches({"product": "REPO"}))

    def test_shipped_tables_load(self):
        for name in ("lcr_basel", "nsfr_basel"):
            table = load_regs(name)
            self.assertTrue(table.sets)
            for rules in table.sets.values():
                for rule in rules.rules:
                    self.assertTrue(0.0 <= rule.factor <= 1.0,
                                    f"{name}/{rule.id} factor out of range")


# --- FX -----------------------------------------------------------------------
class TestFx(unittest.TestCase):
    def setUp(self):
        self.fx = FxBook("USD", [
            {"pair": "EURUSD", "rate": 1.10},
            {"currency": "CHF", "base": "USD", "rate": 1.13},
        ])

    def test_conversion_and_inversion(self):
        self.assertAlmostEqual(self.fx.to_base(1_000_000, "EUR"), 1_100_000)
        self.assertAlmostEqual(self.fx.rate("USD", "EUR"), 1 / 1.10)
        self.assertEqual(self.fx.rate("USD"), 1.0)

    def test_triangulation(self):
        self.assertAlmostEqual(self.fx.rate("EUR", "CHF"), 1.10 / 1.13, places=6)

    def test_missing_currency_is_reported_not_guessed(self):
        self.assertIsNone(self.fx.rate("JPY"))
        self.assertEqual(self.fx.missing(["EUR", "JPY"]), ["JPY"])


# --- Book enrichment ----------------------------------------------------------
class TestBook(unittest.TestCase):
    def test_side_is_inferred_from_the_product(self):
        book = make_book(positions=[position(side=None, product="DEPOSIT"),
                                    position(side=None, product="LOAN")])
        self.assertEqual(book.rows("positions")[0]["side"], "LIABILITY")
        self.assertEqual(book.rows("positions")[1]["side"], "ASSET")
        self.assertEqual(book.rows("positions")[0]["signed_amount"], -1_000_000)

    def test_every_product_side_maps_to_a_known_side(self):
        self.assertTrue(set(PRODUCT_SIDE.values()) <= {"ASSET", "LIABILITY"})

    def test_demand_balances_have_no_residual_maturity(self):
        book = make_book(positions=[position(product="CURRENT_ACCOUNT",
                                             maturity_date=None)])
        row = book.rows("positions")[0]
        self.assertTrue(row["is_demand"])
        self.assertEqual(row["residual_days"], 0)

    def test_unpriced_currencies_raise_a_warning(self):
        book = make_book(positions=[position(currency="JPY", amount=1e9)])
        self.assertTrue(any("JPY" in w for w in book.warnings))

    def test_clients_are_geocoded(self):
        book = make_book(clients=[{"name": "Acme", "country": "Hong Kong",
                                   "city": "Hong Kong"}])
        row = book.rows("clients")[0]
        self.assertEqual(row["iso2"], "HK")
        self.assertAlmostEqual(row["lat"], 22.32, places=2)


class TestGeo(unittest.TestCase):
    def test_country_aliases(self):
        for value, expected in (("USA", "US"), ("U.K.", "GB"), ("PRC", "CN"),
                                ("Hong Kong", "HK"), ("CHE", "CH"),
                                ("Switzerland", "CH"), ("", "")):
            self.assertEqual(geo.country_code(value), expected, value)

    def test_city_beats_country_centroid(self):
        point = geo.resolve("Germany", "Frankfurt")
        self.assertEqual(point.precision, "city")
        self.assertAlmostEqual(point.lat, 50.11, places=2)

    def test_explicit_coordinates_win(self):
        point = geo.resolve("Germany", "Frankfurt", lat=1.0, lon=2.0)
        self.assertEqual((point.lat, point.lon, point.precision), (1.0, 2.0, "exact"))

    def test_eurozone_currency_defaults(self):
        self.assertEqual(geo.currency_of("DE"), "EUR")
        self.assertEqual(geo.currency_of("HK"), "HKD")


# --- LCR ----------------------------------------------------------------------
class TestLcr(unittest.TestCase):
    """A book small enough to check on paper.

    HQLA:      100m Level 1 (no haircut)                       ->  100m
    Outflows:  100m FI non-operational, 20 days   x 100%       ->  100m
                50m corporate non-operational, 10 days x 40%   ->   20m
                40m operational deposit          x 25%         ->   10m
               ---------------------------------------------------- 130m
    Inflows:    60m placement with a bank, 15 days x 100%      ->   60m
               (cap is 75% x 130m = 97.5m, so not binding)
    Net:       130m - 60m = 70m   ->  LCR = 100/70 = 1.4286
    """

    def setUp(self):
        self.book = make_book(
            securities=[{"name": "T-bill", "currency": "USD", "market_value": 100e6,
                         "hqla_level": "L1", "issuer_type": "SOVEREIGN",
                         "maturity_date": day(200)}],
            positions=[
                position(counterparty_type="FI", amount=100e6, side="LIABILITY",
                         maturity_date=day(20)),
                position(counterparty_type="CORPORATE", amount=50e6, side="LIABILITY",
                         maturity_date=day(10)),
                position(counterparty_type="FI", amount=40e6, side="LIABILITY",
                         operational=True, product="CALL_ACCOUNT",
                         maturity_date=None),
                position(counterparty_type="FI", amount=60e6, side="ASSET",
                         product="PLACEMENT", maturity_date=day(15)),
            ])
        self.panel = compute(self.book, "lcr")

    def test_ratio(self):
        self.assertEqual(self.panel["error"], "")
        self.assertAlmostEqual(self.panel["hero"]["value"], 100 / 70, places=4)

    def test_components(self):
        by_label = {k["label"]: k["value"] for k in self.panel["kpis"]}
        self.assertAlmostEqual(by_label["HQLA stock (after haircuts and caps)"], 100e6)
        self.assertAlmostEqual(by_label["Gross outflows (30d)"], -130e6)
        self.assertAlmostEqual(by_label["Net cash outflows"], -70e6)

    def test_liabilities_beyond_the_horizon_are_excluded(self):
        book = make_book(positions=[
            position(counterparty_type="FI", amount=100e6, side="LIABILITY",
                     maturity_date=day(45)),
        ])
        outflows = {k["label"]: k["value"] for k in compute(book, "lcr")["kpis"]}
        self.assertEqual(outflows["Gross outflows (30d)"], 0)

    def test_inflows_are_capped_at_75_percent(self):
        book = make_book(positions=[
            position(counterparty_type="FI", amount=100e6, side="LIABILITY",
                     maturity_date=day(20)),
            position(counterparty_type="FI", amount=500e6, side="ASSET",
                     product="PLACEMENT", maturity_date=day(15)),
        ])
        values = {k["label"]: k["value"] for k in compute(book, "lcr")["kpis"]}
        self.assertAlmostEqual(values["Inflows after the 75% cap"], 75e6)
        self.assertAlmostEqual(values["Net cash outflows"], -25e6)

    def test_level_2_is_capped_at_40_percent_of_the_stock(self):
        book = make_book(securities=[
            {"name": "L1", "currency": "USD", "market_value": 100e6,
             "hqla_level": "L1", "maturity_date": day(300)},
            {"name": "L2A", "currency": "USD", "market_value": 400e6,
             "hqla_level": "L2A", "maturity_date": day(300)},
        ], positions=[position(counterparty_type="FI", amount=10e6,
                               side="LIABILITY", maturity_date=day(5))])
        stock = {k["label"]: k["value"] for k in compute(book, "lcr")["kpis"]}[
            "HQLA stock (after haircuts and caps)"]
        # L2 post-haircut is 340m but may not exceed 2/3 of L1 = 66.67m.
        self.assertAlmostEqual(stock, 100e6 + (2 / 3) * 100e6, places=2)

    def test_secured_funding_backed_by_level_1_does_not_run_off(self):
        book = make_book(positions=[
            position(counterparty_type="FI", amount=50e6, side="LIABILITY",
                     product="REPO", secured=True, collateral_level="L1",
                     maturity_date=day(7)),
        ])
        values = {k["label"]: k["value"] for k in compute(book, "lcr")["kpis"]}
        self.assertEqual(values["Gross outflows (30d)"], 0)

    def test_every_weighted_line_names_its_rule(self):
        outflows = [t for t in self.panel["tables"] if t["title"] == "Outflows"][0]
        for row in outflows["rows"]:
            self.assertTrue(row["rule"], row)


# --- NSFR ---------------------------------------------------------------------
class TestNsfr(unittest.TestCase):
    """ASF: 100m capital x 100%  +  200m corporate <1y x 50%   ->  200m
       RSF: 300m corporate loan >1y x 85%                      ->  255m
       NSFR = 200 / 255 = 0.7843
    """

    def setUp(self):
        self.book = make_book(positions=[
            position(side="CAPITAL", product="CAPITAL", amount=100e6,
                     counterparty_type="INTERNAL", maturity_date=None, rate=None),
            position(side="LIABILITY", counterparty_type="CORPORATE", amount=200e6,
                     maturity_date=day(180)),
            position(side="ASSET", product="LOAN", counterparty_type="CORPORATE",
                     amount=300e6, maturity_date=day(700)),
        ])
        self.panel = compute(self.book, "nsfr")

    def test_ratio(self):
        self.assertEqual(self.panel["error"], "")
        self.assertAlmostEqual(self.panel["hero"]["value"], 200 / 255, places=4)

    def test_components(self):
        values = {k["label"]: k["value"] for k in self.panel["kpis"]}
        self.assertAlmostEqual(values["Available stable funding"], 200e6)
        self.assertAlmostEqual(values["Required stable funding"], 255e6)

    def test_short_financial_funding_earns_no_asf(self):
        book = make_book(positions=[
            position(side="LIABILITY", counterparty_type="FI", amount=100e6,
                     maturity_date=day(60)),
        ])
        values = {k["label"]: k["value"] for k in compute(book, "nsfr")["kpis"]}
        self.assertEqual(values["Available stable funding"], 0)

    def test_undrawn_commitments_require_stable_funding(self):
        book = make_book(commitments=[
            {"counterparty": "Acme", "counterparty_type": "CORPORATE",
             "facility_type": "CREDIT", "currency": "USD", "undrawn": 100e6,
             "expiry_date": day(400)},
        ], positions=[position(side="CAPITAL", product="CAPITAL", amount=10e6,
                               maturity_date=None, rate=None)])
        values = {k["label"]: k["value"] for k in compute(book, "nsfr")["kpis"]}
        self.assertAlmostEqual(values["Required stable funding"], 5e6)


# --- FX risk ------------------------------------------------------------------
class TestFxRisk(unittest.TestCase):
    def setUp(self):
        self.book = make_book(
            fx_rates=[{"pair": "EURUSD", "rate": 1.10, "volatility": 0.08}],
            positions=[
                position(currency="EUR", side="ASSET", product="LOAN", amount=50e6),
                position(currency="EUR", side="LIABILITY", amount=20e6),
            ],
            fx_trades=[{"buy_currency": "USD", "buy_amount": 11e6,
                        "sell_currency": "EUR", "sell_amount": 10e6,
                        "value_date": day(30)}],
            capital_base=100e6)
        self.panel = compute(self.book, "fx_risk")

    def test_net_open_position_nets_every_source(self):
        row = [r for r in self.panel["tables"][0]["rows"] if r["currency"] == "EUR"][0]
        self.assertAlmostEqual(row["net"], 20e6)            # 50 - 20 - 10
        self.assertAlmostEqual(row["base_net"], 22e6)       # x 1.10

    def test_the_base_currency_is_not_a_position(self):
        self.assertNotIn("USD", [r["currency"] for r in self.panel["tables"][0]["rows"]])

    def test_var_uses_the_supplied_volatility(self):
        row = [r for r in self.panel["tables"][0]["rows"] if r["currency"] == "EUR"][0]
        expected = 22e6 * 0.08 * 2.3263 * (1 / 252) ** 0.5
        self.assertAlmostEqual(row["var"], expected, places=2)

    def test_limits_come_from_the_capital_base(self):
        row = [r for r in self.panel["tables"][0]["rows"] if r["currency"] == "EUR"][0]
        self.assertAlmostEqual(row["utilisation"], 22e6 / 10e6)   # 10% of 100m
        self.assertTrue(row["breach"])


# --- cash projection ----------------------------------------------------------
class TestCashProjection(unittest.TestCase):
    def setUp(self):
        self.book = make_book(positions=[
            position(product="CENTRAL_BANK_RESERVE", side="ASSET", amount=30e6,
                     maturity_date=None, rate=None),
            position(side="LIABILITY", amount=20e6, maturity_date=day(10), rate=None),
            position(side="ASSET", product="PLACEMENT", amount=5e6,
                     maturity_date=day(5), rate=None),
        ], cashflows=[{"date": day(3), "currency": "USD", "amount": -1e6,
                       "category": "Tax"}])
        self.panel = compute(self.book, "cash_projection")

    def test_opening_cash_is_only_actual_cash(self):
        values = {k["label"]: k["value"] for k in self.panel["kpis"]}
        self.assertAlmostEqual(values["Cash available now"], 30e6)

    def test_net_thirty_day_flow(self):
        values = {k["label"]: k["value"] for k in self.panel["kpis"]}
        self.assertAlmostEqual(values["Net contractual flow, 30 days"], -16e6)

    def test_the_trough_is_the_lowest_projected_balance(self):
        self.assertAlmostEqual(self.panel["hero"]["value"], 14e6)

    def test_interest_settles_with_principal(self):
        book = make_book(positions=[
            position(side="ASSET", product="PLACEMENT", amount=1_000_000,
                     rate=0.036, start_date=day(0), maturity_date=day(30)),
        ])
        table = [t for t in compute(book, "cash_projection")["tables"]
                 if "Largest" in t["title"]][0]
        self.assertAlmostEqual(table["rows"][0]["amount"], 1_003_000, places=0)


# --- P&L ----------------------------------------------------------------------
class TestPnl(unittest.TestCase):
    def test_spread_and_accrual_agree(self):
        book = make_book(positions=[
            position(side="ASSET", product="LOAN", amount=100e6, rate=0.05,
                     start_date=day(-100), maturity_date=day(300)),
            position(side="LIABILITY", amount=100e6, rate=0.03,
                     start_date=day(-100), maturity_date=day(300)),
        ], pnl_period_start=day(-10))
        panel = compute(book, "pnl")
        values = {k["label"]: k["value"] for k in panel["kpis"]}
        self.assertAlmostEqual(values["Asset yield"], 0.05)
        self.assertAlmostEqual(values["Cost of funds"], 0.03)
        self.assertAlmostEqual(values["Net interest spread"], 0.02)
        daily = 100e6 * 0.02 / 360
        self.assertAlmostEqual(values["Net interest, run rate per day"], daily, places=2)
        self.assertAlmostEqual(values["Net interest, period to date"], daily * 10,
                               places=2)


# --- client panels ------------------------------------------------------------
class TestClients(unittest.TestCase):
    def setUp(self):
        self.book = make_book(
            clients=[{"name": "Acme", "counterparty_type": "CORPORATE",
                      "country": "Germany", "city": "Frankfurt", "limit": 10e6,
                      "limit_currency": "USD"},
                     {"name": "Beta Bank", "counterparty_type": "FI",
                      "country": "Hong Kong", "limit": 100e6}],
            positions=[
                position(counterparty="Acme", side="LIABILITY", amount=40e6),
                position(counterparty="Acme", side="ASSET", product="LOAN", amount=8e6),
                position(counterparty="Beta Bank", side="LIABILITY", amount=10e6),
            ],
            commitments=[{"counterparty": "Acme", "counterparty_type": "CORPORATE",
                          "currency": "USD", "undrawn": 5e6,
                          "facility_type": "CREDIT", "expiry_date": day(300)}])

    def test_limit_utilisation_includes_undrawn(self):
        panel = compute(self.book, "client_balances")
        acme = [r for r in panel["tables"][0]["rows"] if r["name"] == "Acme"][0]
        self.assertAlmostEqual(acme["utilisation"], 13 / 10)
        self.assertTrue(acme["breach"])

    def test_both_client_panels_report_the_same_breaches(self):
        balances = {k["label"]: k["value"]
                    for k in compute(self.book, "client_balances")["kpis"]}
        mapped = {k["label"]: k["value"]
                  for k in compute(self.book, "client_map")["kpis"]}
        self.assertEqual(balances["Limit breaches"],
                         mapped["Counterparty limit breaches"])

    def test_the_map_places_every_client(self):
        panel = compute(self.book, "client_map")
        points = panel["map"]["points"]
        self.assertEqual(len(points), 2)
        self.assertEqual({p["iso2"] for p in points}, {"DE", "HK"})
        self.assertTrue(all(p["lat"] is not None for p in points))

    def test_country_overlay_sections_build(self):
        from treasury import overlays
        from treasury.metrics.clients_map import country_context
        context = country_context(self.book, "DE")
        sections = overlays.build(self.book, "DE", context)
        self.assertIn("regulation", [s["id"] for s in sections])
        self.assertIn("quota", [s["id"] for s in sections])


# --- registry -----------------------------------------------------------------
class TestRegistry(unittest.TestCase):
    def test_every_metric_renders_or_explains_itself(self):
        empty = make_book()
        for metric_id in REGISTRY:
            panel = compute(empty, metric_id)
            self.assertNotIn("traceback", panel,
                             f"{metric_id} raised: {panel.get('error')}")

    def test_a_metric_with_missing_data_says_so(self):
        panel = compute(make_book(), "lcr")
        self.assertIn("needs data", panel["error"])

    def test_an_unknown_metric_is_not_an_exception(self):
        self.assertIn("no metric", compute(make_book(), "nope")["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
