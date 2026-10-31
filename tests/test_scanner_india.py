"""T4: trader-scanner, the NSE port. Universe, bhavcopy daily scan, Upstox ORB. No network."""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import FIX, load, core

core()  # repo root on sys.path

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner", "india")
BHAVS = [os.path.join(SDIR, "sec_bhavdata_full_%s.csv" % s)
         for s in ("21102026", "22102026", "23102026", "26102026", "27102026")]
FINAL = BHAVS[-1]
EMPTY_BHAV = os.path.join(SDIR, "sec_bhavdata_full_28102026.csv")
ORB_FILE = os.path.join(SDIR, "RELIANCE-1m-2026-10-21-27.json")
KEY = "NSE_EQ|INE002A01018"

MASTER_ROWS = [
    {"instrument_key": KEY, "exchange_token": "2885", "trading_symbol": "RELIANCE",
     "isin": "INE002A01018", "instrument_type": "EQ", "lot_size": 1.0, "tick_size": 0.05,
     "freeze_quantity": 11111.0},
    {"instrument_key": "NSE_EQ|INE467B01029", "exchange_token": "11536", "trading_symbol": "TCS",
     "isin": "INE467B01029", "instrument_type": "EQ", "lot_size": 1.0, "tick_size": 0.05,
     "freeze_quantity": 10000.0},
    {"instrument_key": "NSE_EQ|INE423A01024", "exchange_token": "25", "trading_symbol": "ADANIENT",
     "isin": "INE423A01024", "instrument_type": "EQ", "lot_size": 1.0, "tick_size": 0.05,
     "freeze_quantity": 4833.0},
    {"instrument_key": "NSE_FO|57353", "exchange_token": "57353", "trading_symbol": "RELIANCE",
     "isin": "INE002A01018", "instrument_type": "FUT", "lot_size": 500.0, "tick_size": 0.05,
     "freeze_quantity": 4500.0},
]


def run(argv, source=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv, source=source)
    return code, out.getvalue(), err.getvalue()


