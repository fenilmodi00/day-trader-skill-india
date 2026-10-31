import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst", "india")
XBRL = os.path.join(DIR, "RELIANCE-results.xml")
BAD = os.path.join(DIR, "RELIANCE-results-malformed.xml")

# Hand-computed from the fixture numbers, crore = 1e7 rupees.
CUR_END = "2026-06-30"
CUR_START = "2026-04-01"
REV = 2486560000000.0
REV_PY = 2148690000000.0
TOTAL_INCOME = 2541560000000.0
EXPENSES = 220000000000.0 + 82000000000.0 + 145000000000.0 + 1402000000000.0
OP_PROFIT = TOTAL_INCOME - EXPENSES
PBT = 692560000000.0
TAX = 175060000000.0
PROFIT = 517500000000.0
PROFIT_PY = 405000000000.0
SEG_REV = 982000000000.0


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


def report(*extra):
    code, out, err = run(["RELIANCE", "--xbrl", XBRL, "--json"] + list(extra))
    assert code == 0, err
    return json.loads(out)


class Reliance(unittest.TestCase):
    def setUp(self):
        self.r = report()

    def test_period_is_the_current_quarter(self):
        self.assertEqual(self.r["period"]["start"], CUR_START)
        self.assertEqual(self.r["period"]["end"], CUR_END)
        self.assertEqual(self.r["unit"], "INR crore")

    def test_filed_facts(self):
        f = self.r["facts"]
        self.assertEqual(f["revenue"]["value"], REV)
        self.assertEqual(f["revenue"]["concept"], "RevenueFromOperations")
        self.assertEqual(f["total_income"]["value"], TOTAL_INCOME)
        self.assertEqual(f["profit"]["value"], PROFIT)
        self.assertEqual(f["profit_before_tax"]["value"], PBT)
        self.assertEqual(f["tax_expense"]["value"], TAX)
        self.assertEqual(f["operating_profit"]["value"], OP_PROFIT)
        self.assertEqual(sum(f["expenses"].values()), EXPENSES)

    def test_derived_margins_and_growth(self):
        d = self.r["derived"]
        self.assertEqual(d["net_margin"], round(100.0 * PROFIT / REV, 2))
        self.assertEqual(d["operating_margin"],
                         round(100.0 * OP_PROFIT / TOTAL_INCOME, 2))
        self.assertEqual(d["revenue_yoy"],
                         round(100.0 * (REV - REV_PY) / REV_PY, 2))
        self.assertEqual(d["profit_yoy"],
                         round(100.0 * (PROFIT - PROFIT_PY) / PROFIT_PY, 2))

    def test_qoq_needs_a_prior_quarter_the_file_lacks(self):
        d = self.r["derived"]
        self.assertIsNone(d["revenue_qoq"])
        self.assertIsNone(d["profit_qoq"])
        self.assertIn("no comparable prior period in the file for revenue qoq",
                      self.r["missing"])
        self.assertIn("no comparable prior period in the file for profit qoq",
                      self.r["missing"])

    def test_missing_eps_is_reported_not_zero_filled(self):
        self.assertNotIn("eps_basic", self.r["facts"])
        self.assertTrue(any(m.startswith("EPS (")
                            for m in self.r["missing"]))
        self.assertTrue(any(m.startswith("NumberOfShares")
                            for m in self.r["missing"]))

    def test_segment_fact_never_stands_for_the_total(self):
        # the Refining segment tagged 982000000000; the row keeps 2486560000000
        self.assertEqual(self.r["facts"]["revenue"]["value"], REV)

    def test_artifact_keys(self):
        for key in ("symbol", "period", "unit", "facts", "derived", "missing",
                    "conflicts", "source"):
            self.assertIn(key, self.r)
        self.assertEqual(self.r["symbol"], "RELIANCE")


class Conflicts(unittest.TestCase):
    def test_conflicting_duplicate_is_visible(self):
        r = report()
        by_name = {c["name"]: c for c in r["conflicts"]}
        self.assertIn("OtherExpenses", by_name)
        c = by_name["OtherExpenses"]
        self.assertEqual(sorted(c["values"]),
                         sorted([1410000000000.0, 1402000000000.0]))
        # q26dur carries two agreeing facts, q26dur-b dissents
        self.assertEqual(c["contexts"], ["q26dur", "q26dur", "q26dur-b"])
        # the majority value is accepted, the dissent is still reported
        self.assertEqual(r["facts"]["expenses"]["OtherExpenses"],
                         1402000000000.0)


