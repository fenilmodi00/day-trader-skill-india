"""Offline tests for the core foundation: IST clock, NSE calendar, config, state."""

import datetime
import json
import os
import tempfile
import unittest

from _load import core

core()

from trader_core import IST, calendar, config, timeutil  # noqa: E402

MON = datetime.datetime(2026, 10, 26, 10, 0, tzinfo=IST)  # a regular Monday
REPUBLIC_DAY = datetime.date(2026, 1, 26)


def ist(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=IST)


class TimeUtil(unittest.TestCase):
    def test_now_ist_is_aware_at_fixed_offset(self):
        now = timeutil.now_ist()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), datetime.timedelta(hours=5, minutes=30))

    def test_to_ist_takes_naive_as_ist_and_converts_aware(self):
        naive = datetime.datetime(2026, 10, 26, 9, 15)
        self.assertEqual(timeutil.to_ist(naive).tzinfo, IST)
        utc = datetime.datetime(2026, 10, 26, 3, 45, tzinfo=datetime.timezone.utc)
        self.assertEqual(timeutil.to_ist(utc), ist(2026, 10, 26, 9, 15))

    def test_parse_ist_handles_iso_and_space_forms(self):
        self.assertEqual(timeutil.parse_ist("2026-10-26T10:00+05:30"), ist(2026, 10, 26, 10, 0))
        self.assertEqual(timeutil.parse_ist("2026-10-26 10:00"), ist(2026, 10, 26, 10, 0))
        self.assertEqual(timeutil.parse_ist("2026-10-26T04:30:00Z"), ist(2026, 10, 26, 10, 0))
        with self.assertRaises(ValueError):
            timeutil.parse_ist("not a time")

    def test_fmt_ist(self):
        self.assertEqual(timeutil.fmt_ist(MON), "2026-10-26 10:00 IST")
        self.assertEqual(timeutil.fmt_ist(MON, seconds=True), "2026-10-26 10:00:00 IST")
        utc = datetime.datetime(2026, 10, 26, 4, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(timeutil.fmt_ist(utc), "2026-10-26 10:00 IST")


class SessionPhase(unittest.TestCase):
    def test_boundaries(self):
        d = MON
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 8, 59)), "closed")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 9, 0)), "preopen")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 9, 14)), "preopen")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 9, 15)), "regular")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 15, 19)), "regular")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 15, 20)), "closing")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 15, 29)), "closing")
        self.assertEqual(timeutil.session_phase(ist(d.year, d.month, d.day, 15, 30)), "closed")

    def test_phase_is_time_of_day_only(self):
        self.assertEqual(timeutil.session_phase(ist(2026, 10, 25, 10, 0)), "regular")


class MarketOpen(unittest.TestCase):
    def test_open_boundaries_on_a_trading_day(self):
        self.assertFalse(timeutil.is_open(ist(2026, 10, 26, 9, 14)))
        self.assertTrue(timeutil.is_open(ist(2026, 10, 26, 9, 15)))
        self.assertTrue(timeutil.is_open(ist(2026, 10, 26, 15, 29)))
        self.assertFalse(timeutil.is_open(ist(2026, 10, 26, 15, 30)))

    def test_weekend_is_closed(self):
        self.assertFalse(timeutil.is_open(ist(2026, 10, 24, 11, 0)))
        self.assertFalse(timeutil.is_open(ist(2026, 10, 25, 11, 0)))

    def test_holiday_is_closed(self):
        self.assertTrue(calendar.is_holiday(REPUBLIC_DAY))
        self.assertFalse(calendar.is_trading_day(REPUBLIC_DAY))
        self.assertFalse(timeutil.is_open(ist(2026, 1, 26, 11, 0)))


