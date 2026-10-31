"""India tests for trader-risk: INR limits, IST window, lots, tick, freeze, MIS, charges."""

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from _load import FIX, core, load

core()  # repo root on sys.path before trader_core imports
from trader_core import broker as broker_mod  # noqa: E402
from trader_core import charges, config, orders, upstox  # noqa: E402
from trader_core.timeutil import parse_ist  # noqa: E402

risk = load("trader-risk", "check")
budget_size = load("trader-budget", "size")

DIR = os.path.join(FIX, "trader-risk", "india")
KEY = "NSE_EQ|INE002A01018"
MON = "2026-10-26"  # a trading Monday in the seeded fixtures week
NOW = parse_ist("2026-10-26 10:00")  # inside 09:30-15:00
REL = {"instrument_key": KEY, "trading_symbol": "RELIANCE", "lot_size": 1.0,
       "tick_size": 0.05, "freeze_quantity": 11111.0}
NIFTY = {"instrument_key": "NSE_FO|9992600", "trading_symbol": "NIFTY", "lot_size": 75.0,
         "tick_size": 0.05, "freeze_quantity": 1800.0}


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def base_cfg(**over):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["state_dir"] = tempfile.mkdtemp(prefix="dt-t7-risk-")
    # gross loss 248.75, charges 45.53, net 294.28: the default clears the net
    cfg["account"]["max_loss_per_trade_inr"] = 300.0
    cfg["account"].update(over)
    return cfg


def rel_plan(**over):
    """A valid two-leg RELIANCE plan: entry 1193.6, stop 1183.65, target 1203.55, 25 units."""
    p = {
        "symbol": "RELIANCE", "instrument_key": KEY, "date": MON, "direction": "long",
        "product": "I", "quantity": 25, "lot_size": 1, "tick_size": 0.05,
        "legs": [
            {"role": "entry", "instrument_token": KEY, "transaction_type": "BUY", "quantity": 25,
             "order_type": "LIMIT", "product": "I", "validity": "DAY", "price": 1193.6,
             "tag": "dt-entry-orb"},
            {"role": "stop", "instrument_token": KEY, "transaction_type": "SELL", "quantity": 25,
             "order_type": "SL", "product": "I", "validity": "DAY", "trigger_price": 1183.65,
             "tag": "dt-stop-orb"},
            {"role": "target", "instrument_token": KEY, "transaction_type": "SELL", "quantity": 25,
             "order_type": "LIMIT", "product": "I", "validity": "DAY", "price": 1203.55,
             "tag": "dt-target-orb"},
        ],
    }
    p.update(over)
    return p


def nifty_plan(qty=75, **over):
    p = {
        "symbol": "NIFTY", "instrument_key": "NSE_FO|9992600", "date": MON, "direction": "long",
        "product": "I", "quantity": qty, "lot_size": 75, "tick_size": 0.05,
        "legs": [
            {"role": "entry", "instrument_token": "NSE_FO|9992600", "transaction_type": "BUY",
             "quantity": qty, "order_type": "LIMIT", "product": "I", "validity": "DAY",
             "price": 25150.0, "tag": "dt-entry-nifty"},
            {"role": "stop", "instrument_token": "NSE_FO|9992600", "transaction_type": "SELL",
             "quantity": qty, "order_type": "SL", "product": "I", "validity": "DAY",
             "trigger_price": 25100.0, "tag": "dt-stop-nifty"},
            {"role": "target", "instrument_token": "NSE_FO|9992600", "transaction_type": "SELL",
             "quantity": qty, "order_type": "LIMIT", "product": "I", "validity": "DAY",
             "price": 25250.0, "tag": "dt-target-nifty"},
        ],
    }
    p.update(over)
    return p


class FakeBroker:
    """The seam check() injects: funds(), margins(keys), orders() like UpstoxBroker."""

    def __init__(self, funds=None, margins=None, orders=None, error=None):
        self.funds_doc = funds
        self.margins_doc = margins or {}
        self.orders_doc = orders or []
        self.error = error
        self.asked = []

    def funds(self):
        if self.error:
            raise self.error
        return self.funds_doc

    def margins(self, keys):
        self.asked.extend(keys)
        if self.error:
            raise self.error
        return self.margins_doc

    def orders(self):
        return self.orders_doc


