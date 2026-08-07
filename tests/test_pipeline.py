"""Offline robustness tests -- stdlib unittest, no network, no Bloomberg.

    python -m unittest discover -s tests -v      (or: python tests/test_pipeline.py)

These lock in the behaviours that have actually broken before, so a regression
shows up on the offline machine instead of in a trading signal.
"""
from __future__ import annotations

import os
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quote_ocr import sources                        # noqa: E402
from quote_ocr.arb import scan_surface_noarb         # noqa: E402
from quote_ocr.bloomberg import (                    # noqa: E402
    FxPoint, all_pairs, order_pair, pip_of, quote_kind, to_outright,
)
from quote_ocr.docx_reader import parse_docx         # noqa: E402
from quote_ocr.implied import basis_of, implied_base_yield   # noqa: E402
from quote_ocr.parser import canonical_tenor          # noqa: E402
from quote_ocr.pricing import lookup_tenor            # noqa: E402


# --------------------------------------------------------------------------
class TestTenors(unittest.TestCase):
    def test_months_written_as_s(self):
        # the sheets write months as 1s/6s; downstream expects 1M/6M
        self.assertEqual(canonical_tenor("1s"), "1M")
        self.assertEqual(canonical_tenor("6s"), "6M")

    def test_overnight_variants(self):
        for raw in ("o/n", "o.n", "0/N"):
            self.assertEqual(canonical_tenor(raw), "O/N")

    def test_1y_and_12m_are_one_bucket(self):
        # a whole column silently vanished when these were treated as different
        self.assertEqual(lookup_tenor({"1Y": 0.042}, "12M"), 0.042)
        self.assertEqual(lookup_tenor({"12M": 0.042}, "1Y"), 0.042)

    def test_tenors_follow_the_data(self):
        surf = {"USD": {"bid": {"O/N": 0.04, "1W": 0.04, "2W": 0.04, "1M": 0.04,
                                "2M": 0.04, "3M": 0.04, "6M": 0.04, "1Y": 0.04},
                        "offer": {}}}
        self.assertEqual(sources.tenors_in(surf),
                         ["O/N", "1W", "2W", "1M", "2M", "3M", "6M", "1Y"])


# --------------------------------------------------------------------------
class TestCounterpartyAccess(unittest.TestCase):
    def test_no_kyc_names_are_excluded(self):
        for seg in ("Korean", "Taiwanese", "Indian", "ISLAMIC"):
            self.assertFalse(sources.is_tradeable(seg), seg)
            self.assertEqual(sources.access_status(seg), "no_kyc")

    def test_ocr_variants_of_those_names(self):
        for seg in ("ISLAMlC", "lndian", "Taiwanes", "ISLAMIC BANK", "korean "):
            self.assertFalse(sources.is_tradeable(seg), seg)

    def test_tradeable_and_blank_pass(self):
        self.assertTrue(sources.is_tradeable("Chinese"))
        self.assertTrue(sources.is_tradeable(""))

    def test_unknown_counterparty_excluded_by_default(self):
        self.assertEqual(sources.access_status("SomeNewBroker"), "unknown")
        self.assertFalse(sources.is_tradeable("SomeNewBroker"))


# --------------------------------------------------------------------------
class TestFxQuotes(unittest.TestCase):
    def test_points_vs_outright_classification(self):
        self.assertEqual(quote_kind("EUR/CNH 3M Curncy"), "outright")
        self.assertEqual(quote_kind("CNH+1M Curncy"), "outright")
        self.assertEqual(quote_kind("EURCNH3M Curncy"), "points")
        self.assertEqual(quote_kind("CGEU12M Curncy"), "points")

    def test_points_are_converted_to_an_outright(self):
        # EURCNH: spot 7.7972, 3M points -225.07  ->  7.7747
        got = to_outright(-225.0679, 7.7972, "points", 10000.0)
        self.assertAlmostEqual(got, 7.7972 - 225.0679 / 10000, places=9)

    def test_misclassified_points_are_caught(self):
        # declared outright but nowhere near spot -> treated as points
        got = to_outright(-225.0679, 7.7972, "outright", 10000.0)
        self.assertAlmostEqual(got, 7.7972 - 225.0679 / 10000, places=9)

    def test_pip_factor(self):
        self.assertEqual(pip_of("USDCNH"), 10000.0)
        self.assertEqual(pip_of("CNHJPY"), 100.0)

    def test_pair_direction_is_stable(self):
        self.assertEqual(order_pair("CNH", "USD"), "USDCNH")
        self.assertEqual(order_pair("USD", "EUR"), "EURUSD")
        self.assertEqual(order_pair("CNH", "HKD"), "HKDCNH")
        self.assertEqual(len(all_pairs(["USD", "CNH", "CHF", "EUR", "HKD"])), 10)


