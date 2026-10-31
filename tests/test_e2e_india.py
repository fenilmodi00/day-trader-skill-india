"""One offline end-to-end chain on the real skill CLIs, fixtures only, no network.

scanner daily (NSE bhavcopy) -> news (Indian RSS) -> entry (ORB) -> exit plan
-> budget size -> risk check -> broker ticket (sandbox, typed yes) -> exit plan
read-back. Every step is a hand-off on an artifact: the scanner's watchlist
feeds news and entry, the entry trigger feeds the exit plan, the exit plan
feeds the sizing, the sized plan carries the risk PASS, and only that PASS
plus the typed yes let the sandbox order through. A broken path, missing
field, or changed JSON shape fails the test loudly; internal call sequences
are never asserted.
"""

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta

from _load import FIX, core, load

core()  # repo root on sys.path

from trader_core import config  # noqa: E402

scan = load("trader-scanner", "scan")
news = load("trader-news", "news")
entry = load("trader-entry", "entry")
exitm = load("trader-exit", "exit")
size = load("trader-budget", "size")
risk = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

KEY = "NSE_EQ|INE002A01018"
NDIR = os.path.join(FIX, "trader-news", "india")
MASTER = os.path.join(FIX, "upstox", "instruments-NSE.json.gz")
BINST = os.path.join(FIX, "trader-budget", "india", "instruments.json")
NEWS_KEY = "NSE_EQ|INE467B01029"  # TCS in the recorded quotes fixture
NOW_NEWS = "2026-10-26T12:00:00+05:30"