FUNDS = {"equity": {"available_margin": 50000.0}}
MARGINS = {"equity": {"instruments": [{"instrument_key": KEY, "total_margin": 29840.0}]}}


def verdict(res):
    return res["verdict"]


def reasons(res):
    return " | ".join(res["reasons"])


class FundsAnswered(FakeBroker):
    def __init__(self):
        FakeBroker.__init__(self, funds=FUNDS, margins=MARGINS)


class PassCase(unittest.TestCase):
    def test_valid_reliance_plan_passes(self):
        res = risk.check(rel_plan(), base_cfg(), now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))
        for key in ("verdict", "reasons", "checks", "plan"):
            self.assertIn(key, res)
        self.assertEqual(res["plan"]["symbol"], "RELIANCE")
        self.assertIn("max loss", res["checks"]["max_loss"])

    def test_missing_broker_seam_is_not_a_pass(self):
        res = risk.check(rel_plan(), base_cfg(), now=NOW, broker=None, instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))
        # the funds check is marked as not run: no silent pass in the checks dict
        self.assertIn("no broker seam", res["checks"]["funds"])


class PlanShape(unittest.TestCase):
    def test_missing_leg_blocks(self):
        plan_ = rel_plan()
        plan_["legs"] = [plan_["legs"][0], plan_["legs"][1]]
        res = risk.check(plan_, base_cfg(), now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("missing target leg", reasons(res))

    def test_short_direction_blocks_long_only(self):
        plan_ = rel_plan()
        plan_["direction"] = "short"
        res = risk.check(plan_, base_cfg(), now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("long only", reasons(res))


class BudgetOutput(unittest.TestCase):
    """The check reads trader-budget --json's {"sizing", "plan", "legs"} shape:
    quantity, symbol and product live under "plan", never at the top."""

    def budget_doc(self, max_loss=250.0):
        cfg = base_cfg()
        source = {"symbol": "RELIANCE", "date": MON, "entry": 1193.6, "stop": 1183.65,
                  "target": 1203.55, "setup": "orb"}
        sized = budget_size.size_plan(source, cfg, REL, max_loss=max_loss, today=MON)
        self.assertEqual(sized["quantity"], 25)
        return cfg, {"sizing": {k: v for k, v in sized.items() if k != "legs"},
                     "plan": sized, "legs": sized["legs"]}

    def test_sized_budget_output_reaches_a_verdict(self):
        cfg, doc = self.budget_doc()
        self.assertNotIn("quantity", doc)
        res = risk.check(doc, cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))
        self.assertIn("Rs 248.75", res["checks"]["max_loss"])
        self.assertIn("quantity 25", res["checks"]["lot_size"])

    def test_plan_with_no_quantity_blocks_instead_of_crashing(self):
        plan_ = rel_plan()
        del plan_["quantity"]
        for leg_ in plan_["legs"]:
            del leg_["quantity"]
        res = risk.check(plan_, base_cfg(), now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("no measurable max loss" in r for r in res["reasons"]), reasons(res))
        self.assertIn("no quantity", res["checks"]["lot_size"])


class MaxLoss(unittest.TestCase):
    def test_loss_over_cap_blocks(self):
        res = risk.check(rel_plan(), base_cfg(max_loss_per_trade_inr=100.0), now=NOW,
                         instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("max loss", reasons(res))

    def test_loss_at_cap_passes(self):
        res = risk.check(rel_plan(), base_cfg(max_loss_per_trade_inr=300.0), now=NOW,
                         instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class DailyLoss(unittest.TestCase):
    def test_daily_limit_reached_blocks(self):
        state = read("broker-state.json")
        res = risk.check(rel_plan(), base_cfg(max_daily_loss_inr=200.0), now=NOW, record=state,
                         instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("daily loss", reasons(res))

    def test_daily_loss_under_cap_passes(self):
        state = read("broker-state.json")
        plan_ = rel_plan()
        for leg_ in plan_["legs"]:
            leg_["tag"] = leg_["tag"] + "-new"
        res = risk.check(plan_, base_cfg(max_daily_loss_inr=500.0), now=NOW, record=state,
                         broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class TradesPerDay(unittest.TestCase):
    def test_trade_cap_reached_blocks(self):
        state = read("broker-state.json")
        res = risk.check(rel_plan(), base_cfg(max_trades_per_day=1), now=NOW, record=state,
                         instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("entries recorded today", reasons(res))

    def test_under_the_cap_passes(self):
        state = read("broker-state.json")
        plan_ = rel_plan()
        for leg_ in plan_["legs"]:
            leg_["tag"] = leg_["tag"] + "-x"
        res = risk.check(plan_, base_cfg(max_trades_per_day=3), now=NOW, record=state,
                         broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class EntryWindow(unittest.TestCase):
    def test_before_the_window_blocks(self):
        res = risk.check(rel_plan(), base_cfg(), now=parse_ist("2026-10-26 09:29"), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("outside the entry window", reasons(res))

    def test_after_the_cutoff_blocks(self):
        res = risk.check(rel_plan(), base_cfg(), now=parse_ist("2026-10-26 15:01"), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("outside the entry window", reasons(res))

    def test_configurable_window(self):
        cfg = base_cfg()
        cfg["session"]["entry_start"] = "10:00"
        cfg["session"]["entry_cutoff"] = "10:30"
        res = risk.check(rel_plan(), cfg, now=parse_ist("2026-10-26 09:45"), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("10:00 to 10:30", reasons(res))


class Freeze(unittest.TestCase):
    def test_freeze_breach_blocks(self):
        res = risk.check(nifty_plan(qty=1875), base_cfg(), now=NOW, instrument=NIFTY)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("freeze", reasons(res))

    def test_freeze_boundary_passes(self):
        # 1800 units x 50 = gross 90,000, charges 15,976.94, net 105,976.94
        cfg = base_cfg(max_loss_per_trade_inr=110000.0)
        broker = FakeBroker(funds={"equity": {"available_margin": 100000000.0}},
                            margins={"data": {"instruments": [
                                {"instrument_key": "NSE_FO|9992600", "total_margin": 100000.0}]}})
        res = risk.check(nifty_plan(qty=1800), cfg, now=NOW, broker=broker, instrument=NIFTY)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class LotSize(unittest.TestCase):
    def test_wrong_lot_blocks(self):
        res = risk.check(nifty_plan(qty=80), base_cfg(), now=NOW, instrument=NIFTY)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("whole multiple of lot_size 75", reasons(res))

    def test_wrong_lot_rejected_even_off_instrument(self):
        # plan says lot 75; without an instrument row the plan's own lot is used
        plan_ = nifty_plan(qty=80)
        res = risk.check(plan_, base_cfg(), now=NOW)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("whole multiple of lot_size 75", reasons(res))


class TickGrid(unittest.TestCase):
    def test_off_grid_price_blocks(self):
        plan_ = rel_plan()
        plan_["legs"][0]["price"] = 1193.61
        res = risk.check(plan_, base_cfg(), now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("tick grid", reasons(res))

    def test_tick_1_grid(self):
        plan_ = rel_plan()
        plan_["legs"][0]["price"] = 1193.5
        res = risk.check(plan_, base_cfg(), now=NOW,
                         instrument=dict(REL, tick_size=1.0))
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("1.0 tick grid", reasons(res))

    def test_tick_10_grid(self):
        cfg = base_cfg(max_loss_per_trade_inr=110000.0)
        broker = FakeBroker(funds={"equity": {"available_margin": 100000000.0}},
                            margins={"data": {"instruments": [
                                {"instrument_key": "NSE_FO|9992600", "total_margin": 100000.0}]}})
        plan_ = nifty_plan()
        plan_["legs"][0]["price"] = 25150.0
        res = risk.check(plan_, cfg, now=NOW, broker=broker,
                         instrument=dict(NIFTY, tick_size=10.0))
        self.assertEqual(verdict(res), "PASS", reasons(res))
        plan_["legs"][0]["price"] = 25155.0
        res = risk.check(plan_, cfg, now=NOW, broker=broker,
                         instrument=dict(NIFTY, tick_size=10.0))
        self.assertEqual(verdict(res), "BLOCK")


class MisEligibility(unittest.TestCase):
    def test_mis_listed_passes(self):
        cfg = base_cfg()
        cfg["risk"]["mis_products"] = ["NSE_MIS"]
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))

    def test_product_code_list_passes(self):
        # the documented default: product codes, I included
        cfg = base_cfg()
        cfg["risk"]["mis_products"] = ["I"]
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))

    def test_product_missing_from_the_list_blocks(self):
        cfg = base_cfg()
        cfg["risk"]["mis_products"] = ["D"]
        res = risk.check(rel_plan(), cfg, now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("not in your risk.mis_products", reasons(res))

    def test_unlisted_product_blocks(self):
        cfg = base_cfg()
        cfg["risk"]["mis_products"] = ["I"]
        plan_ = rel_plan(product="MTF")
        for leg_ in plan_["legs"]:
            leg_["product"] = "MTF"
        res = risk.check(plan_, cfg, now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("MTF", reasons(res))

    def test_product_d_blocks(self):
        plan_ = rel_plan(product="D")
        for leg_ in plan_["legs"]:
            leg_["product"] = "D"
        res = risk.check(plan_, base_cfg(), now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("not MIS", reasons(res))


class FundsAndMargins(unittest.TestCase):
    def test_missing_funds_answer_is_a_reason(self):
        res = risk.check(rel_plan(), base_cfg(), now=NOW, broker=FakeBroker(), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("funds" in r for r in res["reasons"]), reasons(res))

    def test_broker_error_is_a_reason(self):
        from trader_core.http import UpstoxAuthError
        broker = FakeBroker(error=UpstoxAuthError("token expired"))
        res = risk.check(rel_plan(), base_cfg(), now=NOW, broker=broker, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("funds" in r for r in res["reasons"]), reasons(res))

    def test_short_margin_blocks(self):
        broker = FakeBroker(funds={"equity": {"available_margin": 1000.0}}, margins=MARGINS)
        res = risk.check(rel_plan(), base_cfg(), now=NOW, broker=broker, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("available margin", reasons(res))

    def test_nifty_margin_answer_shapes(self):
        with open(os.path.join(DIR, "funds.json"), encoding="utf-8") as fh:
            funds = json.load(fh)
        funds["data"]["equity"]["available_margin"] = 100000000.0
        cfg = base_cfg(max_loss_per_trade_inr=110000.0)
        broker = FakeBroker(funds=funds,
                            margins={"data": {"instruments": [{"instrument_key": "NSE_FO|9992600",
                                                               "total_margin": 100000.0}]}})
        res = risk.check(nifty_plan(), cfg, now=NOW, broker=broker, instrument=NIFTY)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class Duplicate(unittest.TestCase):
    def test_same_tag_recorded_blocks(self):
        state = read("broker-state.json")
        res = risk.check(rel_plan(), base_cfg(), now=NOW, record=state, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("duplicate", reasons(res))

    def test_fresh_tag_passes(self):
        state = read("broker-state.json")
        plan_ = rel_plan()
        for leg_ in plan_["legs"]:
            leg_["tag"] = leg_["tag"] + "-2"
        res = risk.check(plan_, base_cfg(), now=NOW, record=state,
                         broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))


class ChargesAdjusted(unittest.TestCase):
    def test_charges_push_loss_over_cap(self):
        # gross loss 248.75 fits 250, but the net loss 294.28 (charges 45.53) does not
        cfg = base_cfg(max_loss_per_trade_inr=250.0)
        rt = charges.round_trip(1193.6, 1183.65, 25, cfg=cfg)
        self.assertLess(-rt["gross_pnl"], 250.0)
        self.assertGreater(-rt["net_pnl"], 250.0)
        res = risk.check(rel_plan(), cfg, now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertIn("charges-adjusted loss", reasons(res))

    def test_cap_above_net_passes(self):
        cfg = base_cfg(max_loss_per_trade_inr=300.0)
        res = risk.check(rel_plan(), cfg, now=NOW, instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))

    def test_charges_change_the_reported_max_loss(self):
        res = risk.check(rel_plan(), base_cfg(), now=NOW, instrument=REL)
        self.assertIn("charges", res["checks"]["charges"])
        self.assertIn("net loss", res["checks"]["charges"])


class ChargesModel(unittest.TestCase):
    def test_round_trip_total_is_pinned(self):
        # entry 1193.60, target 1203.55, qty 25: 45.94 across both sides intraday
        rt = charges.round_trip(1193.6, 1203.55, 25, cfg=config.DEFAULTS)
        self.assertEqual(rt["gross_pnl"], 248.75)
        self.assertEqual(rt["total_charges"], 45.94)
        self.assertEqual(rt["net_pnl"], 202.81)

    def test_delivery_stt_is_never_read(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["charges"]["stt_delivery_bps"] = 10000.0
        self.assertEqual(charges.charges("SELL", 1193.6, 25, cfg=cfg)["stt"], 7.46)
        self.assertEqual(charges.round_trip(1193.6, 1203.55, 25, cfg=cfg)["total_charges"], 45.94)


class WatchLists(unittest.TestCase):
    def test_asm_list_warns_but_does_not_block(self):
        cfg = base_cfg()
        cfg["risk"]["asm_symbols"] = ["RELIANCE"]
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))
        self.assertTrue(any("user-supplied" in w for w in res["warnings"]), res["warnings"])

    def test_ban_list_wording(self):
        cfg = base_cfg()
        cfg["risk"]["ban_symbols"] = ["RELIANCE"]
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "PASS", reasons(res))
        self.assertIn("ban", " ".join(res["warnings"]))

    def test_blocked_symbol_blocks_naming_the_list(self):
        cfg = base_cfg()
        cfg["risk"]["blocked_symbols"] = ["RELIANCE"]
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("blocked_symbols" in r and "RELIANCE" in r for r in res["reasons"]),
                        reasons(res))


class Closure(unittest.TestCase):
    """A weekend or NSE holiday blocks even inside the clock window."""

    def test_saturday_blocks_with_the_closure_date(self):
        res = risk.check(rel_plan(), base_cfg(), now=parse_ist("2026-10-31 10:00"),
                         broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("2026-10-31" in r and "weekend" in r for r in res["reasons"]),
                        reasons(res))

    def test_nse_holiday_blocks_with_the_name_and_date(self):
        # Dussehra, from trader_core/data/nse_holidays.json
        res = risk.check(rel_plan(), base_cfg(), now=parse_ist("2026-10-20 10:00"),
                         broker=FundsAnswered(), instrument=REL)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("2026-10-20" in r and "Dussehra" in r for r in res["reasons"]),
                        reasons(res))


class OrdersPerDay(unittest.TestCase):
    def test_order_cap_reached_blocks(self):
        cfg = base_cfg()
        cfg["account"]["max_orders_per_day"] = 2
        record = {"orders": [
            {"date": MON, "status": "placed", "kind": "entry", "side": "BUY", "symbol": "RELIANCE"},
            {"date": MON, "status": "approved", "kind": "flatten", "side": "SELL",
             "symbol": "RELIANCE"},
        ], "trades": []}
        res = risk.check(rel_plan(), cfg, now=NOW, broker=FundsAnswered(), instrument=REL,
                         record=record)
        self.assertEqual(verdict(res), "BLOCK")
        self.assertTrue(any("orders recorded today" in r for r in res["reasons"]), reasons(res))
        self.assertEqual(res["checks"]["orders_today"], "2 of 2")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-t7-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plan_path = os.path.join(self.tmp, "plan.json")
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(rel_plan(), fh)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["account"]["max_loss_per_trade_inr"] = 300.0
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def run_cli(self, argv, broker=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = risk.main(argv + ["--config", self.cfg_path], source=broker)
        return code, out.getvalue(), err.getvalue()

    def test_json_contract_at_top_level(self):
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--json", "--no-write"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        for key in ("verdict", "reasons", "plan"):
            self.assertIn(key, doc)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("no broker seam", doc["checks"]["funds"])

    def test_writes_state_outside_repo(self):
        out_path = os.path.join(self.tmp, "state", "risk.json")
        code, out, _ = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                     "--json", "--out", out_path])
        self.assertIn(code, (0, 1))
        with open(out_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIn(doc["verdict"], ("PASS", "BLOCK"))

    def test_missing_plan_is_an_error_not_a_crash(self):
        code, out, err = self.run_cli(["--plan", os.path.join(self.tmp, "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("trader-risk", err)

    def test_broker_seam_flips_to_pass(self):
        broker = FakeBroker(funds=FUNDS, margins=MARGINS)
        code, out, _ = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                     "--json", "--no-write"], broker=broker)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])

    def test_broker_flag_passes_the_config_through_the_gate(self):
        # UpstoxBroker without cfg reads mode None and its funds() gate raises:
        # every --broker funds and margin check would become a reason (a BLOCK)
        seen = []

        def transport(method, url, payload=None, headers=None, timeout=30):
            seen.append((method, url))
            if url.endswith(broker_mod.FUNDS_PATH):
                return 200, json.dumps({"status": "success",
                                        "data": {"equity": {"available_margin": 50000.0}}})
            if url.endswith(broker_mod.MARGINS_PATH):
                # the risk check reads a flat envelope data.instruments list; a
                # real Upstox envelope nests the list under data.equity (unparsed bug)
                return 200, json.dumps({"status": "success", "data": {"instruments": [
                    {"instrument_key": KEY, "total_margin": 29840.0}]}})
            return 404, ""

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(upstox, "default_transport", transport), \
                mock.patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "sandbox-token"}):
            with redirect_stdout(out), redirect_stderr(err):
                code = risk.main(["--plan", self.plan_path, "--config", self.cfg_path,
                                  "--broker", "--now", "2026-10-26 10:00", "--json",
                                  "--no-write"])
        doc = json.loads(out.getvalue())
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("available margin", doc["checks"]["funds"])
        self.assertIn("Rs 50,000", doc["checks"]["funds"])
        self.assertIn("required margin", doc["checks"]["margins"])
        self.assertIn("Rs 29,840", doc["checks"]["margins"])
        self.assertFalse(any("mode must be one of" in r for r in doc["reasons"]), doc["reasons"])
        self.assertEqual([method for method, _ in seen], ["GET", "POST"])

    def test_block_outside_window(self):
        code, out, _ = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 15:01",
                                     "--json", "--no-write"], broker=FakeBroker(funds=FUNDS,
                                                                                margins=MARGINS))
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "BLOCK")

    def funds_fixture(self, available):
        path = os.path.join(self.tmp, "funds.json")
        doc = {"funds": {"equity": {"available_margin": available}},
               "margins": {"equity": {"instruments": [
                   {"instrument_key": KEY, "total_margin": 29840.0}]}}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def test_sufficient_funds_fixture_passes(self):
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--funds", self.funds_fixture(50000.0),
                                       "--json", "--no-write"])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("available margin", doc["checks"]["funds"])
        self.assertIn("required margin", doc["checks"]["margins"])

    def test_insufficient_funds_fixture_blocks(self):
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--funds", self.funds_fixture(100.0),
                                       "--json", "--no-write"])
        self.assertEqual(code, 1, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "BLOCK")
        self.assertTrue(any("available margin" in r and "under the entry cost" in r
                            for r in doc["reasons"]), doc["reasons"])

    def test_funds_only_fixture_reports_margins_not_supplied(self):
        # the recorded funds envelope carries no margins segment: say so, never invent one
        path = os.path.join(FIX, "trader-risk", "india", "funds.json")
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--funds", path, "--json", "--no-write"])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("not supplied by this seam", doc["checks"]["margins"])

    def test_no_broker_flag_keeps_the_not_checked_line(self):
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--json", "--no-write"])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertIn("no broker seam: not checked here", doc["checks"]["funds"])
        self.assertIn("no broker seam: not checked here", doc["checks"]["margins"])

    def test_gzipped_instrument_master_loads(self):
        master = os.path.join(FIX, "upstox", "instruments-NSE.json.gz")
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--instruments", master, "--json", "--no-write"])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("lot 1", doc["checks"]["lot_size"])

    def test_plain_instrument_master_loads(self):
        master = os.path.join(self.tmp, "master.json")
        with open(master, "w", encoding="utf-8") as fh:
            json.dump([dict(REL, tick_size=5.0)], fh)  # raw masters carry paise
        code, out, err = self.run_cli(["--plan", self.plan_path, "--now", "2026-10-26 10:00",
                                       "--instruments", master, "--json", "--no-write"])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"], "PASS", doc["reasons"])
        self.assertIn("lot 1", doc["checks"]["lot_size"])


if __name__ == "__main__":
    unittest.main()
