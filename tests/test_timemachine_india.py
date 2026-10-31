"""India tests for trader-timemachine: Upstox 1m resampled to 5m, 15 to 20
session window, deterministic seed, no 5m historical call."""

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


def plan_flags(rows, plan_date):
    """CLI plan flags priced off the plan day's own 10:00 AM bar (bar 8): the
    entry is that bar's close, the stop 5 below, the target 5 above."""
    entry = round(day_bar(rows, plan_date, 8)["c"], 2)
    return ["--symbol", "RELIANCE", "--date", plan_date,
            "--entry", str(entry), "--stop", str(entry - 5), "--target", str(entry + 5),
            "--entry-time", "10:00 AM"]


def run_cli(argv, source=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv, source)
    return code, out.getvalue(), err.getvalue()


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
        self.assertEqual(call["instrument_key"], "NSE_EQ|RELIANCE")
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
        self.assertEqual(src.calls[0]["instrument_key"], "NSE_EQ|RELIANCE")
        self.assertEqual(doc["data"]["instrument_key"], "NSE_EQ|RELIANCE")
        for call in src.calls:
            self.assertNotEqual(call["interval"], "5m")

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
        for key in ("plan", "mc", "history", "verdict", "verdict_notes", "warnings", "note",
                    "created_by", "created_at", "source", "data", "sessions_used",
                    "window_start", "window_end", "complete_sessions", "dropped_sessions",
                    "caveat"):
            self.assertIn(key, out)
        for key in ("p_target", "p_stop", "p_neither", "p_green", "median_close", "p5_close",
                    "p95_close", "avg_R", "r_at_target", "bands", "hist", "replays", "seed"):
            self.assertIn(key, out["mc"])
        self.assertEqual(out["mc"]["replays"], 10000)
        self.assertEqual(out["mc"]["seed"], 19)
        for key in ("sessions", "triggered", "target", "stop", "close", "green", "avg_R",
                    "rule", "trades"):
            self.assertIn(key, out["history"])

    def test_report_prints_window_and_caveat_lines(self):
        text = "\n".join(tm.report(self.machine()))
        self.assertIn("Window: 2026-10-05 to 2026-10-23 IST, 15 complete sessions used", text)
        self.assertIn("Caveat:", text)
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