class Cli(unittest.TestCase):
    def test_table_prints_the_numbers(self):
        code, out, err = run(["RELIANCE", "--xbrl", XBRL,
                              "--announcement",
                              "results for the quarter ended June 30, 2026"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE", out)
        self.assertIn("results for the quarter ended June 30, 2026", out)
        self.assertIn("248656.00", out)   # revenue, INR crore
        self.assertIn("51750.00", out)    # profit, INR crore
        self.assertIn("+15.72%", out)
        self.assertIn("+27.78%", out)
        self.assertIn("EPS", out)
        self.assertIn("never zero-filled", out)

    def test_announcement_is_a_label_only(self):
        r = report("--announcement", "unaudited limited review, Q1")
        self.assertEqual(r["period"]["label"], "unaudited limited review, Q1")
        self.assertEqual(r["facts"]["revenue"]["value"], REV)
        self.assertEqual(len(r["missing"]), 4)  # nothing extra filled in

    def test_announcement_file(self):
        tmp = os.path.join(tempfile.gettempdir(), "analyst-ann.txt")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("audited results for the quarter ended June 30, 2026\n")
            r = report("--announcement-file", tmp)
        finally:
            os.remove(tmp)
        self.assertEqual(r["period"]["label"],
                         "audited results for the quarter ended June 30, 2026")

    def test_malformed_xml_fails_with_a_message(self):
        code, out, err = run(["RELIANCE", "--xbrl", BAD])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("trader-analyst:", err)
        self.assertIn("not well-formed XML", err)

    def test_missing_file_fails(self):
        code, out, err = run(["RELIANCE", "--xbrl", os.path.join(DIR, "nope.xml")])
        self.assertEqual(code, 2)
        self.assertIn("trader-analyst:", err)


class PriorPeriods(unittest.TestCase):
    """A file that also carries the previous quarter, for QoQ."""

    def setUp(self):
        # build a small two-quarter instance from the same taxonomy shape
        self.xml = os.path.join(DIR, "two-quarters.xml")
        with open(self.xml, "w", encoding="utf-8") as fh:
            fh.write(TWO_QUARTERS)
        try:
            self.r = report2(self.xml)
        finally:
            os.remove(self.xml)

    def test_qoq_uses_the_previous_quarter(self):
        d = self.r["derived"]
        self.assertEqual(d["revenue_qoq"],
                         round(100.0 * (2000.0 - 1800.0) / 1800.0, 2))
        self.assertEqual(d["profit_qoq"],
                         round(100.0 * (200.0 - 150.0) / 150.0, 2))
        self.assertNotIn("revenue qoq", " ".join(self.r["missing"]))


def report2(path):
    code, out, err = run(["TCS", "--xbrl", path, "--json"])
    assert code == 0, err
    return json.loads(out)


TWO_QUARTERS = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:in-ca="http://xbrl.icai.org/in-ca/2024">
  <context id="cq">
    <entity><identifier scheme="http://www.mca.gov.in">X</identifier></entity>
    <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period>
  </context>
  <context id="pq">
    <entity><identifier scheme="http://www.mca.gov.in">X</identifier></entity>
    <period><startDate>2026-01-01</startDate><endDate>2026-03-31</endDate></period>
  </context>
  <unit id="INR"><measure>iso4217:INR</measure></unit>
  <in-ca:RevenueFromOperations contextRef="cq" unitRef="INR" decimals="-5">2000</in-ca:RevenueFromOperations>
  <in-ca:ProfitLossForPeriod contextRef="cq" unitRef="INR" decimals="-5">200</in-ca:ProfitLossForPeriod>
  <in-ca:RevenueFromOperations contextRef="pq" unitRef="INR" decimals="-5">1800</in-ca:RevenueFromOperations>
  <in-ca:ProfitLossForPeriod contextRef="pq" unitRef="INR" decimals="-5">150</in-ca:ProfitLossForPeriod>
</xbrli:xbrl>
"""


if __name__ == "__main__":
    unittest.main()