class NseCalendar(unittest.TestCase):
    def test_holiday_table_is_weekday_only_and_realistic(self):
        holidays = calendar.load_holidays()
        self.assertIn("2026-01-26", holidays)
        self.assertIn("2026-10-20", holidays)
        self.assertIn("2026-12-25", holidays)
        for key in holidays:
            day = datetime.date.fromisoformat(key)
            self.assertLess(day.weekday(), 5, key)

    def test_load_holidays_tolerates_absent_file_and_explicit_path(self):
        self.assertEqual(calendar.load_holidays(os.path.join(tempfile.gettempdir(), "nope.json")), {})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"2026-01-26": "Republic Day"}, fh)
            self.assertEqual(calendar.load_holidays(path), {"2026-01-26": "Republic Day"})

    def test_next_open_from_before_the_bell_same_day(self):
        self.assertEqual(calendar.next_open(ist(2026, 10, 23, 8, 0)), ist(2026, 10, 23, 9, 15))

    def test_next_open_is_strictly_after(self):
        self.assertEqual(calendar.next_open(ist(2026, 10, 23, 9, 15)), ist(2026, 10, 26, 9, 15))
        self.assertEqual(calendar.next_open(ist(2026, 10, 23, 9, 16)), ist(2026, 10, 26, 9, 15))

    def test_next_open_from_friday_evening_lands_on_monday(self):
        self.assertEqual(calendar.next_open(ist(2026, 10, 23, 15, 30)), ist(2026, 10, 26, 9, 15))

    def test_next_open_skips_weekend_and_holiday(self):
        self.assertEqual(calendar.next_open(ist(2026, 1, 24, 12, 0)), ist(2026, 1, 27, 9, 15))
        self.assertEqual(calendar.next_open(ist(2026, 1, 26, 10, 0)), ist(2026, 1, 27, 9, 15))

    def test_next_open_accepts_naive_input_as_ist(self):
        self.assertEqual(calendar.next_open(datetime.datetime(2026, 10, 25, 20, 0)), ist(2026, 10, 26, 9, 15))

    def test_trading_days_is_inclusive(self):
        days = calendar.trading_days(datetime.date(2026, 10, 24), datetime.date(2026, 10, 27))
        self.assertEqual(days, [datetime.date(2026, 10, 26), datetime.date(2026, 10, 27)])
        self.assertEqual(calendar.trading_days(datetime.date(2026, 10, 27), datetime.date(2026, 10, 26)), [])


def temp_cfg(**over):
    tmp = tempfile.mkdtemp(prefix="dt-core-test-")
    cfg = config.load(os.path.join(tmp, "absent-config.json"))
    cfg["state_dir"] = tmp
    cfg.update(over)
    return cfg