def run_cli(mod, argv, source=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if source is None:
            code = mod.main(argv)
        else:
            code = mod.main(argv, source=source)
    return code, out.getvalue(), err.getvalue()


def write_bhavcopy_days(directory):
    """21 synthetic NSE bhavcopy sessions ending 2026-10-23: twenty down closes
    then a bounce, so RELIANCE carries the oversold/oversold_bounce rules."""
    os.makedirs(directory)
    days = []
    day = date(2026, 10, 23)
    while len(days) < 21:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    header = ("SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,"
              "LAST_PRICE,CLOSE_PRICE,AVG_PRICE,TLTQ,QTY,TURNOVER_LACS\n")
    close, closes = 1500.0, [1500.0]
    for _ in range(21):
        close = close * (0.99 if len(closes) <= 20 else 1.005)
        closes.append(round(close, 2))
    for i, day in enumerate(days):
        path = os.path.join(directory, "sec_bhavdata_full_%s.csv" % day.strftime("%d%m%Y"))
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(header)
            prev, close = closes[i], closes[i + 1]
            fh.write("RELIANCE,EQ,%s,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,100,100000,1000.00\n" % (
                day.strftime("%d-%b-%Y").upper(), prev, prev, close * 1.01, close * 0.99,
                close, close, close))
    return len(days)


def recorded_quotes():
    """tests/fixtures/trader-news/india/quotes.json -> client-shaped quote bars,
    re-keyed to the real instrument keys (the seam's quote contract)."""
    with open(os.path.join(NDIR, "quotes.json"), encoding="utf-8") as fh:
        raw = json.load(fh)["data"]
    out = {}
    for key, item in raw.items():
        symbol = str(key).split(":")[-1].split("|")[-1].upper()
        ohlc = item.get("ohlc", {})
        out[{"RELIANCE": KEY, "TCS": NEWS_KEY}.get(symbol, str(key).replace(":", "|"))] = {
            "ltp": item["last_price"], "open": ohlc.get("open"), "high": ohlc.get("high"),
            "low": ohlc.get("low"), "close": item["last_price"], "prev_close": ohlc.get("close"),
            "volume": ohlc.get("volume"), "ts": news.parse_time(item["timestamp"])}
    return out


class FakeQuoteSource:
    """The DataSource seam for the news moves: quote(keys) only."""

    def __init__(self, quotes):
        self.quotes = quotes
        self.asked = []

    def quote(self, keys):
        self.asked = list(keys)
        return {k: v for k, v in self.quotes.items() if k in keys}


class FakeBroker:
    """The Broker seam for the funds/margin checks and the sandbox place."""

    def __init__(self):
        self.placed = []

    def place(self, payload, retries=0):
        self.placed.append(payload)
        return {"order_id": "241026000000123", "status": "open"}

    def modify(self, order_id, payload):
        return {}

    def cancel(self, order_id):
        return {}

    def orders(self):
        return {"data": []}

    def trades(self):
        return {"data": []}

    def positions(self):
        return {"data": []}

    def holdings(self):
        return {"data": []}

    def funds(self):
        return {"equity": {"available_margin": 50000.0}}

    def margins(self, keys):
        return {"equity": {"instruments": [{"instrument_key": k, "total_margin": 29840.0}
                                           for k in keys]}}


class EndToEndChain(unittest.TestCase):
    """One trade through every skill, artifacts carrying each hand-off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trader = os.path.join(self.tmp, "trader")
        os.makedirs(self.trader)
        self.cfg_path = os.path.join(self.trader, "config.json")
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["state_dir"] = os.path.join(self.tmp, "state")
        # gross loss 248.75 (25 x 9.95) fits 300, and the charges-adjusted net
        # loss 294.28 fits too: the plan must survive both checks to pass.
        cfg["account"]["max_loss_per_trade_inr"] = 300.0
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    # ---- step 1: scanner daily, NSE bhavcopy -> trader/watchlist.json

    def write_watchlist(self):
        bhav_dir = os.path.join(self.tmp, "bhavcopy")
        sessions = write_bhavcopy_days(bhav_dir)
        self.assertEqual(sessions, 21)
        watch = os.path.join(self.trader, "watchlist.json")
        code, out, err = run_cli(scan, [
            "daily", "--symbols", "RELIANCE", "--master", MASTER, "--out", watch,
            "--now", "2026-10-26 09:45", "--json", "--bhavcopy-dir", bhav_dir,
            "--date", "2026-10-26"])
        self.assertEqual(code, 0, err)
        with open(watch, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-scanner")
        self.assertEqual(doc["mode"], "daily")
        self.assertEqual(doc["source"], "nse-bhavcopy")
        self.assertEqual(doc["date"], "2026-10-26")
        self.assertEqual(doc["count"], 1)
        hit = doc["symbols"][0]
        self.assertEqual(hit["symbol"], "RELIANCE")
        self.assertEqual(hit["instrument_key"], KEY)
        self.assertTrue(hit["setups"], "the scan must fire a rule for the chain to continue")
        self.assertIn(hit["setups"][0], ("oversold", "oversold_bounce", "breakout_20d",
                                         "bull_flag", "reclaim_ma20"))
        return watch, hit

    # ---- step 2: news on the watchlist hit -> trader/news.json

    def run_news(self, watch):
        src = FakeQuoteSource(recorded_quotes())
        news_out = os.path.join(self.trader, "news.json")
        code, out, err = run_cli(news, [
            "--offline", NDIR, "--now", NOW_NEWS, "--watchlist", watch,
            "--state", os.path.join(self.tmp, "state", "news.json"),
            "--out", news_out, "--json"], source=src)
        self.assertEqual(code, 0, err)
        self.assertEqual(src.asked, [KEY], "news quotes the watchlist's instrument key")
        with open(news_out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-news")
        self.assertEqual(doc["checked"], NOW_NEWS)
        self.assertEqual(doc["notes"], [], "no feed or quote errors in the chain")
        self.assertTrue(doc["items"], "the chain needs a headline to read")
        for item in doc["items"]:
            self.assertTrue(item["time"].endswith("+05:30"), item["time"])
            self.assertTrue(item["tags"], item["title"])
        return doc

    # ---- step 3: entry ORB off the watchlist -> trader/entry.json

    def run_entry(self, watch):
        orb_dir = os.path.join(self.tmp, "orb")
        os.makedirs(orb_dir)
        with open(os.path.join(FIX, "upstox", "RELIANCE-1m-2026-10-26.json"), encoding="utf-8") as fh:
            rows = json.load(fh)["data"]["candles"]
        with open(os.path.join(orb_dir, "RELIANCE.json"), "w", encoding="utf-8") as fh:
            json.dump({KEY: rows}, fh)
        code, out, err = run_cli(entry, [
            "watchlist", "--watchlist", watch, "--offline", orb_dir,
            "--now", "2026-10-26 10:00", "--json"])
        self.assertEqual(code, 0, err)
        rel = next(r for r in json.loads(out) if r["symbol"] == "RELIANCE")
        self.assertIsNone(rel["error"], rel["error"])
        res = rel["result"]
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["trigger"]["price"], 1193.6)
        self.assertEqual(res["opening_range"]["mid"], 1183.65)
        entry_out = os.path.join(self.trader, "entry.json")
        code, out, err = run_cli(entry, [
            "check", "RELIANCE", "--key", KEY, "--offline", orb_dir,
            "--now", "2026-10-26 10:00", "--master", MASTER, "--write", entry_out, "--json"])
        self.assertEqual(code, 0, err)
        with open(entry_out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-entry")
        self.assertEqual(doc["status"], "triggered")
        self.assertEqual(doc["symbol"], "RELIANCE")
        self.assertEqual(doc["instrument_key"], KEY)
        self.assertEqual(doc["price"], 1193.6)
        self.assertEqual(doc["time"], "2026-10-26 10:00 IST")
        self.assertEqual(doc["opening_range"]["high"], 1192.0)
        self.assertEqual(doc["opening_range"]["low"], 1175.3)
        return doc

    # ---- step 4: exit plan from the entry artifact -> trader/plan.json

    def run_exit_plan(self, entry_doc):
        plan_path = os.path.join(self.trader, "plan.json")
        code, out, err = run_cli(exitm, [
            "plan", "--from", os.path.join(self.trader, "entry.json"),
            "--master", MASTER, "--target", "1R", "--now", "2026-10-26 10:05",
            "--config", self.cfg_path, "--write", plan_path, "--json"])
        self.assertEqual(code, 0, err)
        with open(plan_path, encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual(plan["created_by"], "trader-exit")
        self.assertEqual(plan["symbol"], entry_doc["symbol"])
        self.assertEqual(plan["instrument_key"], entry_doc["instrument_key"])
        self.assertEqual(plan["entry"], 1193.6)
        self.assertEqual(plan["stop"], 1183.65)     # the range mid
        self.assertEqual(plan["target"], 1203.55)   # 1R
        self.assertEqual(plan["risk_per_share"], 9.95)
        self.assertEqual(plan["flat_by"], "15:10")
        self.assertEqual(plan["direction"], "long")
        self.assertEqual(plan["product"], "I")
        self.assertEqual(len(plan["legs"]), 3)
        return plan

    # ---- step 5: budget size -> the sized plan the later skills read

    def run_budget(self, plan):
        code, out, err = run_cli(size, [
            "--plan", os.path.join(self.trader, "plan.json"), "--instruments", BINST,
            "--now", "2026-10-26 10:06", "--config", self.cfg_path,
            "--budget", "50000", "--max-loss", "250", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        sized = doc["plan"]
        self.assertEqual(doc["sizing"]["bound_by"], "max loss")
        self.assertIsNone(sized["refusal"])
        self.assertEqual(sized["symbol"], plan["symbol"])
        self.assertEqual(sized["instrument_key"], plan["instrument_key"])
        self.assertEqual(sized["quantity"], 25)     # 250 / 9.95, whole lots of 1
        self.assertEqual(sized["risk_inr"], 248.75)
        self.assertEqual(sized["flat_by"], plan["flat_by"])
        self.assertEqual([leg["role"] for leg in sized["legs"]],
                         ["entry", "stop", "target"])
        # the sized plan is the artifact trader-risk and trader-broker consume
        sized_path = os.path.join(self.trader, "sized-plan.json")
        with open(sized_path, "w", encoding="utf-8") as fh:
            json.dump(sized, fh)
        return sized_path, sized

    # ---- step 6: risk check on the sized plan -> trader/state/risk.json

    def run_risk(self, sized_path, sized):
        broker = FakeBroker()
        risk_path = os.path.join(self.tmp, "state", "risk.json")
        code, out, err = run_cli(risk, [
            "--plan", sized_path, "--instruments", MASTER, "--now", "2026-10-26 10:07",
            "--config", self.cfg_path, "--out", risk_path, "--json"], source=broker)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("net loss at the stop", doc["checks"]["charges"])
        with open(risk_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["verdict"], "PASS")
        self.assertEqual(saved["plan"], sized, "the PASS binds the exact sized plan")
        self.assertTrue(str(saved["now"]).startswith("2026-10-26"), saved["now"])
        return risk_path, broker

    # ---- step 7: broker ticket, sandbox, typed yes -> trader/ticket.json

    def run_broker(self, sized_path, sized, risk_path, broker):
        qty = sized["quantity"]
        ticket_path = os.path.join(self.trader, "ticket.json")
        common = ["--plan", sized_path, "--ticket", ticket_path, "--config", self.cfg_path,
                  "--risk", risk_path, "--now", "2026-10-26 10:08"]
        code, out, err = run_cli(ticket, ["show", "--json"] + common)
        self.assertEqual(code, 0, err)
        self.assertIn("READY", out, "the risk PASS must make the ticket ready")
        # the typed yes names this exact order; a wrong number is refused
        code, out, err = run_cli(ticket, ["approve", "--yes", "yes buy 99 RELIANCE"] + common)
        self.assertEqual(code, 2)
        self.assertIn("not recorded", err)
        self.assertFalse(os.path.exists(ticket_path), "a refused yes writes no ticket")
        code, out, err = run_cli(ticket, ["approve", "--yes",
                                          "yes buy %d RELIANCE" % qty] + common)
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(ticket, ["execute"] + common, source=broker)
        self.assertEqual(code, 0, err)
        with open(ticket_path, encoding="utf-8") as fh:
            tkt = json.load(fh)
        self.assertEqual(tkt["status"], "placed")
        self.assertEqual(tkt["approval"]["reply"], "yes buy %d RELIANCE" % qty)
        self.assertEqual(tkt["result"]["host"], "https://api-sandbox.upstox.com")
        self.assertEqual(tkt["result"]["order_id"], "241026000000123")
        self.assertEqual(broker.placed[0]["quantity"], qty)
        self.assertEqual(broker.placed[0]["transaction_type"], "BUY")
        self.assertEqual(broker.placed[0]["instrument_token"], KEY)
        with open(os.path.join(self.tmp, "state", "orders.json"), encoding="utf-8") as fh:
            ledger = json.load(fh)
        self.assertEqual([rec["status"] for rec in ledger["orders"]],
                         ["approved", "placed"])
        return tkt

    # ---- step 8: the exit plan the chain traded on, read back

    def run_exit_show(self):
        code, out, err = run_cli(exitm, ["show", "--plan",
                                         os.path.join(self.trader, "plan.json"), "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["symbol"], "RELIANCE")
        self.assertEqual(doc["entry"], 1193.6)
        self.assertEqual(doc["stop"], 1183.65)
        self.assertEqual(doc["target"], 1203.55)
        self.assertEqual(doc["flat_by"], "15:10")
        self.assertEqual(len(doc["legs"]), 3)

    def test_one_trade_through_every_skill(self):
        watch, _ = self.write_watchlist()
        self.run_news(watch)
        entry_doc = self.run_entry(watch)
        plan = self.run_exit_plan(entry_doc)
        sized_path, sized = self.run_budget(plan)
        risk_path, broker = self.run_risk(sized_path, sized)
        self.run_broker(sized_path, sized, risk_path, broker)
        self.run_exit_show()


if __name__ == "__main__":
    unittest.main()