# --------------------------------------------------------------------------
class TestImpliedYield(unittest.TestCase):
    def test_round_trip(self):
        # a CIP-consistent forward must reverse-solve to the input rate
        S, r_q, r_b, act = 7.1850, 0.0150, 0.0430, 30
        F = S * (1 + r_q * act / 360) / (1 + r_b * act / 360)
        got = implied_base_yield("USDCNH", "1M", act, S, S, F, F, r_q)
        self.assertAlmostEqual(got.bid, r_b, places=9)

    def test_day_count_basis_per_currency(self):
        self.assertEqual(basis_of("CNH"), 360)   # confirmed on FXFA
        self.assertEqual(basis_of("HKD"), 365)
        self.assertEqual(basis_of("USD"), 360)

    def test_act_matters(self):
        # 1M is 33 actual days, not 30; using 30 shifts the yield materially
        S = 7.1850
        a = implied_base_yield("USDCNH", "1M", 33, S, S, 7.1725, 7.1725, 0.015)
        b = implied_base_yield("USDCNH", "1M", 30, S, S, 7.1725, 7.1725, 0.015)
        self.assertGreater(abs(a.bid - b.bid) * 1e4, 10)   # >10bp apart


# --------------------------------------------------------------------------
def _fx(spot, fwd, act=33):
    return FxPoint(tenor="3M", spot=spot, points=(fwd - spot) * 10000,
                   spot_bid=spot, spot_ask=spot, fwd_bid=fwd, fwd_ask=fwd, act=act)


class TestArbScan(unittest.TestCase):
    def setUp(self):
        self.fx = {"USDCNH": {"3M": _fx(7.1850, 7.1500), "1M": _fx(7.1850, 7.1725)}}

    def test_no_arb_surface_yields_nothing(self):
        # derive the CNH rate from CIP with the SAME act the scanner uses,
        # so the surface is exactly consistent -> no edge
        S, F, act, r_usd = 7.1850, 7.1500, 33, 0.0400
        r_cnh = ((F / S) * (1 + r_usd * act / 360) - 1) * 360 / act
        surf = {"USD": {"bid": {"3M": r_usd}, "offer": {"3M": r_usd}},
                "CNH": {"bid": {"3M": r_cnh}, "offer": {"3M": r_cnh}}}
        opps = scan_surface_noarb(surf, self.fx, ["USDCNH"], ["3M"],
                                  threshold_bps=5.0, allow_mismatch=False)
        self.assertEqual(opps, [])

    def test_obvious_arb_is_found(self):
        surf = {"USD": {"bid": {"3M": 0.0800}, "offer": {"3M": 0.0810}},
                "CNH": {"bid": {"3M": 0.0100}, "offer": {"3M": 0.0110}}}
        opps = scan_surface_noarb(surf, self.fx, ["USDCNH"], ["3M"],
                                  threshold_bps=1.0, allow_mismatch=False)
        self.assertTrue(opps)
        self.assertGreater(opps[0].pnl_bps, 0)

    def test_tenor_mismatch_is_labelled(self):
        surf = {"USD": {"bid": {"3M": 0.0800, "1M": 0.0800}, "offer": {"3M": 0.0810, "1M": 0.0810}},
                "CNH": {"bid": {"3M": 0.0100, "1M": 0.0100}, "offer": {"3M": 0.0110, "1M": 0.0110}}}
        opps = scan_surface_noarb(surf, self.fx, ["USDCNH"], ["1M", "3M"],
                                  threshold_bps=1.0, allow_mismatch=True)
        mism = [o for o in opps if o.mismatch]
        self.assertTrue(mism, "expected at least one mismatched-tenor route")
        o = mism[0]
        self.assertIn("TENOR MISMATCH", o.detail)
        self.assertIn("gap/rollover", o.risk_type)
        self.assertNotEqual(o.fx_tenor, o.lend_tenor)
        # every leg must be stated
        for f in (o.borrow_ccy, o.borrow_tenor, o.fx_tenor, o.lend_ccy, o.lend_tenor):
            self.assertTrue(f)

    def test_mismatch_can_be_disabled(self):
        surf = {"USD": {"bid": {"3M": 0.0800, "1M": 0.0800}, "offer": {"3M": 0.0810, "1M": 0.0810}},
                "CNH": {"bid": {"3M": 0.0100, "1M": 0.0100}, "offer": {"3M": 0.0110, "1M": 0.0110}}}
        opps = scan_surface_noarb(surf, self.fx, ["USDCNH"], ["1M", "3M"],
                                  threshold_bps=1.0, allow_mismatch=False)
        self.assertFalse([o for o in opps if o.mismatch])


