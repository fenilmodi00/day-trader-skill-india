"""India tests for trader-budget: INR sizing, whole lots, tick snapping, plan contract."""

import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, core, load

core()  # repo root on sys.path before trader_core imports
from trader_core import config, orders  # noqa: E402
from trader_core.instruments import load as load_instruments  # noqa: E402

size = load("trader-budget", "size")
risk = load("trader-risk", "check")

DIR = os.path.join(FIX, "trader-budget", "india")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "NSE_EQ|INE002A01018"
NIFTY_KEY = "NSE_FO|9992600"
MON = "2026-10-26"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime(2026, 10, 26, 10, 0, tzinfo=IST)
REL = {"instrument_key": KEY, "trading_symbol": "RELIANCE", "lot_size": 1.0,
       "tick_size": 0.05, "freeze_quantity": 11111.0}
NIFTY = {"instrument_key": NIFTY_KEY, "trading_symbol": "NIFTY", "lot_size": 75.0,
         "tick_size": 0.05, "freeze_quantity": 1800.0}


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def base_cfg(**over):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["state_dir"] = tempfile.mkdtemp(prefix="dt-t7-budget-")
    cfg["account"].update(over)
    return cfg


def nifty_plan(**over):
    with open(os.path.join(DIR, "nifty-plan.json"), encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rel_plan(**over):
    with open(os.path.join(DIR, "reliance-plan.json"), encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rel_legs(entry=1193.6, stop=1183.65, target=1203.55, qty=25):
    """Plan doc without legs; sizing builds them. Prices off-grid on purpose
    (1193.6 is on the 0.05 grid; 1193.62 is not) so the snap shows."""
    return {"symbol": "RELIANCE", "instrument_key": KEY, "date": MON, "direction": "long",
            "entry": entry, "stop": stop, "target": target, "tick_size": 0.05,
            "lot_size": 1, "product": "I"}


def rel_legs_no_date():
    p = rel_legs()
    p.pop("date")
    return p


def size_nifty(cfg=None, **over):
    return size.size_plan(nifty_plan(), cfg or base_cfg(), NIFTY, today=MON, **over)


def size_rel(cfg=None, **over):
    return size.size_plan(rel_legs(**over), cfg or base_cfg(), REL, today=MON)


class NiftyLots(unittest.TestCase):
    """NIFTY: lot 75, tick 0.05, freeze 1800. Risk 50 per unit, 3750 per lot."""

    def test_sizes_in_whole_lots(self):
        s = size_nifty(max_loss=10000.0, budget=10000000.0)
        # 10000 / 3750 = 2.67 -> 2 whole lots = 150 units, risk 7500 <= 10000
        self.assertEqual(s["quantity"], 150)
        self.assertEqual(s["quantity"] % 75, 0)

    def test_budget_capped_sizing(self):
        s = size_nifty(max_loss=100000.0, budget=100000.0)
        # budget 100000 / (25150 x 75) = 0.053 -> 0 lots: too expensive for the budget
        self.assertEqual(s["quantity"], 0)
        self.assertTrue(s["refusal"])

    def test_max_loss_capped_sizing(self):
        s = size_nifty(max_loss=7600.0, budget=10000000.0)
        # 7600 / 3750 = 2.03 -> 2 whole lots = 150 units, risk 7500 <= 7600
        self.assertEqual(s["quantity"], 150)
        self.assertLessEqual(s["risk_inr"], 7600.0)

    def test_freeze_breach_rejected(self):
        # the sizing can never emit more than the freeze quantity: the plan
        # contract promises the risk check a quantity the exchange accepts
        for freeze in (1800.0, 1499.0, 75.0):
            s = size.size_plan(nifty_plan(), base_cfg(), dict(NIFTY, freeze_quantity=freeze),
                               today=MON, max_loss=100000.0, budget=100000000.0)
            self.assertLessEqual(s["quantity"], freeze)

    def _size_with(self, instrument, **kw):
        return size.size_plan(nifty_plan(), base_cfg(), instrument, today=MON, **kw)

    def test_freeze_caps_lots_not_rejects(self):
        # risk and budget want 25 lots = 1875 units; freeze 1800 caps at 24 lots = 1800
        s = self._size_with(dict(NIFTY, freeze_quantity=1800.0),
                            max_loss=100000.0, budget=100000000.0)
        self.assertEqual(s["quantity"], 1800)
        self.assertLessEqual(s["quantity"], 1800)

    def test_freeze_beyond_freeze_is_refused(self):
        # freeze 1499: 19 lots = 1425 fits, 20 lots = 1500 would breach; the
        # floor-to-freeze sizing lands under it, never over
        s = self._size_with(dict(NIFTY, freeze_quantity=1499.0),
                            max_loss=100000.0, budget=100000000.0)
        self.assertLessEqual(s["quantity"], 1499)


class RiskNeverRoundsUp(unittest.TestCase):
    def test_floor_by_loss(self):
        s = size_nifty(max_loss=7499.0, budget=10000000.0)
        self.assertEqual(s["quantity"], 75)  # 2 lots risk 7500 > 7499, so 1 lot
        self.assertLessEqual(s["risk_inr"], 7499.0)

    def test_floor_by_budget(self):
        s = size.size_plan(rel_legs(), base_cfg(), REL, today=MON,
                           budget=20000.0, max_loss=100000.0)
        # 20000 / 1193.6 = 16.7 -> 16 units, cost 19097.6 <= 20000
        self.assertEqual(s["quantity"], 16)
        self.assertLessEqual(s["cost_inr"], 20000.0)


class TickRounding(unittest.TestCase):
    def test_tick_005_snapping(self):
        s = size_rel(entry=1193.62, stop=1183.67, target=1203.57)
        # stop down, target up, entry down: risk 9.95 per unit stays, never widens
        self.assertEqual(s["entry"], 1193.6)
        self.assertEqual(s["stop"], 1183.65)
        self.assertEqual(s["target"], 1203.6)
        self.assertTrue(orders.on_grid(s["entry"], 0.05))
        self.assertTrue(orders.on_grid(s["stop"], 0.05))
        self.assertTrue(orders.on_grid(s["target"], 0.05))

    def test_tick_1(self):
        s = size.size_plan(rel_legs(entry=1193.7, stop=1183.4, target=1203.9),
                           base_cfg(), dict(REL, tick_size=1.0), today=MON)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1193.0, 1183.0, 1204.0))

    def test_tick_10(self):
        s = size.size_plan(rel_legs(entry=1197.0, stop=1181.0, target=1219.0),
                           base_cfg(), dict(REL, tick_size=10.0), today=MON)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1190.0, 1180.0, 1220.0))

    def test_minimum_risk_one_tick(self):
        s = size.size_plan(rel_legs(entry=1193.05, stop=1193.0, target=1203.0),
                           base_cfg(), REL, today=MON)
        # after snapping, stop 1193.0 >= entry 1193.05 would round entry to 1193.0:
        # equal, so refused rather than a zero-risk plan
        self.assertTrue(s.get("refusal") or s["risk_per_unit"] >= 0.05)