def write_master(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(MASTER_ROWS, fh)
    return path


def write_universe(path, lines=None):
    with open(path, "w", encoding="utf-8") as fh:
        for ln in (lines if lines is not None else
                   ["RELIANCE|INE002A01018|" + KEY, "ADANIENT|INE423A01024|NSE_EQ|INE423A01024"]):
            fh.write(ln + "\n")
    return path


BAVS_LIST = ["--bhavcopy"] + BHAVS


def bhav_arg():
    return ["--bhavcopy", FINAL]


class Universe(unittest.TestCase):
    def test_user_agent_is_url_free(self):
        # NSE's edge stalls any User-Agent containing a URL: live EQUITY_L downloads
        # timed out at 30s with the GitHub URL in the UA and returned in 0.3s without.
        self.assertNotIn("://", scan.UA)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.master = write_master(os.path.join(self.tmp, "master.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_build_resolves_the_master_and_skips_the_rest(self):
        out = os.path.join(self.tmp, "trader", "universe.txt")
        code, out_text, err = run(["universe", "--offline", SDIR, "--master", self.master,
                                   "--out", out, "--no-write", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out_text)
        self.assertEqual(doc["source"], "NSE EQUITY_L + Nifty 500")
        self.assertEqual(doc["union"], 23)  # 20 EQ rows + JSWENERGY, IRCTC, NBCC from the Nifty file
        self.assertEqual(doc["count"], 3)   # RELIANCE, TCS and ADANIENT live in the master fixture
        self.assertEqual(doc["missing_master"], 20)

    def test_universe_cache_line_format(self):
        out = os.path.join(self.tmp, "universe.txt")
        code, out_text, err = run(["universe", "--offline", SDIR, "--master", self.master,
                                   "--out", out, "--json"])
        self.assertEqual(code, 0, err)
        with open(out, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual(lines, ["ADANIENT|INE423A01024|NSE_EQ|INE423A01024",
                                 "RELIANCE|INE002A01018|" + KEY,
                                 "TCS|INE467B01029|NSE_EQ|INE467B01029"])
        # the instrument_key pipe survives: field 3 onward is the key
        parts = lines[1].split("|")
        self.assertEqual("|".join(parts[2:]), KEY)  # RELIANCE line's key survives the split

    def test_universe_without_a_master_fails_closed(self):
        code, out_text, err = run(["universe", "--offline", SDIR, "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("instrument master", err)


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.universe = write_universe(os.path.join(self.tmp, "universe.txt"))
        self.out = os.path.join(self.tmp, "watchlist.json")
        self.master = write_master(os.path.join(self.tmp, "master.json"))
        self.base = ["daily", "--universe", self.universe, "--out", self.out,
                     "--master", self.master, "--now", "2026-10-27 18:30",
                     "--no-write", "--json"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def doc(self, argv):
        code, out_text, err = run(self.base + argv)
        return code, (json.loads(out_text) if out_text.strip() else None), err

    def test_ranks_by_turnover_and_carries_the_instrument(self):
        code, doc, err = self.doc(BAVS_LIST)
        self.assertEqual(code, 0, err)
        self.assertEqual(doc["mode"], "daily")
        self.assertEqual(doc["date"], "2026-10-27")
        self.assertEqual(doc["source"], "nse-bhavcopy-offline")
        self.assertEqual(doc["as_of"], "2026-10-27 18:30 IST")
        self.assertTrue(doc["sessions_supplied"] < doc["sessions_needed"])
        hits = doc["symbols"]
        self.assertTrue(hits)
        turn = [h["turnover_inr"] for h in hits]
        self.assertEqual(turn, sorted(turn, reverse=True))
        by_sym = {h["symbol"]: h for h in hits}
        adanient = by_sym["ADANIENT"]
        self.assertEqual(adanient["instrument_key"], "NSE_EQ|INE423A01024")
        self.assertEqual(adanient["tick_size"], 0.05)
        self.assertEqual(adanient["lot_size"], 1.0)
        self.assertEqual(adanient["freeze_quantity"], 4833.0)
        self.assertEqual(adanient["as_of"], "2026-10-27 18:30 IST")
        self.assertIn("breakout_20d", adanient["setups"])
        for hit in hits:
            for tag in hit["setups"]:
                self.assertIn(tag, [k for k, _ in scan.SETUPS])

    def test_short_window_is_admitted_not_hidden(self):
        code, doc, err = self.doc(BAVS_LIST)
        self.assertEqual(code, 0, err)
        self.assertLess(doc["sessions_supplied"], doc["sessions_needed"])
        self.base[self.base.index("--json")] = "--quiet"
        _, out_text, _ = run(self.base + BAVS_LIST)  # non-json path
        self.assertIn("Only 5 session(s) supplied", out_text)

    def test_prior_days_build_the_window(self):
        code, doc, err = self.doc(["--bhavcopy"] + BHAVS[:4])
        self.assertEqual(code, 0, err)
        self.assertEqual(doc["sessions_supplied"], 4)
        self.assertEqual(doc["date"], "2026-10-26")
        hits = doc["symbols"]
        adanient = next(h for h in hits if h["symbol"] == "ADANIENT")
        self.assertIn("breakout_20d", adanient["setups"])

    def test_written_watchlist_file(self):
        self.base[self.base.index("--no-write")] = "--quiet"
        code, out_text, err = run(self.base + BAVS_LIST)
        self.assertEqual(code, 0, err)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["mode"], "daily")
        self.assertTrue(doc["count"] >= 1)
        self.assertTrue(os.path.exists(self.out))

    def test_one_session_is_a_short_not_a_crash(self):
        code, doc, err = self.doc(bhav_arg())
        self.assertEqual(code, 1, err)  # 1 close: no rule can fire
        self.assertEqual(doc["sessions_supplied"], 1)
        self.assertEqual(doc["scanned"], 0)

    def test_empty_bhavcopy_exits_2(self):
        code, doc, err = self.doc(["--bhavcopy", EMPTY_BHAV])
        self.assertEqual(code, 2)
        self.assertIn("empty", err)
        self.assertIsNone(doc)

    def test_holiday_bhavcopy_exits_2_with_the_name(self):
        # Dussehra, 2026-10-20: a file named for a holiday is rejected before reading
        holiday = os.path.join(self.tmp, "sec_bhavdata_full_20102026.csv")
        with open(holiday, "w", encoding="utf-8") as fh:
            fh.write("SYMBOL,SERIES,DATE1,PREV_CLOSE,CLOSE_PRICE\nRELIANCE,EQ,20-OCT-2026,1,1\n")
        code, doc, err = self.doc(["--bhavcopy", holiday])
        self.assertEqual(code, 2)
        self.assertIn("Dussehra", err)

    def test_missing_bhavcopy_exits_2(self):
        code, doc, err = self.doc(["--bhavcopy", os.path.join(self.tmp, "nope.csv")])
        self.assertEqual(code, 2)
        self.assertIn("nope.csv", err)

    def test_non_eq_series_dropped(self):
        rows = scan.read_bhav(open(FINAL, encoding="utf-8").read())
        self.assertNotIn("RELIANCE-DEMO", rows)  # BE series row in EQUITY_L is not in bhav anyway
        self.assertIn("RELIANCE", rows)
        self.assertEqual(rows["RELIANCE"]["close"] > 0, True)


class Orb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.universe = write_universe(os.path.join(self.tmp, "universe.txt"))
        self.out = os.path.join(self.tmp, "watchlist.json")
        self.master = write_master(os.path.join(self.tmp, "master.json"))
        self.base = ["orb", "--universe", self.universe, "--out", self.out,
                     "--master", self.master, "--offline", ORB_FILE,
                     "--no-write", "--json"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def doc(self, argv):
        code, out_text, err = run(self.base + argv)
        return code, (json.loads(out_text) if out_text.strip() else None), err

    def test_reliance_triggers_at_ten_on_the_26th(self):
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-26 10:30"])
        self.assertEqual(code, 0, err)
        rel = doc["symbols"][0]
        self.assertEqual(rel["symbol"], "RELIANCE")
        self.assertEqual(rel["instrument_key"], KEY)
        self.assertEqual(rel["setup"], "orb")
        orb = rel["orb"]
        self.assertEqual(orb["price"], 1193.6)
        self.assertEqual(orb["at"], "2026-10-26 10:00 IST")
        self.assertEqual(orb["bar_start"], "09:55")
        self.assertEqual(orb["or_high"], 1192.0)
        self.assertEqual(orb["or_low"], 1175.3)
        self.assertEqual(doc["date"], "2026-10-26")
        self.assertEqual(doc["status"], "triggered")

    def test_one_signal_per_day_and_none_before_ten(self):
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-26 09:45"])
        self.assertEqual(code, 1, err)
        self.assertEqual(doc["status"], "waiting")
        rel = doc["symbols"][0]
        self.assertIsNone(rel["orb"])
        # the 09:55 bar completes at 10:00: at 09:59 nothing has triggered yet
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-26 09:59"])
        self.assertEqual(code, 1)
        self.assertIsNone(doc["symbols"][0]["orb"])

    def test_expired_on_the_30th(self):
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-30 15:45"])
        self.assertEqual(code, 1, err)
        self.assertEqual(doc["status"], "expired")
        self.assertIsNone(doc["symbols"][0]["orb"])

    def test_offline_replay_without_a_clock_replays_the_last_day(self):
        code, doc, err = self.doc(["--symbols", "RELIANCE"])
        self.assertEqual(code, 1, err)  # 10-30 is the expired day
        self.assertEqual(doc["date"], "2026-10-30")
        self.assertEqual(doc["status"], "expired")

    def test_candle_cutoff_stops_at_the_clock(self):
        # at 09:57 the 09:55 bar is still in progress: it closes at 10:00
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-26 09:57"])
        self.assertEqual(code, 1)
        self.assertIsNone(doc["symbols"][0]["orb"])

    def test_watchlist_json_contract(self):
        code, doc, err = self.doc(["--symbols", "RELIANCE", "--now", "2026-10-26 10:30"])
        self.assertEqual(code, 0, err)
        for key in ("created_by", "mode", "as_of", "source", "date", "or_minutes",
                    "universe", "scanned", "failed", "note", "count", "symbols"):
            self.assertIn(key, doc)
        self.assertEqual(doc["created_by"], "trader-scanner")
        self.assertEqual(doc["mode"], "orb")
        rel = doc["symbols"][0]
        for key in ("symbol", "instrument_key", "isin", "last", "change_pct",
                    "turnover_inr", "setup", "tick_size", "lot_size",
                    "freeze_quantity", "orb", "as_of"):
            self.assertIn(key, rel)
        self.assertEqual(rel["tick_size"], 0.05)
        self.assertEqual(rel["lot_size"], 1.0)
        self.assertEqual(rel["freeze_quantity"], 11111.0)
        self.assertTrue(rel["as_of"].endswith(" IST"))

    def test_written_watchlist_carries_the_hit(self):
        self.base[self.base.index("--no-write")] = "--quiet"
        code, out_text, err = run(self.base + ["--symbols", "RELIANCE", "--now", "2026-10-26 10:30"])
        self.assertEqual(code, 0, err)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["symbols"][0]["orb"]["price"], 1193.6)

    def test_missing_instrument_key_is_skipped(self):
        uni = write_universe(os.path.join(self.tmp, "nokey.txt"),
                             lines=["RELIANCE|INE002A01018|"])
        code, out_text, err = run(["orb", "--universe", uni, "--out", self.out,
                                   "--offline", ORB_FILE, "--symbols", "RELIANCE",
                                   "--now", "2026-10-26 10:30", "--no-write", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("instrument_key", err)

    def test_holiday_window_exits_2(self):
        code, out_text, err = run(["orb", "--universe", self.universe, "--out", self.out,
                                   "--symbols", "RELIANCE", "--now", "2026-10-20 10:30",
                                   "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("Dussehra", err)


class SourceInjection(unittest.TestCase):
    """main(argv, source=...): the ORB bars come from the injected source, not fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.universe = write_universe(os.path.join(self.tmp, "universe.txt"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_injected_source_drives_the_scan(self):
        doc = json.load(open(ORB_FILE))
        rows = doc[KEY]
        seen_keys = []

        class Src:
            def intraday(self, instrument_key, interval="1m"):
                seen_keys.append(instrument_key)
                return rows

        src = Src()
        out = os.path.join(self.tmp, "watchlist.json")
        code, out_text, err = run(["orb", "--universe", self.universe, "--out", out,
                                   "--symbols", "RELIANCE", "--now", "2026-10-26 10:30",
                                   "--no-write", "--json"], source=src)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_keys, [KEY])
        rel = json.loads(out_text)["symbols"][0]
        self.assertEqual(rel["orb"]["price"], 1193.6)
        self.assertEqual(rel["orb"]["at"], "2026-10-26 10:00 IST")

    def test_source_errors_surface_as_skips(self):
        class Src:
            def intraday(self, instrument_key, interval="1m"):
                raise scan.upstox_mod.UpstoxError("token expired")

        code, out_text, err = run(["orb", "--universe", self.universe, "--out",
                                   os.path.join(self.tmp, "w.json"), "--symbols", "RELIANCE",
                                   "--now", "2026-10-26 10:30", "--no-write", "--json"],
                                  source=Src())
        self.assertEqual(code, 2)
        self.assertIn("RELIANCE: token expired", err)


class BhavParsing(unittest.TestCase):
    def test_columns_parse(self):
        rows = scan.read_bhav(open(FINAL, encoding="utf-8").read(), FINAL)
        rel = rows["RELIANCE"]
        for key in ("prev_close", "open", "high", "low", "last", "close", "avg_price",
                    "volume", "turnover_lacs", "deliv_qty", "deliv_per"):
            self.assertIsNotNone(rel[key], key)
        self.assertEqual(rel["date1"], "27-OCT-2026")

    def test_holiday_naming_comes_from_the_shared_calendar(self):
        cal = core().calendar
        self.assertEqual(cal.load_holidays().get("2026-10-20"), "Dussehra")


class LiveDailyCache(unittest.TestCase):
    """The live daily scan fetches or reuses the last SESSIONS_NEEDED sessions
    from a bhavcopy cache; the fetch seam is injected, so no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "bhavcopy")
        self.universe = write_universe(os.path.join(self.tmp, "universe.txt"))
        self.master = write_master(os.path.join(self.tmp, "master.json"))
        self.out = os.path.join(self.tmp, "watchlist.json")
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"state_dir": os.path.join(self.tmp, "state")}, fh)
        self.requested = []

    def fetch(self, day):
        self.requested.append(day.isoformat())
        close = 100.0 + (day.toordinal() % 20)
        text = ("SYMBOL,SERIES,DATE1,PREV_CLOSE,CLOSE_PRICE,TTL_TRD_QNTY,TURNOVER_LACS\n"
                "RELIANCE,EQ,,100.0,%.2f,500000,5000.0\n"
                "ADANIENT,EQ,,50.0,%.2f,300000,1500.0\n" % (close, close - 1))
        return text, "sec_bhavdata_full_%s.csv" % day.strftime("%d%m%Y")

    def daily(self, fetch):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = scan.main(["daily", "--universe", self.universe, "--master", self.master,
                              "--out", self.out, "--config", self.cfg,
                              "--bhavcopy-dir", self.cache, "--now", "2026-10-27 18:30",
                              "--no-write", "--json"], fetch=fetch)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()

    def test_empty_cache_requests_the_needed_prior_sessions(self):
        code, doc, err = self.daily(self.fetch)
        self.assertIn(code, (0, 1), err)
        self.assertEqual(doc["source"], "nse-bhavcopy")
        self.assertEqual(doc["sessions_supplied"], scan.SESSIONS_NEEDED)
        self.assertEqual(len(self.requested), scan.SESSIONS_NEEDED)
        self.assertEqual(self.requested[-1], "2026-10-27")
        for iso in self.requested:
            self.assertTrue(scan.cal.is_trading_day(datetime.date.fromisoformat(iso)), iso)
        self.assertNotIn("2026-10-20", self.requested)  # Dussehra: no bhavcopy to fetch
        self.assertEqual(len(os.listdir(self.cache)), scan.SESSIONS_NEEDED)

    def test_warm_cache_downloads_nothing(self):
        code, doc, err = self.daily(self.fetch)
        self.assertIn(code, (0, 1), err)

        def boom(day):
            raise AssertionError("warm cache should not download %s" % day)

        code, doc, err = self.daily(boom)
        self.assertIn(code, (0, 1), err)
        self.assertEqual(doc["sessions_supplied"], scan.SESSIONS_NEEDED)
        self.assertEqual(len(os.listdir(self.cache)), scan.SESSIONS_NEEDED)


class CrossCutting(unittest.TestCase):
    def test_no_us_market_strings_in_the_skill(self):
        with open(os.path.join(os.path.dirname(scan.__file__), "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read().lower()
        for bad in ("nasdaq", "yahoo", "nyse", " stooq", " alpaca", "et)", " 9:30", "4:00 pm"):
            self.assertNotIn(bad, text, bad)

    def test_no_us_market_strings_in_fixtures(self):
        for name in os.listdir(SDIR):
            with open(os.path.join(SDIR, name), encoding="utf-8") as fh:
                text = fh.read().lower()
            for bad in ("nasdaq", "yahoo", "nyse", "stooq"):
                self.assertNotIn(bad, text, "%s: %s" % (name, bad))


if __name__ == "__main__":
    unittest.main()
