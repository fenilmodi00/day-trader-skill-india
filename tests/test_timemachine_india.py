"""India tests for trader-timemachine: Upstox 1m resampled to 5m, 15 to 20
session window, deterministic seed, no 5m historical call."""

import csv
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine", "india")
FIFTEEN = os.path.join(DIR, "REL-15sessions-1m.json")
THIRTY_M = os.path.join(DIR, "REL-30m-substitute.json")
RELIANCE_5D = os.path.join(FIX, "upstox", "RELIANCE-1m-5d.json")
KEY = "NSE_EQ|INE002A01018"
SOURCE_DAY = "2026-10-26"      # the first session in RELIANCE-1m-5d.json


def read_payload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def payload_rows(doc):
    return doc["data"]["candles"]


def shifted_rows(source_rows, from_date, to_date):
    """The source session's rows re-stamped onto to_date with every price one
    paisa lower: a fixed deterministic drift, no randomness, so the copies
    never match the source session's prices. Timestamps keep their intraday
    offsets, so every copied session is complete."""
    day = datetime.date.fromisoformat(from_date)
    new_day = datetime.date.fromisoformat(to_date)
    shift = datetime.timedelta(days=(new_day - day).days)
    out = []
    for row in source_rows:
        ts = datetime.datetime.fromisoformat(row[0])
        if ts.date() != day:
            continue
        ts = ts + shift
        out.append([ts.isoformat(), row[1] - 0.01, row[2] - 0.01, row[3] - 0.01, row[4] - 0.01,
                    row[5], row[6]])
    return out


def build_sessions(source_rows, dates):
    """The source session re-stamped onto every date in order."""
    out = []
    for d in dates:
        out.extend(shifted_rows(source_rows, SOURCE_DAY, d))
    return out


def trading_days_before(count, after="2026-10-26"):
    """The last `count` weekday dates strictly before `after`, oldest first."""
    out = []
    d = datetime.date.fromisoformat(after)
    while len(out) < count:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return list(reversed(out))


def bars5_of(rows):
    return tm.resample(tm.rows_to_bars(rows), 5)


def day_bar(rows, plan_date, slot):
    return [b for b in bars5_of(rows) if b["dt"].date().isoformat() == plan_date][slot]


def plan_flags(rows, plan_date, key=KEY):
    """CLI plan flags priced off the plan day's own 10:00 AM bar (bar 8): the
    entry is that bar's close, the stop 15 below, the target 15 above. The
    band is wide enough that neither side of a RELIANCE fixture plan day is
    touched after the entry (the 2026-10-26 session runs 1190.09 to 1204.49
    after bar 8), so the run is a forecast, not the hindsight refusal."""
    entry = round(day_bar(rows, plan_date, 8)["c"], 2)
    flags = ["--symbol", "RELIANCE", "--date", plan_date,
             "--entry", str(entry), "--stop", str(entry - 15), "--target", str(entry + 15),
             "--entry-time", "10:00 AM"]
    if key:
        flags += ["--key", key]
    return flags


def run_cli(argv, source=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv, source)
    return code, out.getvalue(), err.getvalue()


def machine_plan(bars5, plan_date="2026-10-26", **over):
    """A normalized plan priced off the plan day's own 10:00 AM bar (bar 8):
    the entry is that bar's close, the stop 5 below, the target 5 above."""
    day = [b for b in bars5 if b["dt"].date().isoformat() == plan_date]
    entry = round(day[8]["c"], 2)
    raw = {"symbol": "RELIANCE", "side": "buy", "date": plan_date, "setup": "orb",
           "entry": entry, "stop": entry - 5, "target": entry + 5, "entry_time": "10:00 AM",
           "opening_range": {"minutes": 15}, "stop_rule": "auto", "interval": "5m"}
    raw.update(over)
    return tm.normalize_plan(raw)


class FakeSource(object):
    """The seam main() injects: .candles(key, interval, start, end), like
    upstox.Client. It records every call so the tests can assert on the
    arguments, and it answers with the same bar dicts Client.candles returns
    (the raw 1m rows run through the module's own row parser)."""

    def __init__(self, rows):
        self.bars = tm.rows_to_bars(rows)
        self.calls = []

    def candles(self, instrument_key, interval, start, end):
        self.calls.append({"instrument_key": instrument_key, "interval": interval,
                           "start": start, "end": end})
        return list(self.bars)