class PlanValidatesAsSet(unittest.TestCase):
    def test_plan_validates(self):
        cfg = base_cfg(max_loss_per_trade_inr=100000.0, budget_inr=100000000.0)
        for instrument, plan_fn in ((NIFTY, nifty_plan), (REL, rel_plan)):
            s = size.size_plan(plan_fn(), cfg, instrument, today=MON)
            reasons = orders.validate_plan(s, instrument, cfg)
            self.assertEqual(reasons, [], (instrument.get("trading_symbol"), reasons))

    def test_legs_are_v3_payloads(self):
        s = size_nifty(max_loss=8000.0, budget=2000000.0)
        self.assertEqual([leg_["role"] for leg_ in s["legs"]], ["entry", "stop", "target"])
        entry = s["legs"][0]
        self.assertEqual(entry["transaction_type"], "BUY")
        self.assertEqual(entry["order_type"], "LIMIT")
        self.assertEqual(entry["quantity"], s["quantity"])
        stop = s["legs"][1]
        self.assertEqual(stop["order_type"], "SL")
        self.assertEqual(stop["trigger_price"], s["stop"])


class PlanContract(unittest.TestCase):
    def test_pinned_fields_present(self):
        s = size_nifty(max_loss=7600.0)
        for key in ("legs", "risk_inr", "reward_risk", "instrument_key", "tick_size",
                    "lot_size", "product", "quantity", "symbol", "direction", "date"):
            self.assertIn(key, s, key)
        self.assertEqual(s["instrument_key"], NIFTY_KEY)
        self.assertEqual(s["product"], "I")
        self.assertEqual(s["direction"], "long")
        self.assertGreater(s["reward_risk"], 0)

    def test_no_usd_leftovers(self):
        doc = json.dumps(size_nifty())
        for word in ("usd", "USD", "pdt", "PDT", "alpaca", "Alpaca", "fractional",
                     "max_position_pct", "cash_buffer"):
            self.assertNotIn(word, doc)


