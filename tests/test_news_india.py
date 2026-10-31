"""India tests for trader-news: IST times, Indian tags, dedupe, seen state, per-feed failure."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

from trader_core.http import UpstoxAuthError

news = load("trader-news", "news")

DIR = os.path.join(FIX, "trader-news", "india")
NOW = "2026-10-26T12:00:00+05:30"  # pinned clock: after every fixture pubDate

EXPIRED = "2026-09-25T12:00:00+05:30"  # more than 30 days before NOW


# The fixture quotes carry the raw v2 colon keys; the seam (upstox.Client.quote)
# returns instrument_key keys, so the tests use the real ISIN keys.
FIXTURE_KEYS = {"RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029"}


class FakeSource(object):
    """The seam main() injects in tests: .quote(keys) -> {instrument_key: bar},
    keyed exactly like upstox.Client.quote (the requested instrument_key)."""

    def __init__(self, quotes=None, error=None):
        self.quotes = quotes or {}
        self.error = error
        self.asked = []

    def quote(self, keys):
        self.asked = list(keys)
        if self.error is not None:
            raise self.error
        return {k: v for k, v in self.quotes.items() if k in keys}


def quotes_from_fixture():
    """tests/fixtures/trader-news/india/quotes.json -> client-shaped quote bars,
    re-keyed from the raw colon form to the real Upstox instrument keys."""
    with open(os.path.join(DIR, "quotes.json"), encoding="utf-8") as fh:
        raw = json.load(fh)["data"]
    out = {}
    for key, entry in raw.items():
        sym = str(key).split(":")[-1].split("|")[-1].upper()
        ohlc = entry.get("ohlc", {})
        out[FIXTURE_KEYS.get(sym, str(key).replace(":", "|"))] = {
            "ltp": entry["last_price"], "open": ohlc.get("open"),
            "high": ohlc.get("high"), "low": ohlc.get("low"),
            "close": entry["last_price"], "prev_close": ohlc.get("close"),
            "volume": ohlc.get("volume"), "ts": news.parse_time(entry["timestamp"])}
    return out


QUOTES = quotes_from_fixture()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="news-india-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_path = os.path.join(self.tmp, "state", "news.json")
        self.out_path = os.path.join(self.tmp, "trader", "news.json")
        self.watchlist_path = os.path.join(self.tmp, "watchlist.json")
        with open(self.watchlist_path, "w", encoding="utf-8") as fh:
            json.dump({"symbols": [{"symbol": sym, "instrument_key": key}
                                   for sym, key in FIXTURE_KEYS.items()]}, fh)

    def run_cli(self, argv, source=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = news.main(argv, source)
        return code, out.getvalue(), err.getvalue()

    def go(self, tickers=("RELIANCE",), feeds=None, source=None, json_mode=False):
        # state and artifact always go to the temp dir, never the repo trader/
        argv = ["--tickers"] + list(tickers) + ["--offline", DIR, "--now", NOW,
                                                "--watchlist", self.watchlist_path,
                                                "--state", self.state_path,
                                                "--out", self.out_path]
        if feeds:
            argv += ["--feed", feeds]
        if json_mode:
            argv.append("--json")
        return self.run_cli(argv, source)


class OfflineRun(Base):
    """The three good feeds, default feed list, Yahoo fetched per symbol."""

    def one_json_run(self, source=None):
        code, out, err = self.go(source=source or FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_tagged_headlines_with_ist_times(self):
        doc = self.one_json_run()
        self.assertEqual(doc["checked"], NOW)
        titles = [it["title"] for it in doc["items"]]
        self.assertEqual(len(doc["items"]), 9)  # 10 raw minus the cross-feed duplicate
        self.assertIn("Reliance Industries announces Q1 results, profit rises 10%", titles)
        tags = set()
        for it in doc["items"]:
            tags.update(it["tags"])
            self.assertTrue(it["time"].endswith("+05:30"), it["time"])
            self.assertTrue(it["tags"], it["title"])
        for label in ("results", "board meeting", "dividend", "bonus/split/buyback",
                      "order win", "regulatory", "SEBI", "ASM/GSM", "circuit change",
                      "delisting", "rating"):
            self.assertIn(label, tags)
        # the notes list every feed error; the good feeds leave none
        self.assertEqual(doc["notes"], [])

    def test_gmt_pubdate_and_ist_pubdate_both_normalize(self):
        doc = self.one_json_run()
        by_title = {it["title"]: it for it in doc["items"]}
        # 05:00 GMT -> 10:30 IST
        gmt_item = by_title["Reliance wins Rs 500 crore order from ONGC"]
        self.assertEqual(gmt_item["time"], "2026-10-26T10:30:00+05:30")
        # 09:30 +0530 stays 09:30 IST
        ist_item = by_title["Reliance board to meet for dividend and bonus issue"]
        self.assertEqual(ist_item["time"], "2026-10-26T10:00:00+05:30")

    def test_parse_time_zones(self):
        gmt = news.parse_time("Mon, 26 Oct 2026 05:00:00 GMT")
        self.assertEqual((gmt.hour, gmt.minute), (10, 30))
        self.assertEqual(gmt.utcoffset(), news.timeutil.IST.utcoffset(None))
        ist = news.parse_time("Mon, 26 Oct 2026 09:30:00 +0530")
        self.assertEqual((ist.hour, ist.minute), (9, 30))
        naive = news.parse_time("2026-10-26 09:30")  # no zone: taken as IST
        self.assertEqual((naive.hour, naive.minute, naive.utcoffset()), (9, 30, ist.utcoffset()))

    def test_text_output_shows_ist_times_tags_and_moving(self):
        code, out, err = self.go(source=FakeSource(quotes=QUOTES))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2026-10-26 10:30 IST", out)          # the GMT item, in IST
        self.assertIn("tags: results, earnings", out)
        self.assertIn("tags: board meeting, dividend", out)
        self.assertIn("MOVING", out)                        # RELIANCE +3.4 percent


class BrokenFeed(Base):
    def test_broken_feed_reported_and_goods_still_flow(self):
        code, out, err = self.go(tickers=("RELIANCE",), feeds="moneycontrol.xml,broken.xml",
                                 source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        titles = [it["title"] for it in doc["items"]]
        self.assertIn("TCS declares interim dividend, board meeting scheduled", titles)
        self.assertTrue(any("broken.xml" in n and "not valid XML" in n for n in doc["notes"]),
                        doc["notes"])

    def test_empty_feed_is_not_an_error(self):
        code, out, err = self.go(tickers=("RELIANCE",), feeds="moneycontrol.xml,empty.xml",
                                 source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(len(doc["items"]), 2)
        self.assertEqual(doc["notes"], [])


class Moves(Base):
    """Move-% from the injected source against quotes.json closes."""

    def run_two_tickers(self, source):
        code, out, err = self.go(tickers=("RELIANCE", "TCS"),
                                 feeds="moneycontrol.xml,et-markets.xml",
                                 source=source, json_mode=True)
        return code, json.loads(out), err

    def test_reliance_crosses_three_percent_tcs_does_not(self):
        src = FakeSource(quotes=QUOTES)
        code, doc, err = self.run_two_tickers(src)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(set(src.asked), {FIXTURE_KEYS["RELIANCE"], FIXTURE_KEYS["TCS"]})
        moves = {m["ticker"]: m for m in doc["moves"]}
        self.assertEqual(moves["RELIANCE"]["pct"], 3.4)      # 103.4 vs 100.0
        self.assertIs(moves["RELIANCE"]["moving"], True)
        self.assertEqual(moves["TCS"]["pct"], 1.1)           # 101.1 vs 100.0
        self.assertIs(moves["TCS"]["moving"], False)

    def test_missing_quote_reported_not_fatal(self):
        key = FIXTURE_KEYS["RELIANCE"]
        src = FakeSource(quotes={key: QUOTES[key]})
        code, doc, err = self.run_two_tickers(src)
        self.assertEqual(code, 0)
        self.assertIn("no quote for TCS", doc["notes"])
        self.assertEqual([m["ticker"] for m in doc["moves"]], ["RELIANCE"])
        self.assertTrue(doc["items"])  # the run still carries headlines

    def test_missing_instrument_key_skips_the_quote(self):
        src = FakeSource(quotes=QUOTES)
        code, out, err = self.go(tickers=("WIPRO",), feeds="moneycontrol.xml",
                                 source=src, json_mode=True)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(src.asked, [])
        self.assertTrue(any("no instrument_key for WIPRO" in n for n in doc["notes"]),
                        doc["notes"])
        self.assertEqual(doc["moves"], [])
        self.assertNotIn("NSE_EQ|WIPRO", out)

    def test_auth_error_exits_2_with_reauth_guidance(self):
        src = FakeSource(quotes=QUOTES, error=UpstoxAuthError("token expired"))
        code, doc, err = self.run_two_tickers(src)
        self.assertEqual(code, 2)
        self.assertIn("re-authenticate", err)
        self.assertTrue(any("re-authenticate" in n for n in doc["notes"]), doc["notes"])
        self.assertEqual(doc["moves"], [])


class State(Base):
    def test_dedupe_on_link_and_title_across_feeds(self):
        code, out, err = self.go(source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        ids = [it["id"] for it in doc["items"]]
        self.assertNotIn("rss:m-dup", ids)
        q1 = [it for it in doc["items"]
              if it["title"] == "Reliance Industries announces Q1 results, profit rises 10%"]
        self.assertEqual(len(q1), 1)  # same link and same title, two feeds, one item

    def test_second_run_suppresses_seen_items(self):
        code1, out1, _ = self.go(source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code1, 0)
        self.assertTrue(json.loads(out1)["items"])
        code2, out2, _ = self.go(source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code2, 1)
        doc2 = json.loads(out2)
        self.assertEqual(doc2["items"], [])

    def test_thirty_day_expiry_re_arms_seen_items(self):
        code1, _, _ = self.go(source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code1, 0)
        with open(self.state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertTrue(state["seen"])
        state["seen"] = {k: EXPIRED for k in state["seen"]}
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        code2, out2, _ = self.go(source=FakeSource(quotes=QUOTES), json_mode=True)
        self.assertEqual(code2, 0)
        self.assertEqual(len(json.loads(out2)["items"]), 9)


class Artifact(Base):
    def test_news_json_artifact_written_with_ist_and_tags(self):
        code, out, err = self.go(source=FakeSource(quotes=QUOTES))
        self.assertEqual((code, err), (0, ""))
        with open(self.out_path, encoding="utf-8") as fh:
            art = json.load(fh)
        self.assertEqual(art["created_by"], "trader-news")
        self.assertEqual(art["checked"], NOW)
        self.assertEqual(len(art["items"]), 9)
        for it in art["items"]:
            self.assertTrue(it["time"].endswith("+05:30"), it["time"])
        self.assertTrue(any(it["tags"] for it in art["items"]))
        moves = {m["ticker"]: m for m in art["moves"]}
        self.assertIs(moves["RELIANCE"]["moving"], True)

    def test_dry_run_writes_nothing(self):
        code, out, err = self.run_cli(
            ["--tickers", "RELIANCE", "--offline", DIR, "--now", NOW,
             "--state", self.state_path, "--out", self.out_path, "--dry-run"],
            FakeSource(quotes=QUOTES))
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.out_path))
        self.assertFalse(os.path.exists(self.state_path))


class Hygiene(Base):
    def test_user_agent_is_url_free(self):
        # NSE's edge stalls any User-Agent containing a URL.
        self.assertNotIn("://", news.BROWSER_UA)

    def test_no_sec_edgar_cik_strings_in_sources_or_output(self):
        for name in ("news.py", "SKILL.md"):
            with open(os.path.join(ROOT, "trader-news", name), encoding="utf-8") as fh:
                text = fh.read()
            for bad in ("SEC", "EDGAR", "CIK", "8-K"):
                self.assertNotIn(bad, text, "%s contains %s" % (name, bad))
        code, out, err = self.go(source=FakeSource(quotes=QUOTES))
        self.assertEqual(code, 0)
        for bad in ("SEC", "EDGAR", "CIK", "8-K"):
            self.assertNotIn(bad, out + err)


if __name__ == "__main__":
    unittest.main()
