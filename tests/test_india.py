"""India prototype: free NSE universe, Yahoo .NS bars, IST opening range."""

import contextlib
import datetime
import io
import json
import os
import unittest

from _load import FIX, load

india = load("trader-india", "india")
DATA = os.path.join(FIX, "trader-india")
CHART = os.path.join(DATA, "RELIANCE-5d5m.json.gz")


class India(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars, cls.meta = india.parse_chart(india.read_text(CHART), "RELIANCE")
        cls.day = datetime.date(2026, 10, 26)

    def test_symbol_suffix(self):
        self.assertEqual(india.to_yahoo("reliance"), "RELIANCE.NS")
        self.assertEqual(india.to_yahoo("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(india.to_yahoo("TCS.BO"), "TCS.BO")
        self.assertEqual(india.to_yahoo("^NSEI"), "^NSEI")

    def test_universe_keeps_only_eq(self):
        rows = india.parse_universe(india.read_text(os.path.join(DATA, "EQUITY_L-sample.csv")))
        self.assertEqual([row["symbol"] for row in rows], ["RELIANCE", "TCS"])
        self.assertEqual(rows[0]["isin"], "INE002A01018")
        self.assertEqual(len(india.parse_universe(india.read_text(os.path.join(DATA, "EQUITY_L-sample.csv")), "")), 3)

    def test_bars_are_ist_session_bars(self):
        self.assertEqual(len(self.bars), 370)
        self.assertEqual(self.bars[0]["dt"], datetime.datetime(2026, 10, 26, 9, 15, tzinfo=india.IST))
        self.assertEqual(self.bars[-1]["dt"], datetime.datetime(2026, 10, 30, 15, 25, tzinfo=india.IST))
        self.assertEqual(self.meta["exchangeTimezoneName"], "Asia/Kolkata")
        per_day = {}
        for bar in self.bars:
            self.assertGreaterEqual(bar["dt"].time(), india.OPEN)
            self.assertLess(bar["dt"].time(), india.CLOSE)
            per_day[bar["dt"].date()] = per_day.get(bar["dt"].date(), 0) + 1
        # 15:20 is empty in Yahoo's answer every day; the last bar starts 15:25
        self.assertEqual(sorted(per_day.values()), [74, 74, 74, 74, 74])

    def test_orb_trigger(self):
        res = india.orb(self.bars, "RELIANCE", "5m", 15, self.day)
        self.assertEqual(res["status"], "triggered")
        self.assertEqual((res["range_low"], res["range_high"], res["range_mid"]), (1175.3, 1192.0, 1183.65))
        self.assertEqual(res["trigger_bar"], "09:55")
        self.assertEqual(res["trigger_time"], "10:00")
        self.assertEqual(res["entry"], 1193.6)
        self.assertEqual(res["stop"], 1183.65)
        self.assertEqual(res["risk_per_share"], 9.95)
        self.assertEqual(res["target"], 1203.55)

    def test_orb_expires_without_a_close_above_the_range(self):
        res = india.orb(self.bars, "RELIANCE", "5m", 15, datetime.date(2026, 10, 30))
        self.assertEqual(res["status"], "expired")
        self.assertEqual((res["range_low"], res["range_high"]), (1162.0, 1171.6))
        self.assertNotIn("entry", res)

    def test_orb_waits_while_the_range_forms(self):
        early = [bar for bar in self.bars if bar["dt"].date() == self.day][:2]
        now = datetime.datetime(2026, 10, 26, 9, 22, tzinfo=india.IST)
        res = india.orb(early, "RELIANCE", "5m", 15, self.day, now)
        self.assertEqual(res["status"], "waiting")
        self.assertIn("range still forming", res["reason"])

    def test_cli_offline_orb_prints_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = india.main(["orb", "RELIANCE", "--offline", CHART, "--date", "2026-10-26", "--json"])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["status"], "triggered")
        self.assertEqual(printed["entry"], 1193.6)


if __name__ == "__main__":
    unittest.main()