def write_payload(tmp, rows, name="payload.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"status": "success", "data": {"candles": rows}}, fh)
    return path


class Resample(unittest.TestCase):
    def test_a_session_resamples_to_75_bars_0915_to_1525(self):
        bars1 = tm.rows_to_bars(payload_rows(read_payload(RELIANCE_5D)))
        bars5 = tm.resample(bars1, 5)
        one_day = [b for b in bars5 if b["dt"].date().isoformat() == SOURCE_DAY]
        self.assertEqual(len(one_day), 75)
        stamps = [b["dt"].strftime("%H:%M") for b in one_day]
        self.assertEqual(stamps[0], "09:15")
        self.assertEqual(stamps[-1], "15:25")
        self.assertEqual(len(set(stamps)), 75)          # all distinct, on the 5m grid
        for b in one_day:
            self.assertEqual((b["dt"].minute - 15) % 5, 0)
            self.assertIsNotNone(b["dt"].tzinfo)


class Window(unittest.TestCase):
    def payload_for(self, sessions_before, plan_date="2026-10-26"):
        """Payload rows: `sessions_before` complete sessions before plan_date
        plus the plan day itself, all copied from the 5d fixture."""
        src = payload_rows(read_payload(RELIANCE_5D))
        dates = trading_days_before(sessions_before, plan_date)
        rows = build_sessions(src, dates)
        rows.extend(shifted_rows(src, SOURCE_DAY, plan_date))
        return rows, dates

    def run_offline(self, rows, plan_date, extra=None):
        tmp = tempfile.mkdtemp(prefix="tm-india-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = write_payload(tmp, rows)
        argv = ["run", "--offline", path, "--now", plan_date + " 18:00", "--json"] + \
            plan_flags(rows, plan_date) + (extra or [])
        return run_cli(argv)

    def test_14_complete_sessions_exits_2_naming_the_count(self):
        rows, _ = self.payload_for(14)
        code, out, err = self.run_offline(rows, "2026-10-26")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("14", err)
        self.assertIn("at least 15", err)
        self.assertIn("0 incomplete dropped", err)

    def test_15_complete_sessions_runs_with_caveat_and_window(self):
        rows, dates = self.payload_for(15)
        code, out, err = self.run_offline(rows, "2026-10-26")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["sessions_used"], 15)
        self.assertEqual(doc["complete_sessions"], 15)
        self.assertEqual(doc["dropped_sessions"], 0)
        self.assertEqual(doc["window_start"], dates[0])
        self.assertEqual(doc["window_end"], dates[-1])
        self.assertIn("Caveat", doc["caveat"])
        self.assertIn("15 complete sessions", doc["caveat"])
        self.assertIn("simulation of the recent window", doc["note"])
        self.assertIn("not a prediction", doc["note"])

    def test_20_complete_sessions_is_the_full_window_without_caveat(self):
        rows, dates = self.payload_for(20)
        code, out, err = self.run_offline(rows, "2026-10-26")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["sessions_used"], 20)
        self.assertIsNone(doc["caveat"])
        self.assertEqual(doc["window_start"], dates[0])
        self.assertEqual(doc["window_end"], dates[-1])

    def test_more_than_20_sessions_uses_only_the_latest_20(self):
        rows, _ = self.payload_for(22)
        code, out, err = self.run_offline(rows, "2026-10-26")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["sessions_used"], 20)
        self.assertIsNone(doc["caveat"])

    def test_incomplete_sessions_are_dropped_and_counted(self):
        # 14 complete sessions + 3 broken ones + the plan day: 14 usable, refuse
        src = payload_rows(read_payload(RELIANCE_5D))
        rows = build_sessions(src, trading_days_before(14))
        rows.extend(shifted_rows(src, SOURCE_DAY, "2026-09-07")[:40])       # 40 bars, then nothing
        hole = shifted_rows(src, SOURCE_DAY, "2026-09-08")
        rows.extend(hole[:100] + hole[130:])                                # a 30-bar gap
        preopen = shifted_rows(src, SOURCE_DAY, "2026-09-09")
        rows.extend(preopen[:200])                                          # ends before the close
        rows.extend(shifted_rows(src, SOURCE_DAY, "2026-10-26"))
        code, out, err = self.run_offline(rows, "2026-10-26")
        self.assertEqual(code, 2)
        self.assertIn("14", err)
        self.assertIn("3 incomplete dropped", err)

    def test_preopen_rows_never_fabricate_a_session(self):
        bars1 = tm.rows_to_bars([["2026-10-26T09:04:00+05:30", 1186.0, 1186.0, 1186.0, 1186.0, 0, 0],
                                 ["2026-10-26T09:10:00+05:30", 1186.0, 1186.0, 1186.0, 1186.0, 0, 0]])
        self.assertEqual(bars1, [])


