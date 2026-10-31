"""T5: trader-entry and trader-exit, the India port. IST ORB trigger, tick-rounded
stops and targets, flat-by 15:10. No network: recorded Upstox fixtures only."""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from _load import FIX, core, load

core()  # repo root on sys.path

entry = load("trader-entry", "entry")
exitm = load("trader-exit", "exit")

from trader_core import instruments as instruments_mod  # noqa: E402
from trader_core import orders as orders_mod  # noqa: E402

DAY1 = os.path.join(FIX, "upstox", "RELIANCE-1m-2026-10-26.json")
DAY5 = os.path.join(FIX, "upstox", "RELIANCE-1m-5d.json")
MASTER = os.path.join(FIX, "upstox", "instruments-NSE.json.gz")
KEY = "NSE_EQ|INE002A01018"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def run(mod, argv, source=None):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv, source=source)
    return code, out.getvalue(), err.getvalue()


def make_source(path):
    doc, days, env = entry.load_offline(path)
    return entry.OfflineSource(doc, env), max(days)


def evaluate(src, day, hh, mm, **kw):
    """One evaluate() call pinned to IST. `day` may be a date or an ISO string."""
    if not isinstance(day, datetime.date):
        day = datetime.date.fromisoformat(str(day))
    args = dict(symbol="RELIANCE", instrument_key=KEY, day=day,
                at=datetime.datetime(2026, 10, 26, hh, mm, tzinfo=IST).replace(year=day.year, month=day.month, day=day.day))
    args.update(kw)
    return entry.evaluate(src, **args)


class Waiting(unittest.TestCase):
    """The range forms 09:15 to 09:30; before it completes the answer is waiting."""

    def setUp(self):
        self.src, self.day = make_source(DAY1)

    def test_waiting_at_0940_with_two_range_bars(self):
        # At 09:29 only the 09:15 and 09:20 bars are complete: the 09:25 bar
        # closes at 09:30. Pinned to the first day of the recorded window.
        res = evaluate(self.src, self.day, 9, 29)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(res["phase"], "forming")
        self.assertEqual(res["bars_closed"], 2)              # the 09:15 and 09:20 bars only
        self.assertEqual(res["opening_range"]["bars"], 2)
        self.assertFalse(res["opening_range"]["complete"])
        self.assertEqual(res["opening_range"]["high"], 1192.0)
        self.assertEqual(res["opening_range"]["low"], 1176.8)

    def test_pre_open_rows_are_ignored(self):
        res = evaluate(self.src, self.day, 9, 0)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(res["phase"], "pre-open")


class Triggered(unittest.TestCase):
    def setUp(self):
        self.src, self.day = make_source(DAY1)

    def test_triggered_at_10_with_the_pinned_numbers(self):
        res = evaluate(self.src, self.day, 10, 0)
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["trigger"]["at"], "2026-10-26 10:00 IST")
        self.assertEqual(res["trigger"]["price"], 1193.6)
        self.assertEqual(res["opening_range"]["high"], 1192.0)
        self.assertEqual(res["opening_range"]["low"], 1175.3)
        self.assertEqual(res["opening_range"]["mid"], 1183.65)

    def test_only_bars_closed_by_the_clock_count(self):
        res = evaluate(self.src, self.day, 9, 29)            # the 09:25 bar is not done
        self.assertEqual(res["status"], "waiting")
        self.assertEqual([b["dt"].strftime("%H:%M") for b in res["_bars"]][-1], "09:20")

    def test_second_close_above_does_not_refire(self):
        res = evaluate(self.src, self.day, 10, 30)
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["trigger"]["bar_index"], 8)      # the 09:55 bar, once


class Expired(unittest.TestCase):
    def test_expired_after_the_entry_window_on_oct_30(self):
        src, _ = make_source(DAY5)
        res = evaluate(src, datetime.date(2026, 10, 30), 15, 5)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(res["opening_range"]["high"], 1171.6)
        self.assertEqual(res["opening_range"]["low"], 1162.0)
        self.assertIn("15:00", res["reason"])


class Holiday(unittest.TestCase):
    def test_no_trigger_on_a_holiday(self):
        src, _ = make_source(DAY1)
        res = evaluate(src, datetime.date(2026, 10, 20), 11, 0)   # Dussehra
        self.assertEqual(res["status"], "no-data")
        self.assertIn("closed", res["reason"])


class EntryCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_triggered_writes_entry_json(self):
        out = os.path.join(self.tmp, "trader", "entry.json")
        code, text, err = run(entry, ["check", "RELIANCE", "--key", KEY, "--now", "2026-10-26 10:00",
                                      "--offline", DAY1, "--master", MASTER, "--write", out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["status"], "triggered")
        self.assertEqual(doc["instrument_key"], KEY)
        self.assertEqual(doc["time"], "2026-10-26 10:00 IST")
        self.assertEqual(doc["source"], "upstox")
        self.assertEqual(doc["tick_size"], 0.05)
        self.assertEqual(doc["lot_size"], 1)
        saved = json.load(open(out, encoding="utf-8"))
        self.assertEqual(saved["status"], "triggered")
        self.assertEqual(saved["opening_range"]["mid"], 1183.65)

    def test_check_expired_exits_1(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--key", KEY, "--now", "2026-10-30 15:05",
                                 "--offline", DAY5, "--json"])
        self.assertEqual(code, 1)


def master_row(tick, lot=1):
    # tick is INR; raw Upstox masters carry paise, so multiply on the way out.
    return {"instrument_key": KEY, "exchange_token": "2885", "trading_symbol": "RELIANCE",
            "isin": "INE002A01018", "instrument_type": "EQ", "lot_size": float(lot),
            "tick_size": float(tick) * 100.0, "freeze_quantity": 11111.0}


class Rounding(unittest.TestCase):
    """compute() rounds away from the entry; a one-tick minimum risk."""

    def test_tick_05_keeps_the_pinned_stop_and_target(self):
        c = exitm.compute(1193.6, "or_mid", None, "r", exitm.as_decimal(1), 1192.0, 1175.3, tick=0.05)
        self.assertEqual(c["stop"], 1183.65)
        self.assertEqual(c["target"], 1203.55)
        self.assertEqual(c["risk"], 9.95)

    def test_tick_1_rounds_stop_down_and_target_up(self):
        c = exitm.compute(100.0, "price", exitm.as_decimal(97.4), "price", exitm.as_decimal(105.4), tick=1.0)
        self.assertEqual(c["stop"], 97.0)
        self.assertEqual(c["target"], 106.0)
        self.assertEqual(c["risk"], 3.0)

    def test_tick_10_rounds_away_from_entry(self):
        c = exitm.compute(100.0, "price", exitm.as_decimal(96.5), "price", exitm.as_decimal(103.5), tick=10.0)
        self.assertEqual(c["stop"], 90.0)
        self.assertEqual(c["target"], 110.0)

    def test_rounding_never_rounds_risk_down_one_tick_minimum(self):
        # A 101.20 entry with a 100.90 stop and tick 1: nearest would round the
        # stop up to 101 and eat the whole risk. The stop stays at 100, and a
        # raw stop that rounds to the entry itself is widened back out so the
        # risk is never under one tick.
        c = exitm.compute(101.2, "price", exitm.as_decimal(100.9), "price", exitm.as_decimal(102.0), tick=1.0)
        self.assertEqual(c["stop"], 100.0)
        self.assertGreaterEqual(c["risk"], 1.0)
        # nearest would have put the stop ON the entry: refused, then widened.
        c2 = exitm.compute(101.2, "price", exitm.as_decimal(101.1), "price", exitm.as_decimal(103.0), tick=1.0)
        self.assertEqual(c2["stop"], 100.0)
        self.assertGreaterEqual(c2["risk"], 1.0)

    def test_on_grid_sanity(self):
        self.assertTrue(orders_mod.on_grid(1183.65, 0.05))
        self.assertTrue(orders_mod.on_grid(97.0, 1.0))


FULL_SESSION = {"open": "09:15", "entry_start": "09:30", "entry_cutoff": "15:00",
                "flat_by": "15:10", "close": "15:30", "mis_square_off": "15:25",
                "mis_square_off_cas": "15:10"}


class FlatBy(unittest.TestCase):
    def test_flat_by_defaults_to_15_10_ist(self):
        t, _ = exitm.resolve_flat_by(datetime.date(2026, 10, 26), None,
                                     {"session": dict(FULL_SESSION, flat_by="15:10")},
                                     datetime.time(10, 0))
        self.assertEqual(t, "15:10")

    def test_entry_at_or_after_flat_by_is_refused(self):
        with self.assertRaises(exitm.PlanError):
            exitm.resolve_flat_by(datetime.date(2026, 10, 26), None,
                                  {"session": dict(FULL_SESSION, flat_by="15:10")},
                                  datetime.time(15, 10))


class PlanCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.entry_path = os.path.join(self.tmp, "trader", "entry.json")
        entry_json = {
            "created_by": "trader-entry", "symbol": "RELIANCE", "instrument_key": KEY, "setup": "orb",
            "side": "buy", "date": "2026-10-26", "status": "triggered", "time": "2026-10-26 10:00 IST",
            "bar_start": "09:55", "bar_index": 8, "price": 1193.6, "level": 1192.0, "interval": "5m",
            "opening_range": {"high": 1192.0, "low": 1175.3, "mid": 1183.65, "minutes": 15},
            "source": "upstox", "tick_size": 0.05, "lot_size": 1,
        }
        os.makedirs(os.path.dirname(self.entry_path))
        with open(self.entry_path, "w", encoding="utf-8") as fh:
            json.dump(entry_json, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, *extra):
        out = os.path.join(self.tmp, "trader", "exit.json")
        argv = ["plan", "--from", self.entry_path, "--master", MASTER, "--target", "1R",
                "--now", "2026-10-26 10:05", "--write", out, "--json"] + list(extra)
        code, text, err = run(exitm, argv)
        return code, (json.load(open(out, encoding="utf-8")) if code == 0 else None), err

    def test_plan_from_entry_json_carries_the_pinned_contract(self):
        code, plan, err = self.plan("--budget", "50000")
        self.assertEqual(code, 0, err)
        self.assertEqual(plan["stop"], 1183.65)
        self.assertEqual(plan["target"], 1203.55)
        self.assertEqual(plan["risk_per_share"], 9.95)
        self.assertEqual(plan["flat_by"], "15:10")
        self.assertEqual(plan["instrument_key"], KEY)
        self.assertEqual(plan["tick_size"], 0.05)
        self.assertEqual(plan["lot_size"], 1)
        self.assertEqual(plan["quantity"], 25)          # 250 max loss / 9.95 risk, whole lots of 1
        self.assertEqual(plan["risk_inr"], 248.75)
        self.assertEqual(plan["reward_risk"], 1.0)
        self.assertEqual(plan["direction"], "long")
        self.assertEqual(plan["product"], "I")
        self.assertIn("IST", plan["generated_at"])
        self.assertEqual(len(plan["legs"]), 3)
        for field in ("stop", "target"):
            self.assertTrue(orders_mod.on_grid(plan[field], 0.05))

    def test_plan_accepted_by_orders_validate_plan(self):
        code, plan, err = self.plan("--budget", "50000")
        self.assertEqual(code, 0, err)
        rows = instruments_mod.load(open(MASTER, "rb").read())
        inst = instruments_mod.find(rows, "RELIANCE")
        self.assertEqual(orders_mod.validate_plan(plan, inst, {}), [])

    def test_plan_without_a_budget_leaves_quantity_out(self):
        # --budget 0 opts out of sizing: quantity stays out rather than guessed.
        code, plan, err = self.plan("--target", "2R", "--budget", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("quantity", plan)
        self.assertIsNone(plan.get("risk_inr"))
        self.assertEqual(plan["legs"], [])

    def test_tick_10_master_rounds_the_plan(self):
        doc = master_row(10.0)
        tmp_master = os.path.join(self.tmp, "master10.json")
        with open(tmp_master, "w", encoding="utf-8") as fh:
            json.dump([doc], fh)
        code, plan, err = self.plan("--master", tmp_master, "--target", "1R", "--budget", "50000")
        self.assertEqual(code, 0, err)
        self.assertEqual(plan["tick_size"], 10.0)
        self.assertEqual(plan["stop"], 1180.0)           # 1183.65 rounds down, away from the entry
        self.assertEqual(plan["target"], 1210.0)         # 1203.55 rounds up, away from the entry
        self.assertGreaterEqual(plan["risk_per_share"], 10.0)   # never under one tick

    def test_show_prints_the_saved_plan(self):
        out = os.path.join(self.tmp, "trader", "exit.json")
        run(exitm, ["plan", "--from", self.entry_path, "--master", MASTER, "--now", "2026-10-26 10:05",
                    "--write", out])
        code, text, err = run(exitm, ["show", "--plan", out, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["stop"], 1183.65)

    def test_show_tolerates_a_sized_plan_without_labels(self):
        # trader-budget --write saves the sized plan; it has no stop_label/target_label.
        plan = {"symbol": "RELIANCE", "date": "2026-10-26", "entry": 1193.6, "stop": 1183.65,
                "target": 1203.55, "risk_per_share": 9.95, "reward_risk": 1.0, "flat_by": "15:10",
                "stop_rule": "or_mid", "target_rule": "r"}
        out = os.path.join(self.tmp, "trader", "sized.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        code, text, err = run(exitm, ["show", "--plan", out, "--json"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(text)["stop"], 1183.65)
        code, text, err = run(exitm, ["show", "--plan", out])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("Stop", text)
        self.assertIn("Target", text)
        self.assertIn("or_mid", text)


class AtrStop(unittest.TestCase):
    """--stop atr:K reads the session's 5m bars through the one resample helper
    and stops K x ATR under the entry; the default write path is trader/plan.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-exit-atr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "config.json")          # absent: the defaults stand

    def argv(self, write=None):
        argv = ["plan", "--symbol", "RELIANCE", "--key", KEY, "--entry", "1193.6",
                "--or-high", "1192.0", "--or-low", "1175.3", "--setup", "orb",
                "--stop", "atr:1.5", "--offline", DAY1, "--date", "2026-10-26",
                "--time", "10:00", "--entry-bar", "8", "--now", "2026-10-26 10:05",
                "--master", MASTER, "--target", "1R", "--budget", "50000",
                "--config", self.cfg, "--json", "--write"]
        return argv if write is None else argv + [write]

    def test_atr_stop_is_rounded_and_the_legs_validate(self):
        out = os.path.join(self.tmp, "plan.json")
        code, text, err = run(exitm, self.argv(out))
        self.assertEqual(code, 0, err)                           # no NameError, no refusal
        plan = json.loads(text)
        self.assertEqual(plan["stop_rule"], "atr")
        self.assertEqual(plan["atr_bars"], 9)                    # slots 0..8, the entry bar last
        self.assertAlmostEqual(plan["atr"], 6.3222, places=4)
        self.assertEqual(plan["stop"], 1184.1)                   # entry - 1.5 x ATR, tick-rounded down
        self.assertEqual(plan["target"], 1203.1)                 # 1R above the rounded risk
        self.assertTrue(orders_mod.on_grid(plan["stop"], 0.05))
        self.assertTrue(orders_mod.on_grid(plan["target"], 0.05))
        self.assertEqual([one["role"] for one in plan["legs"]], ["entry", "stop", "target"])
        rows = instruments_mod.load(open(MASTER, "rb").read())
        inst = instruments_mod.find(rows, "RELIANCE")
        self.assertEqual(orders_mod.validate_plan(plan, inst, {}), [])

    def test_write_without_a_path_uses_the_shared_plan_path(self):
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, text, err = run(exitm, self.argv())
        finally:
            os.chdir(old)
        self.assertEqual(code, 0, err)
        saved = os.path.join(self.tmp, "trader", "plan.json")
        self.assertTrue(os.path.exists(saved), saved)
        with open(saved, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["stop"], 1184.1)
        self.assertEqual(json.loads(text)["stop"], 1184.1)


class CliBoundary(unittest.TestCase):
    """Bad user input exits 2 with one line, never a traceback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-cli-boundary-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bad_cfg = os.path.join(self.tmp, "config.json")
        with open(self.bad_cfg, "w", encoding="utf-8") as fh:
            json.dump({"mode": "paper"}, fh)

    def test_exit_bad_now_and_bad_mode_exit_2(self):
        base = ["plan", "--symbol", "RELIANCE", "--entry", "1193.6", "--date", "2026-10-26",
                "--or-high", "1192", "--or-low", "1175.3"]
        code, out, err = run(exitm, base + ["--now", "not-a-time"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("exit.py: "), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        code, out, err = run(exitm, base + ["--config", self.bad_cfg])
        self.assertEqual(code, 2)
        self.assertIn("mode must be one of", err)
        self.assertNotIn("Traceback", err)

    def test_entry_bad_now_and_bad_mode_exit_2(self):
        code, out, err = run(entry, ["check", "RELIANCE", "--key", KEY, "--offline", DAY1,
                                     "--now", "not-a-time"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("entry.py: "), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        code, out, err = run(entry, ["check", "RELIANCE", "--key", KEY, "--offline", DAY1,
                                     "--config", self.bad_cfg])
        self.assertEqual(code, 2)
        self.assertIn("mode must be one of", err)
        self.assertNotIn("Traceback", err)


class DeadUsSnippets(unittest.TestCase):
    """Dollar forms are gone; plain INR prices parse exactly as before."""

    def test_dollar_prices_are_rejected(self):
        for text in ("price:$1193.65", "pct:$1.5"):
            with self.assertRaises(exitm.PlanError):
                exitm.parse_stop(text)
        with self.assertRaises(exitm.PlanError):
            exitm.parse_target("price:$1203.55")
        with self.assertRaises(exitm.PlanError):
            exitm.as_decimal("$1193.65")

    def test_plain_inr_prices_still_parse(self):
        self.assertEqual(str(exitm.parse_stop("price:inr 1183.65")[1]), "1183.65")
        self.assertEqual(str(exitm.parse_stop("price:1,183.65")[1]), "1183.65")
        self.assertEqual(str(exitm.parse_target("price:1,203.55")[1]), "1203.55")
        self.assertEqual(str(exitm.as_decimal("INR 1193.60")), "1193.60")


if __name__ == "__main__":
    unittest.main()