class LiveGate(unittest.TestCase):
    def test_sandbox_needs_no_confirmation(self):
        cfg = temp_cfg()
        self.assertFalse(config.is_live(cfg))
        config.assert_live_allowed(cfg)

    def test_live_without_confirmation_raises(self):
        cfg = temp_cfg(mode="live")
        with self.assertRaises(config.LiveGateError):
            config.assert_live_allowed(cfg)

    def test_wrong_phrase_is_rejected(self):
        cfg = temp_cfg(mode="live")
        with self.assertRaises(config.LiveGateError):
            config.confirm_live(cfg, "go live with upstox")
        with self.assertRaises(config.LiveGateError):
            config.assert_live_allowed(cfg)

    def test_correct_phrase_then_confirmation_allows_live(self):
        cfg = temp_cfg(mode="live")
        path = config.confirm_live(cfg, config.LIVE_PHRASE)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertNotIn(config.LIVE_PHRASE, stored)
        self.assertEqual(stored["sha256"], config._phrase_sha())
        config.assert_live_allowed(cfg)

    def test_corrupt_confirmation_still_blocks(self):
        cfg = temp_cfg(mode="live")
        config.confirm_live(cfg, config.LIVE_PHRASE)
        with open(os.path.join(cfg["state_dir"], config.CONFIRM_STATE), "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        with self.assertRaises(config.LiveGateError):
            config.assert_live_allowed(cfg)


class ConfigValidation(unittest.TestCase):
    def test_load_with_no_file_yields_sandbox_defaults(self):
        cfg = temp_cfg()
        self.assertEqual(config.mode(cfg), "sandbox")
        self.assertEqual(cfg["broker"], "upstox")
        times = config.session_times(cfg)
        self.assertEqual(times["entry_start"], datetime.time(9, 30))
        self.assertEqual(times["entry_cutoff"], datetime.time(15, 0))
        self.assertEqual(times["flat_by"], datetime.time(15, 10))
        self.assertEqual(times["mis_square_off"], datetime.time(15, 25))
        self.assertEqual(times["mis_square_off_cas"], datetime.time(15, 10))

    def test_load_deep_merges_user_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"account": {"max_trades_per_day": 2}, "universe": {"symbols": ["TCS"]}}, fh)
            cfg = config.load(path)
        self.assertEqual(cfg["account"]["max_trades_per_day"], 2)
        self.assertEqual(cfg["account"]["max_loss_per_trade_inr"], 250.0)
        self.assertEqual(cfg["universe"]["symbols"], ["TCS"])

    def test_bad_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"mode": "yolo"}, fh)
            with self.assertRaises(config.ConfigError):
                config.load(path)
        cfg = temp_cfg()
        cfg["mode"] = None
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        del cfg["mode"]
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_bad_values_fail_closed(self):
        cfg = temp_cfg()
        cfg["session"]["entry_start"] = "25:99"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        cfg = temp_cfg()
        cfg["account"]["max_loss_per_trade_inr"] = -1
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        cfg = temp_cfg()
        cfg["mystery_key"] = 1
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        cfg = temp_cfg()
        cfg["access_token"] = "secret-value"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_mode_helper_rejects_unknown(self):
        with self.assertRaises(config.ConfigError):
            config.mode({"mode": "paper"})


class State(unittest.TestCase):
    def test_state_dir_defaults_under_the_repo(self):
        cfg = config.load(os.path.join(tempfile.gettempdir(), "absent-config.json"))
        self.assertEqual(config.state_dir(cfg), os.path.join(config.REPO_ROOT, "trader", "state"))
        cfg["state_dir"] = "relative/state"
        self.assertTrue(os.path.isabs(config.state_dir(cfg)))

    def test_atomic_write_round_trip(self):
        cfg = temp_cfg()
        payload = {"alerts": [{"symbol": "RELIANCE", "at": "2026-10-26 10:00 IST"}]}
        path = config.state_write(cfg, "alerts.json", payload)
        self.assertEqual(path, os.path.join(cfg["state_dir"], "alerts.json"))
        self.assertEqual(config.state_read(cfg, "alerts.json"), payload)
        leftovers = [f for f in os.listdir(cfg["state_dir"]) if ".tmp-" in f]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_returns_default(self):
        cfg = temp_cfg()
        with open(os.path.join(cfg["state_dir"], "broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(config.state_read(cfg, "broken.json"))
        self.assertEqual(config.state_read(cfg, "broken.json", default={"ok": True}), {"ok": True})
        self.assertIsNone(config.state_read(cfg, "missing.json"))

    def test_state_write_rejects_path_traversal(self):
        cfg = temp_cfg()
        with self.assertRaises(config.ConfigError):
            config.state_write(cfg, os.path.join("..", "escape.json"), {})


class PackageExports(unittest.TestCase):
    def test_seed_constants_survive(self):
        import trader_core

        self.assertEqual(trader_core.__version__, "3.0.0")
        self.assertEqual(trader_core.IST.utcoffset(None), datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(trader_core.OPEN, datetime.time(9, 15))
        self.assertEqual(trader_core.CLOSE, datetime.time(15, 30))
        self.assertEqual(timeutil.IST, trader_core.IST)


if __name__ == "__main__":
    unittest.main()