class Determinism(unittest.TestCase):
    """The 15-session fixture: its plan day is the last session (2026-10-26),
    the pool is the 15 complete sessions before it."""

    def argv(self, seed=None):
        argv = ["run", "--offline", FIFTEEN, "--now", "2026-10-26 18:00", "--json"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        if seed:
            argv += ["--seed", str(seed)]
        return argv

    def test_fixed_seed_yields_byte_identical_json_across_two_runs(self):
        one = run_cli(self.argv())
        two = run_cli(self.argv())
        self.assertEqual((one[0], one[2]), (0, ""))
        self.assertEqual(one[1], two[1])
        doc = json.loads(one[1])
        self.assertEqual(doc["sessions_used"], 15)
        self.assertEqual(doc["plan"]["date"], "2026-10-26")
        self.assertEqual(doc["mc"]["seed"], 19)

    def test_a_different_seed_changes_the_numbers(self):
        # synthetic fixture bars are too uniform for the draws to matter; the
        # date-shifted real RELIANCE sessions give the odds seed sensitivity
        rows, _ = Window.payload_for(Window(), 15)
        tmp = tempfile.mkdtemp(prefix="tm-india-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = write_payload(tmp, rows)

        def run(seed):
            argv = ["run", "--offline", path, "--now", "2026-10-26 18:00", "--json",
                    "--seed", str(seed)] + plan_flags(rows, "2026-10-26")
            code, out, err = run_cli(argv)
            self.assertEqual((code, err), (0, ""))
            return json.loads(out)

        one, two = run(19), run(7)
        self.assertEqual(one["mc"]["seed"], 19)
        self.assertEqual(two["mc"]["seed"], 7)
        self.assertNotEqual(one["mc"]["p_target"], two["mc"]["p_target"])


class DataSide(unittest.TestCase):
    def test_no_5m_interval_call_reaches_the_source(self):
        src = FakeSource(payload_rows(read_payload(FIFTEEN)))
        argv = ["run", "--symbol", "RELIANCE", "--date", "2026-10-26"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26") + \
            ["--now", "2026-10-26 18:00", "--json"]
        code, out, err = run_cli(argv, src)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(src.calls), 1)
        call = src.calls[0]
        self.assertEqual(call["instrument_key"], KEY)
        self.assertEqual(call["interval"], "1m")
        self.assertEqual(call["end"], "2026-10-26")
        self.assertEqual(call["start"], "2026-09-25")     # one month back
        doc = json.loads(out)
        self.assertEqual(doc["source"], "upstox")
        self.assertEqual(doc["data"]["resampled_from"], "1m")

    def test_injected_instrument_key_reaches_the_source(self):
        src = FakeSource(payload_rows(read_payload(FIFTEEN)))
        argv = ["run", "--symbol", "RELIANCE", "--date", "2026-10-26"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26") + \
            ["--now", "2026-10-26 18:00", "--json"]
        code, out, err = run_cli(argv, src)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(src.calls[0]["instrument_key"], KEY)
        self.assertEqual(doc["data"]["instrument_key"], KEY)
        for call in src.calls:
            self.assertNotEqual(call["interval"], "5m")

    def test_missing_instrument_key_errors_and_sends_nothing(self):
        src = FakeSource(payload_rows(read_payload(FIFTEEN)))
        argv = ["run", "--symbol", "RELIANCE", "--date", "2026-10-26"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26", key=None) + \
            ["--now", "2026-10-26 18:00", "--json"]
        code, out, err = run_cli(argv, src)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no instrument_key for RELIANCE: run trader-scanner universe --write "
                      "or pass --key", err)
        self.assertEqual(src.calls, [])

    def test_30m_payload_is_rejected_as_a_substitute(self):
        argv = ["run", "--offline", THIRTY_M, "--now", "2026-10-26 18:00"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        code, out, err = run_cli(argv)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("30-minute", err)
        self.assertIn("1-minute", err)

    def test_plan_with_a_non_5m_interval_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            tm.normalize_plan({"symbol": "RELIANCE", "date": "2026-10-26", "entry": 1193.6,
                               "stop": 1183.65, "target": 1203.55, "entry_time": "10:00 AM",
                               "interval": "1m"})
        self.assertIn("5-minute bars only", str(cm.exception))


class Verdict(unittest.TestCase):
    def machine(self):
        doc = read_payload(FIFTEEN)
        bars5 = bars5_of(payload_rows(doc))
        last = [b for b in bars5 if b["dt"].date().isoformat() == "2026-10-26"]
        p = tm.normalize_plan({"symbol": "RELIANCE", "side": "buy", "date": "2026-10-26",
                               "setup": "orb", "entry": round(last[8]["c"], 2),
                               "stop": round(last[8]["c"], 2) - 5,
                               "target": round(last[8]["c"], 2) + 5,
                               "entry_time": "10:00 AM",
                               "opening_range": {"minutes": 15}, "stop_rule": "auto",
                               "interval": "5m"})
        return tm.time_machine(bars5, p, tm.timeutil.parse_ist("2026-10-26 18:00"))

    def test_verdict_separates_simulation_from_prediction(self):
        doc = self.machine()
        self.assertIn("simulation of the recent window", doc["note"])
        self.assertIn("not a prediction", doc["note"])
        text = "\n".join(tm.report(doc))
        self.assertIn("not a prediction", text)
        self.assertIn("Window:", text)
        self.assertIn("simulation", text.lower())

    def test_verdict_words_name_the_session_count_and_window(self):
        doc = self.machine()
        self.assertEqual(doc["sessions_used"], 15)
        self.assertIn("15", doc["note"])
        self.assertIn(doc["window_start"], doc["note"])
        self.assertIn(doc["window_end"], doc["note"])
        self.assertIn("Caveat:", "\n".join(tm.report(doc)))


class StatsHonesty(unittest.TestCase):
    """PR #2.4: Wilson intervals, the both-touched count, the fill model,
    the flat_by default --until, and the always-printed caveats. The 15-session
    fixture's plan day is real data, so every run here warns that the later
    real bars were not used."""

    @classmethod
    def setUpClass(cls):
        cls.bars5 = bars5_of(payload_rows(read_payload(FIFTEEN)))
        cls.now = tm.timeutil.parse_ist("2026-10-26 18:00")

    def machine(self, replays=300, until=None, fill="close", **over):
        p = machine_plan(self.bars5, **over)
        return tm.time_machine(self.bars5, p, self.now, replays=replays, until=until, fill=fill)

    def test_wilson(self):
        self.assertIsNone(tm.wilson(0, 0))
        lo, hi = tm.wilson(13, 28)
        self.assertEqual((round(lo, 4), round(hi, 4)), (0.2953, 0.6419))
        lo, hi = tm.wilson(2938, 10000)
        self.assertEqual((round(lo, 4), round(hi, 4)), (0.285, 0.3028))
        self.assertEqual(tm.wilson(0, 10)[0], 0.0)
        self.assertEqual(tm.wilson(10, 10)[1], 1.0)
        a, b = tm.wilson(3, 10), tm.wilson(7, 10)
        self.assertAlmostEqual(a[0], 1 - b[1])        # symmetric about one half
        self.assertAlmostEqual(a[1], 1 - b[0])
        self.assertEqual(tm.ci_text((0.2849527, 0.3028056)), "(95 % interval 28.5 to 30.3)")
        self.assertEqual(tm.ci_text((0.2953, 0.6419), 0), "(95 % interval 30 to 64)")
        self.assertEqual(tm.ci_text(None), "")

    def test_mc_carries_intervals_and_both_touched(self):
        m = self.machine(replays=500)["mc"]
        self.assertEqual(m["fill"], "close")
        lo, hi = m["ci_target"]
        self.assertTrue(0.0 <= lo <= m["p_target"] <= hi <= 1.0)
        self.assertTrue(0.0 <= m["ci_stop"][0] <= m["p_stop"] <= m["ci_stop"][1] <= 1.0)
        self.assertEqual(m["both_touched"], 0)        # the +-5 band is wider than any pool bar
        self.assertEqual(m["p_both"], 0.0)
        doc = self.machine(replays=500)
        self.assertIn("both touched in one bar: 0 (0.0 %), counted as the stop", "\n".join(tm.report(doc)))

    def test_a_bar_that_touches_both_counts_as_the_stop(self):
        # a 0.30 band: plenty of drawn bars span it, so the conservative tie rule decides most replays
        pool = tm.build_pool(tm.full_sessions(tm.split_sessions(self.bars5), "2026-10-26")[0])
        entry = round([b for b in self.bars5 if b["dt"].date().isoformat() == "2026-10-26"][8]["c"], 2)
        m = tm.simulate(pool, entry, entry - 0.15, entry + 0.15, 8, 74, 2000, 19, 2, "buy")
        self.assertEqual((m["both_touched"], m["p_both"]), (2000, 1.0))
        self.assertEqual(m["p_target"], 0.0)          # every both-bar counted as the stop
        self.assertEqual(m["p_stop"], 1.0)
        sell = tm.simulate(pool, entry, entry + 0.15, entry - 0.15, 8, 74, 2000, 19, 2, "sell")
        self.assertEqual(sell["both_touched"], 2000)
        self.assertEqual(sell["p_stop"], 1.0)

    def test_history_decided_win_rate_and_ci(self):
        h = self.machine(replays=100)["history"]
        self.assertEqual((h["decided"], h["win_rate"], h["win_ci"], h["fill"]), (0, None, None, "close"))
        for t in h["trades"]:
            self.assertEqual(t["fill"], t["entry"])   # close fill: the fill is the signal bar's close
            self.assertIn("close", t)
            self.assertIn("exit_price", t)

    def test_history_exit_price_differs_from_close_on_some_session(self):
        # a tight pct stop and a far target: the stop fires first in most sessions, and
        # those rows carry exit_price = stop while close is the session's final candle
        bars5 = self.bars5
        sessions, _ = tm.full_sessions(tm.split_sessions(bars5), "2026-10-26")
        rule = {"setup": "orb", "or_minutes": 15, "stop_rule": "pct", "stop_pct": 0.02, "target_r": 30.0}
        h = tm.history_replay(sessions, rule, 74, "buy")
        self.assertTrue(h["trades"])
        stops = [t for t in h["trades"] if t["outcome"] == "stop"]
        self.assertTrue(stops, "expected the tight stop to fire in some session")
        for t in h["trades"]:
            want = t["target"] if t["outcome"] == "target" else t["stop"] if t["outcome"] == "stop" else t["close"]
            self.assertEqual(t["exit_price"], want)
            risk = t["entry"] - t["stop"]
            self.assertAlmostEqual(t["R"], round((t["exit_price"] - t["fill"]) / risk, 2), places=2)

    def test_next_open_fill_model(self):
        doc = self.machine(replays=300, fill="next-open")
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["fill"], h["fill"]), ("next-open", "next-open"))
        self.assertIn("median_fill", m)
        text = "\n".join(tm.report(doc))
        self.assertIn("Fills at the next bar's open. No spread, slippage or commission is included.", text)
        self.assertIn("Median fill", text)
        self.assertIn("Average R: %+.3f (R = (exit - fill) / the %.2f risk)." % (m["avg_R"], doc["plan"]["risk_per_share"]),
                      text)
        self.assertNotIn("signal bar's close", text)
        self.assertEqual(doc["caveats"][1], "Fills at the next bar's open; a real fill can be worse. No costs included.")
        for t in h["trades"]:
            self.assertGreaterEqual(t["fill"], 0)
        with self.assertRaises(tm.TMError):
            self.machine(fill="vwap")

    def test_default_until_is_the_plans_flat_by(self):
        doc = self.machine(flat_by="15:10")
        m = doc["mc"]
        self.assertEqual((m["until"], m["until_from"], m["fill"]), ("15:10", "flat_by", "close"))
        self.assertEqual(m["band_times"][-1], "15:10")
        text = "\n".join(tm.report(doc))
        self.assertIn("10:00 AM to 3:10 PM, the plan's flat_by (", text)
        # an explicit --until flag wins and is labelled
        p = machine_plan(self.bars5, flat_by="15:10")
        doc2 = tm.time_machine(self.bars5, p, self.now, replays=50, until="15:30")
        self.assertEqual((doc2["mc"]["until"], doc2["mc"]["until_from"]), ("15:30", "flag"))

    def test_without_a_flat_by_the_default_is_the_close(self):
        m = self.machine()["mc"]
        self.assertEqual((m["until"], m["until_from"]), ("15:30", "close"))
        self.assertIn("10:00 AM to 3:30 PM, the close (", "\n".join(tm.report(self.machine())))

    def test_a_flat_by_off_the_grid_moves_to_the_bar_before_it(self):
        doc = self.machine(flat_by="15:47")
        self.assertEqual(doc["mc"]["until"], "15:30")
        self.assertIn("the plan's flat_by 15:47 is after the 3:30 PM close; the replays end at "
                      "3:30 PM instead (pass --until to choose)", doc["warnings"])
        doc = self.machine(flat_by="15:12")
        self.assertEqual(doc["mc"]["until"], "15:10")
        self.assertIn("the plan's flat_by 15:12 is off the 5-minute grid; the replays end at "
                      "3:10 PM instead (pass --until to choose)", doc["warnings"])
        with self.assertRaises(tm.TMError) as cm:
            self.machine(flat_by="10:00")
        self.assertIn("flat_by 10:00 AM leaves nothing to replay", str(cm.exception))
        with self.assertRaises(tm.TMError):
            self.machine(flat_by="soon")

    def test_caveats_always_printed(self):
        doc = self.machine(replays=50)
        self.assertEqual(doc["caveats"], [
            "Built from the same 15 sessions as the history: the two are not independent evidence.",
            "Fills at the signal bar's close; a real fill is the next bar's open or worse. No costs included."])
        text = "\n".join(tm.report(doc))
        self.assertIn("Built from the same 15 sessions as the history", text)
        self.assertIn("Fills at the signal bar's close; a real fill is the next bar's open or worse.", text)

    def test_the_later_real_bars_warning(self):
        doc = self.machine(replays=50)
        self.assertEqual(doc["warnings"],
                         ["66 bar(s) after the entry are already in the data and were not used: the odds are "
                          "from the entry bar only"])


class CsvExport(unittest.TestCase):
    """PR #8: --csv writes one monte_carlo row per replay and one history row per
    trade; close and exit_price are separate; --csv and --write cannot collide."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tm-india-csv-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def run_csv(self, extra):
        argv = ["run", "--offline", FIFTEEN, "--now", "2026-10-26 18:00", "--replays", "25",
                "--symbol", "RELIANCE", "--date", "2026-10-26",
                "--entry", "1268.30", "--stop", "1263.30", "--target", "1273.30",
                "--entry-time", "10:00 AM", "--key", KEY] + extra
        return run_cli(argv)

    def test_csv_export_contains_replays_and_historical_trades(self):
        path = os.path.join(self.d, "reports", "timemachine.csv")
        code, text, err = self.run_csv(["--csv", path])
        self.assertEqual(code, 0, err)
        self.assertIn("Wrote %s." % path, text)
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        replay_rows = [row for row in rows if row["record_type"] == "monte_carlo"]
        history_rows = [row for row in rows if row["record_type"] == "history"]
        self.assertEqual(len(replay_rows), 25)
        self.assertEqual(len(history_rows), 15)
        first = replay_rows[0]
        self.assertEqual(first["replay"], "1")
        self.assertEqual(first["symbol"], "RELIANCE")
        self.assertEqual(first["side"], "buy")
        self.assertEqual(first["plan_date"], "2026-10-26")
        self.assertEqual(first["entry"], "1268.3")
        self.assertIn(first["outcome"], ("target", "stop", "close"))
        self.assertNotEqual(first["close"], "")
        self.assertNotEqual(first["R"], "")
        # history rows carry the fill; under --fill close it equals the entry
        self.assertEqual(history_rows[0]["fill"], history_rows[0]["entry"])
        # every replay's R agrees with its outcome: +1R target, -1 stop, (close-entry)/risk otherwise
        risk = 5.0
        for row in replay_rows:
            if row["outcome"] == "target":
                self.assertAlmostEqual(float(row["R"]), 1.0, places=4)
            elif row["outcome"] == "stop":
                self.assertAlmostEqual(float(row["R"]), -1.0, places=4)
            else:
                self.assertAlmostEqual(float(row["R"]), round((float(row["close"]) - 1268.30) / risk, 4), places=2)
        self.assertNotEqual(history_rows[0]["date"], "")
        self.assertNotEqual(history_rows[0]["time"], "")
        for row in history_rows:
            self.assertIn(row["outcome"], ("target", "stop", "close"))
            self.assertEqual(row["fill"], row["entry"])

    def test_history_exit_price_is_the_stop_or_target_not_just_the_close(self):
        # a tight pct stop and a far target (0.02 % stop, 30R target): the stop fires
        # first in most sessions, so history rows carry exit_price = stop while close
        # is the final candle, and the two differ
        path = os.path.join(self.d, "history.csv")
        code, text, err = self.run_csv(["--csv", path, "--stop-rule", "pct",
                                        "--stop", str(1268.30 * 0.9998), "--target", str(1268.30 * 1.60)])
        self.assertEqual(code, 0, err)
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [row for row in csv.DictReader(fh) if row["record_type"] == "history"]
        self.assertTrue(rows)
        for row in rows:
            want = row["target"] if row["outcome"] == "target" else \
                row["stop"] if row["outcome"] == "stop" else row["close"]
            self.assertEqual(float(row["exit_price"]), float(want))
        self.assertTrue(any(float(r["exit_price"]) != float(r["close"]) for r in rows),
                        "no history row left the position before the close")

    def test_replay_results_are_not_leaked_into_json_or_write(self):
        out = os.path.join(self.d, "tm.json")
        code, text, err = self.run_csv(["--json", "--write", out])
        self.assertEqual(code, 0, err)
        self.assertNotIn("replay_results", json.loads(text)["mc"])
        with open(out, encoding="utf-8") as fh:
            self.assertNotIn("replay_results", json.load(fh)["mc"])
        # with --csv too: the popped results reach the CSV, not the JSON
        csvp = os.path.join(self.d, "out.csv")
        code, text, err = self.run_csv(["--json", "--write", out, "--csv", csvp])
        self.assertEqual(code, 0, err)
        self.assertNotIn("replay_results", json.loads(text)["mc"])
        with open(csvp, newline="", encoding="utf-8") as fh:
            self.assertEqual(len([r for r in csv.DictReader(fh) if r["record_type"] == "monte_carlo"]), 25)

    def test_csv_and_write_cannot_overwrite_each_other(self):
        same = os.path.join(self.d, "same.json")
        code, _, err = self.run_csv(["--csv", same, "--write", same])
        self.assertEqual(code, 2)
        self.assertIn("--csv and --write must use different paths", err)
        # same file through a different spelling of the path
        other = os.path.normcase(os.path.abspath(same))
        code, _, err = self.run_csv(["--csv", other.upper(), "--write", other])
        self.assertEqual(code, 2)
        self.assertIn("--csv and --write must use different paths", err)

    def test_csv_default_path_and_writable_directory(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = self.run_csv(["--csv"])
            self.assertEqual(code, 0, err)
            self.assertIn("Wrote trader/timemachine.csv.", text)
            self.assertTrue(os.path.exists(os.path.join(self.d, "trader", "timemachine.csv")))
        finally:
            os.chdir(cwd)

    def test_an_unwritable_csv_path_is_one_plain_line(self):
        # "blocked" is a file, so creating blocked/sub fails with a plain one-line error
        blocked = os.path.join(self.d, "blocked")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        code, _, err = self.run_csv(["--csv", os.path.join(blocked, "sub", "x.csv")])
        self.assertEqual(code, 2)
        self.assertIn("cannot write", err)
        self.assertNotIn("Traceback", err)


class HindsightRefusal(unittest.TestCase):
    """PR #25: the real bars after the entry settle the trade -> refuse; they do
    not -> run, and say the later bars were not used."""

    @classmethod
    def setUpClass(cls):
        cls.bars5 = bars5_of(payload_rows(read_payload(FIFTEEN)))
        cls.now = tm.timeutil.parse_ist("2026-10-26 18:00")

    def machine(self, **over):
        p = machine_plan(self.bars5, **over)
        return tm.time_machine(self.bars5, p, self.now, replays=50)

    def test_refuses_odds_once_the_real_bars_already_resolved_it(self):
        # the fixture's 11:05 bar (high 1269.20) already hits this target before the sim starts
        with self.assertRaises(tm.TMError) as cm:
            self.machine(stop=1266.90, target=1269.20, stop_rule="pct")
        msg = str(cm.exception)
        self.assertIn("target was hit at 11:05 (bar 21) on 2026-10-26", msg)
        self.assertIn("hindsight, not a forecast", msg)
        # a plan whose stop sits above the day's low after the entry resolves as a stop
        with self.assertRaises(tm.TMError) as cm:
            self.machine(stop=1268.15, target=1273.30, stop_rule="pct")
        self.assertIn("stop was hit at", str(cm.exception))

    def test_the_real_outcome_tie_rule_is_stop_first(self):
        # a bar that spans both the stop and the target counts as the stop, as in simulate()
        day = {s: b for s, b in enumerate([b for b in self.bars5
                                           if b["dt"].date().isoformat() == "2026-10-26"])}
        outcome, at, later = tm.real_outcome(day, 8, 74, 1268.30, 1268.65, 1269.20, True)
        self.assertEqual((outcome, at), ("stop", 9))
        self.assertEqual(len(later), 66)

    def test_unresolved_later_bars_only_warn(self):
        doc = self.machine()                                  # the +-5 plan never resolves in the fixture
        self.assertEqual(doc["warnings"],
                         ["66 bar(s) after the entry are already in the data and were not used: the odds are "
                          "from the entry bar only"])
        self.assertIn("Note: 66 bar(s) after the entry", "\n".join(tm.report(doc)))

    def test_refusal_still_names_the_time_and_bar_on_the_plan_day(self):
        # the 10:05 bar's low is 1268.05, so a stop at 1268.20 is hit one bar after the entry
        with self.assertRaises(tm.TMError) as cm:
            self.machine(stop=1268.20, target=1280.00, stop_rule="pct")
        msg = str(cm.exception)
        self.assertIn("stop was hit at 10:05 (bar 9) on 2026-10-26", msg)
        self.assertIn("(bar 9)", msg)


class OfflineAndErrors(unittest.TestCase):
    def test_offline_folder_finds_the_symbol_file(self):
        tmp = tempfile.mkdtemp(prefix="tm-india-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "RELIANCE-1m.json"), "w", encoding="utf-8") as fh:
            json.dump(read_payload(FIFTEEN), fh)
        argv = ["run", "--offline", tmp, "--now", "2026-10-26 18:00", "--json"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        code, out, err = run_cli(argv)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["source"], "offline")

    def test_missing_offline_file_is_a_clean_error(self):
        argv = ["run", "--offline", os.path.join(DIR, "nope.json"), "--now", "2026-10-26 18:00"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        code, out, err = run_cli(argv)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no offline candle file", err)

    def test_payload_without_candles_is_a_clean_error(self):
        tmp = tempfile.mkdtemp(prefix="tm-india-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"status": "error"}, fh)
        argv = ["run", "--offline", path, "--now", "2026-10-26 18:00"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        code, out, err = run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("no data.candles", err)


class ArtifactShape(unittest.TestCase):
    def machine(self):
        doc = read_payload(FIFTEEN)
        bars5 = bars5_of(payload_rows(doc))
        last = [b for b in bars5 if b["dt"].date().isoformat() == "2026-10-26"]
        p = tm.normalize_plan({"symbol": "RELIANCE", "side": "buy", "date": "2026-10-26",
                               "setup": "orb", "entry": round(last[8]["c"], 2),
                               "stop": round(last[8]["c"], 2) - 5,
                               "target": round(last[8]["c"], 2) + 5,
                               "entry_time": "10:00 AM",
                               "opening_range": {"minutes": 15}, "stop_rule": "auto",
                               "interval": "5m"})
        return tm.time_machine(bars5, p, tm.timeutil.parse_ist("2026-10-26 18:00"))

    def test_artifact_keeps_the_original_outcome_keys(self):
        out = self.machine()
        for key in ("plan", "mc", "history", "verdict", "verdict_notes", "caveats", "warnings", "note",
                    "created_by", "created_at", "source", "data", "sessions_used",
                    "window_start", "window_end", "complete_sessions", "dropped_sessions",
                    "caveat"):
            self.assertIn(key, out)
        for key in ("p_target", "p_stop", "p_neither", "p_green", "ci_target", "ci_stop",
                    "both_touched", "p_both", "fill", "median_close", "p5_close",
                    "p95_close", "avg_R", "r_at_target", "bands", "hist", "replays", "seed"):
            self.assertIn(key, out["mc"])
        self.assertEqual(out["mc"]["replays"], 10000)
        self.assertEqual(out["mc"]["seed"], 19)
        for key in ("sessions", "triggered", "target", "stop", "close", "green", "decided",
                    "win_rate", "win_ci", "fill", "avg_R", "rule", "trades"):
            self.assertIn(key, out["history"])

    def test_report_prints_window_and_caveat_lines(self):
        text = "\n".join(tm.report(self.machine()))
        self.assertIn("Window: 2026-10-05 to 2026-10-23 IST, 15 complete sessions used", text)
        self.assertIn("Caveat:", text)
        self.assertIn("Built from the same 15 sessions as the history", text)
        self.assertIn("Fills at the signal bar's close", text)
        self.assertIn("(95 % interval", text)
        self.assertIn("both touched in one bar:", text)
        self.assertNotIn("Yahoo", text)
        self.assertNotIn("60-day", text)
        self.assertNotIn("New York", text)


class Cli(unittest.TestCase):
    def test_write_saves_the_artifact(self):
        tmp = tempfile.mkdtemp(prefix="tm-india-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        target = os.path.join(tmp, "timemachine.json")
        argv = ["run", "--offline", FIFTEEN, "--seed", "19", "--now", "2026-10-26 18:00",
                "--write", target, "--json"] + \
            plan_flags(payload_rows(read_payload(FIFTEEN)), "2026-10-26")
        code, out, err = run_cli(argv)
        self.assertEqual((code, err), (0, ""))
        with open(target, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["sessions_used"], 15)
        self.assertEqual(doc["mc"]["seed"], 19)

    def test_no_subcommand_returns_2(self):
        code, out, err = run_cli([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