class PositiveLimits(unittest.TestCase):
    """#2.3: a limit that is 0, negative, missing or not a number is a refusal,
    never "no limit"."""

    def test_a_limit_that_is_not_a_positive_number_is_refused(self):
        plan = rel_legs()
        for over in ({"budget": 0}, {"budget": -5}, {"budget": "fifty"}, {"budget": True},
                     {"max_loss": 0}, {"max_loss": -5}, {"max_loss": "fifty"}, {"max_loss": True},
                     {"budget": 0, "max_loss": 0}):
            with self.assertRaises(size.SizeError) as cm:
                size.size_plan(plan, base_cfg(), REL, today=MON, **over)
            self.assertNotIn("\n", str(cm.exception))
        # an explicit None param means "not passed": it falls back to the config,
        # so only a bad config value is a refusal
        for key, value in (("budget_inr", 0), ("budget_inr", -5), ("budget_inr", "fifty"),
                           ("budget_inr", True), ("budget_inr", None),
                           ("max_loss_per_trade_inr", 0), ("max_loss_per_trade_inr", -5),
                           ("max_loss_per_trade_inr", None), ("max_loss_per_trade_inr", True)):
            with self.assertRaises(size.SizeError) as cm:
                size.size_plan(plan, base_cfg(**{key: value}), REL, today=MON)
            self.assertIn("%s must be a positive number" % key, str(cm.exception))
        # a config whose limit never became a number says what it got
        with self.assertRaises(size.SizeError) as cm:
            size.size_plan(plan, base_cfg(budget_inr="fifty"), REL, today=MON)
        self.assertEqual(str(cm.exception), "budget_inr must be a positive number, got 'fifty'")

    def test_a_plan_without_a_date_is_refused(self):
        for p in (rel_legs_no_date(), rel_legs_no_date() | {"date": None}, rel_legs_no_date() | {"date": ""}):
            with self.assertRaises(size.SizeError) as cm:
                size.size_plan(p, base_cfg(), REL, today=MON)
            self.assertEqual(str(cm.exception), "the plan has no date: make it with trader-exit")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-t7-budget-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = size.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_plan(self, doc, name="plan.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def test_nifty_json_shape(self):
        path = self.write_plan(nifty_plan())
        inst = os.path.join(DIR, "instruments.json")
        code, out, err = self.run_cli(["--plan", path, "--instruments", inst, "--now", MON,
                                       "--max-loss", "8000", "--budget", "2000000", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        plan_ = doc["plan"]
        for key in ("legs", "risk_inr", "reward_risk", "instrument_key", "tick_size",
                    "lot_size", "product", "quantity"):
            self.assertIn(key, plan_, key)
        self.assertEqual(plan_["quantity"] % 75, 0)
        self.assertLessEqual(plan_["risk_inr"], 8000.0)

    def test_report_exit_code_1_on_refusal(self):
        path = self.write_plan(nifty_plan())
        inst = os.path.join(DIR, "instruments.json")
        code, out, _ = self.run_cli(["--plan", path, "--instruments", inst, "--now", MON,
                                     "--max-loss", "100", "--budget", "1000"])
        self.assertEqual(code, 1)
        self.assertIn("No order", out)

    def test_missing_instrument_is_an_error(self):
        path = self.write_plan(nifty_plan())
        code, out, err = self.run_cli(["--plan", path, "--now", MON])
        self.assertEqual(code, 2)
        self.assertIn("trader-budget", err)

    def test_config_example_loads(self):
        cfg = config.load(os.path.join(os.path.dirname(DIR), "..", "..", "trader-budget",
                                       "config.example.json"))
        self.assertEqual(cfg["mode"], "sandbox")
        self.assertEqual(cfg["broker"], "upstox")
        self.assertEqual(cfg["products"]["default"], "I")
        self.assertEqual(cfg["session"]["entry_cutoff"], "15:00")
        self.assertEqual(cfg["session"]["mis_square_off_cas"], "15:10")

    def test_bad_now_and_bad_mode_exit_2_one_line(self):
        path = self.write_plan(rel_legs())
        master = os.path.join(self.tmp, "master.json")
        with open(master, "w", encoding="utf-8") as fh:
            json.dump([dict(REL, tick_size=5.0)], fh)  # raw masters carry paise
        code, out, err = self.run_cli(["--plan", path, "--instruments", master,
                                       "--now", "not-a-time"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("trader-budget: "), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mode": "paper"}, fh)
        code, out, err = self.run_cli(["--plan", path, "--instruments", master, "--config", cfg])
        self.assertEqual(code, 2)
        self.assertIn("mode must be one of", err)
        self.assertNotIn("Traceback", err)


class WritePlan(unittest.TestCase):
    """--write saves the sized plan as the exact JSON trader-risk reads; without
    the flag nothing is written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-t7-budget-write-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inst = os.path.join(DIR, "instruments.json")

    def write_src(self, name="incoming.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rel_legs(), fh)
        return path

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(self.tmp)                       # the shared plan path is repo-relative
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = size.main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def test_write_saves_the_plan_risk_reads(self):
        code, text, err = self.run_cli(["--plan", self.write_src(), "--instruments", self.inst,
                                        "--now", MON, "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("Wrote trader/plan.json", text)
        saved_path = os.path.join(self.tmp, "trader", "plan.json")
        with open(saved_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        for key in ("symbol", "instrument_key", "direction", "product", "entry", "stop",
                    "target", "tick_size", "lot_size", "quantity", "legs"):
            self.assertIn(key, saved, key)
        self.assertEqual(saved["quantity"], 25)
        self.assertEqual([one["role"] for one in saved["legs"]], ["entry", "stop", "target"])
        res = risk.check(saved, base_cfg(), now=NOW, instrument=REL, record={})
        self.assertIn(res["verdict"], ("PASS", "BLOCK"))
        self.assertNotIn("missing", res["checks"]["plan"])
        self.assertNotIn("not measurable", res["checks"]["max_loss"])

    def test_no_write_without_the_flag(self):
        code, text, err = self.run_cli(["--plan", self.write_src(), "--instruments", self.inst,
                                        "--now", MON, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["plan"]["quantity"], 25)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "trader", "plan.json")))

    def test_a_refusal_with_write_removes_the_stale_proposal(self):
        out = os.path.join(self.tmp, "sized.json")
        removed = "Removed the stale proposal at %s" % out

        def good():
            code, _, err = self.run_cli(["--plan", self.write_src(), "--instruments", self.inst,
                                         "--now", MON, "--write", out])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(out))

        good()
        # a zero-share refusal (the max loss cannot cover one unit of risk)
        # without --write leaves yesterday's file alone
        bad = os.path.join(self.tmp, "bad-plan.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump(rel_legs(), fh)
        code, text, err = self.run_cli(["--plan", bad, "--instruments", self.inst,
                                        "--now", MON, "--max-loss", "5"])
        self.assertEqual(code, 1)
        self.assertNotIn("stale", text)
        self.assertNotIn("stale", err)
        self.assertTrue(os.path.exists(out))
        # the same refusal with --write: the file goes, and one line says so
        code, text, err = self.run_cli(["--plan", bad, "--instruments", self.inst,
                                        "--now", MON, "--max-loss", "5", "--write", out])
        self.assertEqual(code, 1)
        self.assertIn(removed, err)
        self.assertEqual(err.count("Removed the stale"), 1)
        self.assertFalse(os.path.exists(out))
        # a refusal raised as an error (the plan is for another day) removes it too
        good()
        late = self.write_src("late-plan.json")
        code, _, err = self.run_cli(["--plan", late, "--instruments", self.inst,
                                     "--now", "2026-10-27 10:00", "--write", out])
        self.assertEqual(code, 2)
        self.assertIn(removed, err)
        self.assertIn("the plan is for 2026-10-26", err)
        self.assertFalse(os.path.exists(out))
        # an undated plan: refused as an error, stale file gone
        good()
        undated = os.path.join(self.tmp, "undated.json")
        with open(undated, "w", encoding="utf-8") as fh:
            json.dump(rel_legs_no_date(), fh)
        code, _, err = self.run_cli(["--plan", undated, "--instruments", self.inst,
                                     "--now", MON, "--write", out])
        self.assertEqual(code, 2)
        self.assertIn("the plan has no date", err)
        self.assertFalse(os.path.exists(out))

    def test_an_unexpected_error_is_one_line(self):
        # a config file whose sections are not objects: one line, not a traceback
        out = os.path.join(self.tmp, "sized.json")

        def good():
            code, _, err = self.run_cli(["--plan", self.write_src(), "--instruments", self.inst,
                                         "--now", MON, "--write", out])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(out))

        good()
        bad_cfg = os.path.join(self.tmp, "bad.json")
        with open(bad_cfg, "w", encoding="utf-8") as fh:
            json.dump({"account": []}, fh)
        code, _, err = self.run_cli(["--plan", self.write_src(), "--instruments", self.inst,
                                     "--now", MON, "--config", bad_cfg, "--write", out])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("trader-budget: unexpected AttributeError"), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        # the failure is in config.load, before sizing: drop_stale is a SizeError
        # path and has nothing to say here (covered by the test above)


class ExampleConfigs(unittest.TestCase):
    """Both example configs show the schema config.DEFAULTS documents: the same
    keys in every section they show, state_dir and risk.mis_products included,
    and no dead delivery-only charge rate."""

    def examples(self):
        for folder in ("trader-budget", "trader-broker"):
            path = os.path.join(ROOT, folder, "config.example.json")
            with open(path, encoding="utf-8") as fh:
                yield folder, path, json.load(fh)

    def assert_same_keys(self, doc, base, where):
        self.assertEqual(set(doc), set(base), where)
        for key, want in base.items():
            if isinstance(want, dict):
                self.assertIsInstance(doc.get(key), dict, "%s.%s" % (where, key))
                self.assert_same_keys(doc[key], want, "%s.%s" % (where, key))

    def test_examples_match_the_defaults_schema(self):
        for folder, path, doc in self.examples():
            self.assert_same_keys(doc, config.DEFAULTS, folder)
            self.assertEqual(doc["state_dir"], config.DEFAULTS["state_dir"], folder)
            self.assertEqual(doc["risk"]["mis_products"], ["I"], folder)
            self.assertNotIn("stt_delivery_bps", doc["charges"], folder)
            self.assertEqual(config.load(path)["risk"]["mis_products"], ["I"], folder)


if __name__ == "__main__":
    unittest.main()
