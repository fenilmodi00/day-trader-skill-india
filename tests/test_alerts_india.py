"""India tests for trader-alerts: IST bells, stops, targets, flat-by, once-only firing."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

from trader_core.http import UpstoxAuthError

alerts = load("trader-alerts", "alerts")

DIR = os.path.join(FIX, "trader-alerts", "india")
PLAN = os.path.join(DIR, "exit.json")
MON = "2026-10-26"  # a trading Monday in the seeded fixtures week


def plan():
    with open(PLAN, encoding="utf-8") as fh:
        return json.load(fh)


def ist(text):
    return alerts.timeutil.parse_ist(text)


class FakeSource(object):
    """The seam main() injects in tests: .quote(keys), like upstox.Client."""

    def __init__(self, bars=None, error=None):
        self.bars = bars or {}
        self.error = error
        self.asked = []

    def quote(self, keys):
        self.asked = list(keys)
        if self.error is not None:
            raise self.error
        return {k: v for k, v in self.bars.items() if k in keys}


def bar(ltp):
    return {"ltp": ltp, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "prev_close": ltp}


KEY = "NSE_EQ|INE002A01018"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alerts-india-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rules = os.path.join(self.tmp, "trader", "alerts.json")
        self.config = os.path.join(self.tmp, "config.json")
        os.makedirs(os.path.dirname(self.rules))
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump({"state_dir": os.path.join(self.tmp, "state")}, fh)

    def write_rules(self, rules):
        with open(self.rules, "w", encoding="utf-8") as fh:
            json.dump({"rules": rules}, fh)

    def run_cli(self, argv, source=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = alerts.main(argv, source)
        return code, out.getvalue(), err.getvalue()

    def check(self, now, rules, source=None, json_mode=False):
        self.write_rules(rules)
        argv = ["check", "--config", self.config, "--rules", self.rules, "--now", now]
        if json_mode:
            argv.append("--json")
        code, out, err = self.run_cli(argv, source)
        doc = json.loads(out) if json_mode else None
        return code, doc, out, err

    def state(self):
        with open(os.path.join(self.tmp, "state", "alerts.json"), encoding="utf-8") as fh:
            return json.load(fh)


class AddPlan(Base):
    def test_add_from_plan_arms_stop_target_flat_by(self):
        code, out, err = self.run_cli(["add", "--rules", self.rules, "--config", self.config,
                                   "--plan", PLAN, "--from-plan"])
        self.assertEqual((code, err), (0, ""))
        with open(self.rules, encoding="utf-8") as fh:
            rules = {r["id"]: r for r in json.load(fh)["rules"]}
        self.assertEqual(rules["RELIANCE-stop"]["price"], 1183.65)
        self.assertEqual(rules["RELIANCE-target"]["price"], 1203.55)
        self.assertEqual(rules["RELIANCE-flat-by"]["at"], "15:10")
        self.assertEqual(rules["RELIANCE-stop"]["instrument_key"], KEY)
        self.assertEqual(rules["RELIANCE-stop"]["date"], MON)

    def test_add_price_alert_needs_a_symbol(self):
        code, out, err = self.run_cli(["add", "--rules", self.rules, "--above", "1200"])
        self.assertEqual(code, 2)
        self.assertIn("--symbol is required", err)

    def test_add_price_alert_writes_the_rule(self):
        code, out, err = self.run_cli(["add", "--rules", self.rules, "--symbol", "RELIANCE",
                                   "--instrument-key", KEY, "--above", "1200"])
        self.assertEqual((code, err), (0, ""))
        with open(self.rules, encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules, [{"id": "RELIANCE-above-1200", "symbol": "RELIANCE",
                                  "type": "above", "price": 1200.0,
                                  "instrument_key": KEY}])

    def test_add_bells_and_flat_by(self):
        code, _, err = self.run_cli(["add", "--rules", self.rules, "--opening-bell"])
        self.assertEqual((code, err), (0, ""))
        code, _, err = self.run_cli(["add", "--rules", self.rules, "--flat-by"])
        self.assertEqual((code, err), (0, ""))
        with open(self.rules, encoding="utf-8") as fh:
            rules = {r["id"] for r in json.load(fh)["rules"]}
        self.assertEqual(rules, {"bell-open", "flat-by"})

    def test_plan_command_prints_the_plan_file(self):
        code, out, err = self.run_cli(["plan", "--plan", PLAN])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["symbol"], "RELIANCE")

    def test_plan_command_falls_back_to_the_example(self):
        code, out, err = self.run_cli(["plan", "--plan", os.path.join(self.tmp, "nope.json")])
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertIn("rules", doc)


class Bells(Base):
    def test_preopen_open_and_close_bells_fire_once_each(self):
        rules = [{"id": "bell-preopen", "type": "bell", "bell": "preopen"},
                 {"id": "bell-open", "type": "bell", "bell": "open"},
                 {"id": "bell-close", "type": "bell", "bell": "close"}]
        src = FakeSource()
        # 09:14: the pre-open bell rings (it is 09:00 past), the open bell is pending
        code, doc, _, err = self.check(MON + " 09:14", rules, src, json_mode=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([f["id"] for f in doc["fired"]], ["bell-preopen"])
        self.assertEqual([p["id"] for p in doc["pending"]], ["bell-open", "bell-close"])
        # 09:16: only the open bell is newly fired, pre-open rang at 09:14 already
        code, doc, _, err = self.check(MON + " 09:16", rules, src, json_mode=True)
        self.assertEqual(code, 0)
        self.assertEqual([f["id"] for f in doc["fired"]], ["bell-open"])
        # a second check the same session: quiet, still exit 0
        code, doc, _, err = self.check(MON + " 10:00", rules, src, json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        # next day re-arms
        code, doc, _, err = self.check("2026-10-27 09:16", rules, src, json_mode=True)
        self.assertEqual(code, 0)
        self.assertEqual([f["id"] for f in doc["fired"]], ["bell-preopen", "bell-open"])

    def test_0914_does_not_fire_the_open_bell(self):
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"}]
        code, doc, out, err = self.check(MON + " 09:14", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        self.assertIn("bell-open", out)  # listed as pending

    def test_0915_fires_the_open_bell(self):
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"}]
        code, doc, out, err = self.check(MON + " 09:15", rules, FakeSource(), json_mode=True)
        self.assertEqual(code, 0)
        self.assertEqual([f["id"] for f in doc["fired"]], ["bell-open"])
        self.assertEqual(doc["fired"][0]["at"], MON + " 09:15 IST")

    def test_close_bell_rings_after_1530(self):
        rules = [{"id": "bell-close", "type": "bell", "bell": "close"}]
        code, doc, _, err = self.check(MON + " 15:31", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, [f["id"] for f in doc["fired"]]), (0, ["bell-close"]))

    def test_holiday_produces_no_bells(self):
        # 2026-10-20 is Dussehra in the checked-in NSE holiday table
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"},
                 {"id": "bell-close", "type": "bell", "bell": "close"},
                 {"id": "flat", "type": "flat_by", "at": "15:10"}]
        code, doc, out, err = self.check("2026-10-20 09:16", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        self.assertEqual(doc["errors"], [])
        self.assertIn("Dussehra", out)
        self.assertIn("closed", out)

    def test_weekend_produces_no_bells(self):
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"}]
        code, doc, out, err = self.check("2026-10-24 09:16", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        self.assertIn("closed", out)


class StopsTargets(Base):
    RULES = [{"id": "rel-stop", "symbol": "RELIANCE", "instrument_key": KEY,
              "type": "stop", "price": 1183.65},
             {"id": "rel-target", "symbol": "RELIANCE", "instrument_key": KEY,
              "type": "target", "price": 1203.55}]

    def test_stop_and_target_fire_exactly_once(self):
        src = FakeSource({KEY: bar(1183.65)})  # LTP exactly at the stop
        code, doc, _, err = self.check(MON + " 10:00", self.RULES, src, json_mode=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([f["id"] for f in doc["fired"]], ["rel-stop"])
        self.assertEqual(src.asked, [KEY])  # one batched quote request
        # second check, same session: no refire, exit 0
        code, doc, _, err = self.check(MON + " 10:05", self.RULES, src, json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        # next day: re-armed and rings again
        code, doc, _, err = self.check("2026-10-27 10:00", self.RULES, src, json_mode=True)
        self.assertEqual([f["id"] for f in doc["fired"]], ["rel-stop"])

    def test_target_and_above_fire_at_or_above_the_level(self):
        rules = [{"id": "t", "symbol": "RELIANCE", "instrument_key": KEY,
                  "type": "target", "price": 1203.55}]
        src = FakeSource({KEY: bar(1203.55)})
        code, doc, _, _ = self.check(MON + " 11:00", rules, src, json_mode=True)
        self.assertEqual([f["id"] for f in doc["fired"]], ["t"])

    def test_below_fires_at_or_below_the_level(self):
        rules = [{"id": "b", "symbol": "RELIANCE", "instrument_key": KEY,
                  "type": "below", "price": 1170.0}]
        src = FakeSource({KEY: bar(1169.95)})
        code, doc, _, _ = self.check(MON + " 11:00", rules, src, json_mode=True)
        self.assertEqual([f["id"] for f in doc["fired"]], ["b"])

    def test_unhit_price_alert_is_pending(self):
        src = FakeSource({KEY: bar(1190.0)})
        code, doc, _, _ = self.check(MON + " 11:00", self.RULES, src, json_mode=True)
        self.assertEqual(doc["fired"], [])
        self.assertEqual({p["id"] for p in doc["pending"]}, {"rel-stop", "rel-target"})
        self.assertEqual(doc["missing"], [])

    def test_dated_rule_only_watches_its_date(self):
        rules = [dict(self.RULES[0], date=MON)]
        src = FakeSource({KEY: bar(1183.65)})
        code, doc, _, _ = self.check("2026-10-27 10:00", rules, src, json_mode=True)
        self.assertEqual((doc["fired"], doc["pending"], doc["missing"]), ([], [], []))
        self.assertEqual(src.asked, [])  # not its day: not even quoted


class PlanIntegration(Base):
    def test_stop_target_and_flat_by_from_the_plan_file(self):
        src = FakeSource({KEY: bar(1203.55)})
        rules = alerts._plan_rules(plan(), None)
        code, doc, out, err = self.check(MON + " 15:11", rules, src, json_mode=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([f["id"] for f in doc["fired"]], ["RELIANCE-target", "RELIANCE-flat-by"])

    def test_dated_plan_rules_do_not_fire_on_another_day(self):
        rules = alerts._plan_rules(plan(), None)
        code, doc, _, _ = self.check("2026-10-27 15:11", rules, FakeSource({KEY: bar(1100.0)}),
                                     json_mode=True)
        self.assertEqual(doc["fired"], [])


class QuoteFailures(Base):
    RULES = [{"id": "rel-stop", "symbol": "RELIANCE", "instrument_key": KEY,
              "type": "stop", "price": 1183.65}]

    def test_missing_quote_is_reported_and_not_fired(self):
        src = FakeSource({})  # the batch answer omits RELIANCE
        code, doc, out, err = self.check(MON + " 10:00", self.RULES, src, json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        self.assertEqual(doc["missing"],
                         [{"id": "rel-stop", "symbol": "RELIANCE",
                           "detail": "no quote for RELIANCE"}])
        self.assertIn("no quote for RELIANCE", out)

    def test_auth_error_reports_reauth_guidance_and_exit_2(self):
        src = FakeSource(error=UpstoxAuthError("token expired", status=401))
        code, doc, out, err = self.check(MON + " 10:00", self.RULES, src, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(doc["fired"], [])
        self.assertEqual(doc["errors"], ["Upstox rejected the access token (token expired): "
                                         "re-authenticate with Upstox to refresh the token, "
                                         "then run the check again"])
        self.assertIn("re-authenticate", err)

    def test_transport_failure_is_reported_with_exit_2(self):
        src = FakeSource(error=alerts.UpstoxError("connection refused", status=None))
        code, doc, _, err = self.check(MON + " 10:00", self.RULES, src, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(doc["errors"], ["quote request failed: connection refused"])

    def test_no_price_rules_never_touches_the_source(self):
        src = FakeSource(error=UpstoxAuthError("nope"))
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"}]
        code, doc, _, err = self.check(MON + " 09:15", rules, src, json_mode=True)
        self.assertEqual((code, [f["id"] for f in doc["fired"]]), (0, ["bell-open"]))

    def test_check_without_a_source_builds_the_client(self):
        self.write_rules([{"id": "rel-stop", "symbol": "RELIANCE", "instrument_key": KEY,
                           "type": "stop", "price": 1183.65}])
        code, out, err = self.run_cli(["check", "--config", self.config, "--rules", self.rules,
                                       "--now", MON + " 10:00"])
        # the real Client has no reachable Upstox token in tests: reported, exit 2, not a crash
        self.assertEqual(code, 2)
        self.assertIn("re-authenticate", err)

    def test_missing_instrument_key_skips_the_quote(self):
        rules = [{"id": "rel-above", "symbol": "RELIANCE", "type": "above", "price": 1200.0}]
        src = FakeSource({KEY: bar(1201.0)})
        code, doc, _, err = self.check(MON + " 10:00", rules, src, json_mode=True)
        self.assertEqual(code, 2)
        self.assertEqual(src.asked, [])
        self.assertTrue(any("no instrument_key for RELIANCE" in e for e in doc["errors"]), doc)
        self.assertEqual(doc["fired"], [])
        self.assertEqual(doc["pending"], [])
        self.assertNotIn("NSE_EQ|RELIANCE", json.dumps(doc))


class FlatBy(Base):
    def test_flat_by_fires_once_then_stays_quiet(self):
        rules = [{"id": "flat", "type": "flat_by", "at": "15:10",
                  "label": "flat by close"}]
        code, doc, out, err = self.check(MON + " 15:09", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        self.assertEqual([p["id"] for p in doc["pending"]], ["flat"])
        code, doc, out, err = self.check(MON + " 15:10", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, [f["id"] for f in doc["fired"]]), (0, ["flat"]))
        self.assertIn("flat by close", out)
        code, doc, _, _ = self.check(MON + " 15:20", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, doc["fired"]), (0, []))
        code, doc, _, _ = self.check("2026-10-27 15:10", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, [f["id"] for f in doc["fired"]]), (0, ["flat"]))

    def test_flat_by_defaults_to_the_config_session(self):
        rules = [{"id": "flat", "type": "flat_by"}]
        code, doc, _, err = self.check(MON + " 15:10", rules, FakeSource(), json_mode=True)
        self.assertEqual((code, [f["id"] for f in doc["fired"]]), (0, ["flat"]))


class Output(Base):
    def test_json_has_the_pinned_shape(self):
        rules = [{"id": "bell-open", "type": "bell", "bell": "open"}]
        code, doc, out, err = self.check(MON + " 09:14", rules, FakeSource(), json_mode=True)
        self.assertEqual(set(doc), {"now", "fired", "pending", "missing", "errors"})
        self.assertTrue(doc["now"].endswith("IST"))
        self.assertEqual(doc["now"], MON + " 09:14 IST")

    def test_quiet_check_exits_0_with_no_rules(self):
        code, doc, out, err = self.check(MON + " 10:00", [], FakeSource(), json_mode=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((doc_empty(doc)), {"fired": [], "pending": [], "missing": [], "errors": []})

    def test_bad_args_exit_2(self):
        code, out, err = self.run_cli(["check", "--config", self.config,
                                   "--rules", os.path.join(self.tmp, "absent.json"),
                                   "--now", "not a time"])
        self.assertEqual(code, 2)
        self.assertIn("trader-alerts:", err)

    def test_bad_rule_type_is_an_error_not_a_crash(self):
        code, doc, _, err = self.check(MON + " 10:00",
                                       [{"id": "x", "type": "pct_move", "pct": 3}],
                                       FakeSource(), json_mode=True)
        self.assertEqual(code, 2)
        self.assertIn("unknown rule type", err)


def doc_empty(doc):
    return {k: doc[k] for k in ("fired", "pending", "missing", "errors")}


if __name__ == "__main__":
    unittest.main()