# --------------------------------------------------------------------------
DOCX_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:tbl>
{rows}
</w:tbl></w:body></w:document>"""


def _row(*cells):
    tcs = "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in cells)
    return f"<w:tr>{tcs}</w:tr>"


def _make_docx(path, rows):
    xml = DOCX_XML.format(rows="".join(_row(*r) for r in rows))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


class TestDocxReader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("_test_tmp.docx")

    def tearDown(self):
        if self.tmp.exists():
            os.remove(self.tmp)

    def test_currency_columns_reference_rates(self):
        _make_docx(self.tmp, [
            ("MONEY MARKET REFERENCE RATES(%)",),
            ("Tenor", "USD", "EUR"),
            ("1M", "3.85", "2.40"),
            ("3M", "3.95", "2.50"),
            ("Money Market Reference is for T+0 value.",),
        ])
        surf, meta = parse_docx(str(self.tmp), verbose=False)
        self.assertAlmostEqual(surf["USD"]["bid"]["1M"], 0.0385)
        self.assertAlmostEqual(surf["EUR"]["bid"]["3M"], 0.0250)
        # both currency columns survive even when they hold identical numbers
        self.assertIn("EUR", surf)
        self.assertEqual(meta["settle"], "T+0")
        self.assertIn("USD", meta["reference_only"])

    def test_identical_values_in_two_columns(self):
        _make_docx(self.tmp, [
            ("Tenor", "USD", "EUR"),
            ("1M", "1.11", "1.11"),
        ])
        surf, _ = parse_docx(str(self.tmp), verbose=False)
        self.assertIn("USD", surf)
        self.assertIn("EUR", surf)   # de-duping on text used to drop this

    def test_bid_offer_layout(self):
        _make_docx(self.tmp, [
            ("USD RATES (%)",),
            ("Tenor", "BID", "OFFER"),
            ("1M", "3.80", "3.90"),
            ("note: T+2",),
        ])
        surf, meta = parse_docx(str(self.tmp), verbose=False)
        self.assertAlmostEqual(surf["USD"]["bid"]["1M"], 0.0380)
        self.assertAlmostEqual(surf["USD"]["offer"]["1M"], 0.0390)
        self.assertEqual(meta["settle"], "T+2")

    def test_bond_section_is_not_funding(self):
        _make_docx(self.tmp, [
            ("Tenor", "USD"),
            ("1M", "3.85"),
            ("USD SENIOR BOND",),
            ("Tenor", "REFERENCE RATES(%)"),
            ("3Y", "4.50"),
            ("5Y", "4.70"),
        ])
        surf, meta = parse_docx(str(self.tmp), verbose=False)
        self.assertIn("1M", surf["USD"]["bid"])
        self.assertNotIn("3Y", surf["USD"]["bid"])   # bond rows must not leak
        self.assertNotIn("5Y", surf["USD"]["bid"])
        self.assertTrue(meta["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
