"""T3: Upstox broker seam, order plans, and the INR charge model, all offline.

The fake transport captures every (method, url, payload, headers) so the tests
can assert the exact JSON Upstox would have received: same field names, no
extra or renamed keys, sandbox host in sandbox mode, and no request at all when
a client-side gate rejects the order.
"""

import copy
import datetime
import json
import os
import shutil
import tempfile
import unittest

from _load import FIX, core

core()  # put the repo root on sys.path
from trader_core import broker as broker_mod  # noqa: E402
from trader_core import charges, config, orders  # noqa: E402
from trader_core.http import SANDBOX, UpstoxAuthError, UpstoxError  # noqa: E402
from trader_core.upstox import Client  # noqa: E402

FX = os.path.join(FIX, "upstox")
KEY = "NSE_EQ|INE002A01018"
REL = "NSE_EQ|RELIANCE"
INSTRUMENT = {
    "trading_symbol": "RELIANCE",
    "instrument_key": KEY,
    "lot_size": 1,
    "tick_size": 0.05,
    "freeze_quantity": 5000,
}


def read_fixture():
    with open(os.path.join(FX, "orders.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


class Transport:
    """Fake transport: records every call, answers from the recorded fixture."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, url, payload=None, headers=None, timeout=30):
        self.calls.append({"method": method, "url": url, "payload": payload,
                           "headers": headers or {}, "timeout": timeout})
        for needle, body in self.responses.items():
            if needle in url:
                return 200, json.dumps(body)
        return 404, json.dumps({"status": "error", "errors": {"errorCode": "TEST_404",
                                                              "message": "no fixture for " + url}})

    def bodies(self, method=None, path=None):
        out = []
        for call in self.calls:
            if method and call["method"] != method:
                continue
            if path and path not in call["url"]:
                continue
            out.append(call["payload"])
        return out


class TempStateCase(unittest.TestCase):
    """Config with a temp state dir, so no test ever writes under trader/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-t3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["state_dir"] = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg["state_dir"], exist_ok=True)


class LegPayloadTest(unittest.TestCase):
    def test_limit_payload_field_for_field(self):
        leg = orders.leg(KEY, "BUY", 25, "LIMIT", product="I", price=1193.6, tag="dt-entry")
        expected = {
            "quantity": 25,
            "product": "I",
            "validity": "DAY",
            "instrument_token": KEY,
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "disclosed_quantity": 0,
            "is_amo": False,
            "slice": False,
            "price": 1193.6,
            "tag": "dt-entry",
        }
        self.assertEqual(set(leg), set(expected))
        for name, value in expected.items():
            self.assertEqual(leg[name], value, name)
        self.assertNotIn("market_protection", leg)
        self.assertNotIn("trigger_price", leg)
        self.assertNotIn("symbol", leg)

    def test_market_with_market_protection_minus_one_sends_string(self):
        leg = orders.leg(KEY, "BUY", 25, "MARKET", market_protection=-1)
        self.assertEqual(leg["market_protection"], "-1")
        self.assertNotIn("price", leg)
        self.assertNotIn("trigger_price", leg)

    def test_market_with_market_protection_zero_rejected(self):
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "MARKET", market_protection=0)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "SELL", 25, "SL-M", market_protection=0)

    def test_market_protection_bounds(self):
        for value in (1, 25):
            leg = orders.leg(KEY, "BUY", 25, "MARKET", market_protection=value)
            self.assertEqual(leg["market_protection"], value)
        for value in (26, 0, -2):
            with self.assertRaises(orders.PlanError):
                orders.leg(KEY, "BUY", 25, "MARKET", market_protection=value)

    def test_sl_requires_trigger_price(self):
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "SELL", 25, "SL", price=1183.65)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "SELL", 25, "SL-M")
        leg = orders.leg(KEY, "SELL", 25, "SL", price=1183.65, trigger_price=1183.9)
        self.assertEqual(leg["trigger_price"], 1183.9)
        self.assertNotIn("market_protection", leg)

    def test_limit_requires_positive_price(self):
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "LIMIT", price=0)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "LIMIT", price=-1)

    def test_enum_and_quantity_validation(self):
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "BRACKET")
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "LIMIT", price=100.0, product="BRACKET")
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "LIMIT", price=100.0, validity="GTC")
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "HOLD", 25, "LIMIT", price=100.0)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 0, "LIMIT", price=100.0)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25.0, "LIMIT", price=100.0)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", True, "LIMIT", price=100.0)

    def test_tag_length(self):
        orders.leg(KEY, "BUY", 25, "LIMIT", price=100.0, tag="x" * 40)
        with self.assertRaises(orders.PlanError):
            orders.leg(KEY, "BUY", 25, "LIMIT", price=100.0, tag="x" * 41)


class RoundTickTest(unittest.TestCase):
    def test_grid_snapping(self):
        self.assertEqual(orders.round_tick(1193.62, 0.05), 1193.6)
        self.assertEqual(orders.round_tick(1193.63, 0.05), 1193.65)
        self.assertEqual(orders.round_tick(1193.65, 0.05), 1193.65)
        self.assertEqual(orders.round_tick(1192.63, 1), 1193.0)
        self.assertEqual(orders.round_tick(1183.651, 10), 1180.0)
        self.assertEqual(orders.round_tick(1187.4, 10), 1190.0)

    def test_up_down_modes(self):
        self.assertEqual(orders.round_tick(1193.62, 0.05, "down"), 1193.6)
        self.assertEqual(orders.round_tick(1193.62, 0.05, "up"), 1193.65)
        self.assertEqual(orders.round_tick(1193.65, 0.05, "down"), 1193.65)
        with self.assertRaises(orders.PlanError):
            orders.round_tick(100.0, 0.05, "sideways")

    def test_result_is_on_grid(self):
        for price in (1193.615, 1175.3, 1203.55, 1162.0):
            value = orders.round_tick(price, 0.05)
            self.assertTrue(orders.on_grid(value, 0.05), value)


class PlanTest(unittest.TestCase):
    @staticmethod
    def good_plan():
        entry = orders.leg(KEY, "BUY", 25, "LIMIT", product="I", price=1193.6, tag="dt-entry")
        stop = orders.leg(KEY, "SELL", 25, "SL", product="I", price=1183.65, trigger_price=1183.9)
        target = orders.leg(KEY, "SELL", 25, "LIMIT", product="I", price=1203.55, tag="dt-target")
        plan = orders.plan(entry, stop, target, symbol="RELIANCE")
        return plan, entry

    def test_plan_shape(self):
        plan, entry = self.good_plan()
        self.assertEqual(plan["symbol"], "RELIANCE")
        self.assertEqual(plan["instrument_key"], KEY)
        self.assertEqual(plan["product"], "I")
        self.assertEqual(plan["direction"], "long")
        self.assertEqual(plan["legs"][0]["role"], "entry")
        self.assertEqual(entry.get("role"), None)  # leg() payloads stay role-free
        self.assertEqual([leg["role"] for leg in plan["legs"]], ["entry", "stop", "target"])
        self.assertEqual(plan["quantity"], 25)

    def test_valid_plan_has_no_reasons(self):
        plan, _ = self.good_plan()
        self.assertEqual(orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS), [])

    def test_missing_leg(self):
        plan = orders.plan(orders.leg(KEY, "BUY", 25, "LIMIT", price=1193.6), None,
                           orders.leg(KEY, "SELL", 25, "LIMIT", price=1203.55))
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("missing stop leg" in r for r in reasons), reasons)

    def test_duplicate_role(self):
        entry = orders.leg(KEY, "BUY", 25, "LIMIT", price=1193.6)
        stop = orders.leg(KEY, "SELL", 25, "LIMIT", price=1183.65)
        plan, _ = self.good_plan()
        plan["legs"][1]["role"] = "entry"
        plan["legs"].append(dict(stop, role="entry"))
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("duplicate entry leg" in r for r in reasons), reasons)

    def test_wrong_side(self):
        plan, _ = self.good_plan()
        plan["legs"][1]["transaction_type"] = "BUY"
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("wrong side: stop leg must be SELL" in r for r in reasons), reasons)

    def test_lot_and_freeze_breaches(self):
        plan, _ = self.good_plan()
        for leg in plan["legs"]:
            leg["quantity"] = 6010
        instrument = dict(INSTRUMENT, lot_size=25)
        reasons = orders.validate_plan(plan, instrument, config.DEFAULTS)
        self.assertTrue(any("not a whole multiple of lot_size" in r for r in reasons), reasons)
        self.assertEqual(len([r for r in reasons if "freeze quantity" in r]), 3, reasons)

    def test_lot_multiple(self):
        instrument = dict(INSTRUMENT, lot_size=25)
        plan, _ = self.good_plan()
        self.assertEqual(orders.validate_plan(plan, instrument, config.DEFAULTS), [])
        for leg in plan["legs"]:
            leg["quantity"] = 30
        reasons = orders.validate_plan(plan, instrument, config.DEFAULTS)
        self.assertEqual(len([r for r in reasons if "lot_size" in r]), 3, reasons)

    def test_tick_breach(self):
        plan, _ = self.good_plan()
        plan["legs"][2]["price"] = 1203.57
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("not on the 0.05 tick grid" in r for r in reasons), reasons)

    def test_stop_and_target_must_bracket_entry(self):
        plan, _ = self.good_plan()
        plan["legs"][1]["trigger_price"] = 1193.6
        plan["legs"][1]["price"] = 1193.6
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("not equal to it" in r for r in reasons), reasons)
        plan2, _ = self.good_plan()
        plan2["legs"][1]["trigger_price"] = 1195.0
        plan2["legs"][1]["price"] = 1195.0
        reasons = orders.validate_plan(plan2, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("at or below the entry" in r for r in reasons), reasons)
        plan3, _ = self.good_plan()
        plan3["legs"][2]["price"] = 1190.0
        reasons = orders.validate_plan(plan3, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("target 1190.0 must be above the entry" in r for r in reasons), reasons)

    def test_mis_restriction(self):
        plan, _ = self.good_plan()
        plan["product"] = "D"
        for leg in plan["legs"]:
            leg["product"] = "D"
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("restricts trading to MIS" in r for r in reasons), reasons)
        permissive = copy.deepcopy(config.DEFAULTS)
        permissive["products"]["default"] = "D"
        self.assertEqual(orders.validate_plan(plan, INSTRUMENT, permissive), [])

    def test_long_only_blocks_short(self):
        sell = orders.leg(KEY, "SELL", 25, "LIMIT", price=1193.6)
        buy_stop = orders.leg(KEY, "BUY", 25, "SL", price=1203.55, trigger_price=1193.9)
        buy_target = orders.leg(KEY, "BUY", 25, "LIMIT", price=1183.55)
        plan = orders.plan(sell, buy_stop, buy_target)
        reasons = orders.validate_plan(plan, INSTRUMENT, config.DEFAULTS)
        self.assertTrue(any("long only" in r for r in reasons), reasons)


class ChargesTest(unittest.TestCase):
    def test_sell_side_stt_and_buy_side_stamp(self):
        buy = charges.charges("BUY", 1193.6, 25, cfg=config.DEFAULTS)
        sell = charges.charges("SELL", 1193.6, 25, cfg=config.DEFAULTS)
        self.assertEqual(buy["stt"], 0.0)
        self.assertEqual(sell["stamp"], 0.0)
        self.assertGreater(sell["stt"], 0.0)
        self.assertGreater(buy["stamp"], 0.0)
        for head in ("brokerage", "exchange", "sebi", "gst", "total"):
            self.assertGreater(buy[head], 0.0, head)

    def test_rates_come_from_config(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["charges"]["stt_intraday_sell_bps"] = 0.0
        sell = charges.charges("SELL", 1193.6, 25, cfg=cfg)
        self.assertEqual(sell["stt"], 0.0)
        cfg["charges"]["brokerage_bps"] = 0.0
        cfg["charges"]["brokerage_per_order_inr"] = 7.0
        cheap = charges.charges("SELL", 1193.6, 25, cfg=cfg)
        self.assertEqual(cheap["brokerage"], 0.0)  # bps part is 0, below the cap
        big = charges.round_trip(1.0, 1.0, 500, cfg=cfg)  # 500 * 1.0 turnover
        self.assertLessEqual(big["buy"]["brokerage"], 7.0)

    def test_brokerage_floored_at_zero_and_capped(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["charges"]["brokerage_bps"] = -1.0
        self.assertEqual(charges.charges("BUY", 100.0, 1, cfg=cfg)["brokerage"], 0.0)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["charges"]["brokerage_bps"] = 5000.0
        self.assertEqual(charges.charges("BUY", 100.0, 1, cfg=cfg)["brokerage"],
                         cfg["charges"]["brokerage_per_order_inr"])

    def test_round_trip_total_and_net(self):
        out = charges.round_trip(1193.6, 1203.55, 25, cfg=config.DEFAULTS)
        self.assertEqual(out["gross_pnl"], 248.75)
        expected_total = round(out["buy"]["total"] + out["sell"]["total"], 2)
        self.assertEqual(out["total_charges"], expected_total)
        self.assertEqual(out["net_pnl"], round(out["gross_pnl"] - out["total_charges"], 2))
        self.assertLess(out["net_pnl"], out["gross_pnl"])

    def test_charges_change_reported_pnl(self):
        out = charges.round_trip(1193.6, 1203.55, 25, cfg=config.DEFAULTS)
        free = copy.deepcopy(config.DEFAULTS)
        for key in free["charges"]:
            free["charges"][key] = 0.0
        zero = charges.round_trip(1193.6, 1203.55, 25, cfg=free)
        self.assertEqual(zero["total_charges"], 0.0)
        self.assertEqual(zero["net_pnl"], 248.75)
        self.assertGreater(out["total_charges"], 0.0)


class BrokerTest(TempStateCase):
    def setUp(self):
        TempStateCase.setUp(self)
        self._clock = mock_now(broker_mod, datetime.datetime(
            2026, 10, 26, 10, 0, tzinfo=core().IST))
        self._clock.__enter__()
        self.fx = read_fixture()
        self.transport = Transport({
            "/v3/order/place": self.fx["place"],
            "/v2/order/modify": self.fx["modify"],
            "/v2/order/cancel": self.fx["cancel"],
            "/v2/order/retrieve-all": self.fx["orders"],
            "/v2/order/trades/get-trades-for-day": self.fx["trades"],
            "/v2/portfolio/short-term-positions": self.fx["positions"],
            "/v2/portfolio/long-term-holdings": self.fx["holdings"],
            "/v2/user/get-funds-and-margin": self.fx["funds"],
            "/v2/charges/margin": self.fx["margins"],
        })
        self.client = Client(cfg=self.cfg, transport=self.transport, token="test-token")
        self.broker = broker_mod.UpstoxBroker(client=self.client, cfg=self.cfg)

    def tearDown(self):
        self._clock.__exit__()

    def place_payload(self, **over):
        payload = orders.leg(KEY, "BUY", 25, "LIMIT", product="I", price=1193.6,
                             tag="dt-entry-reliance")
        payload.update(over)
        return payload

    def test_place_sends_validated_payload_to_sandbox(self):
        result = self.broker.place(self.place_payload())
        self.assertEqual(len(self.transport.calls), 1)
        call = self.transport.calls[0]
        self.assertTrue(call["url"].startswith(SANDBOX + "/v3/order/place"), call["url"])
        self.assertEqual(call["method"], "POST")
        sent = call["payload"]
        expected = {
            "quantity": 25,
            "product": "I",
            "validity": "DAY",
            "instrument_token": KEY,
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "disclosed_quantity": 0,
            "is_amo": False,
            "slice": False,
            "price": 1193.6,
            "tag": "dt-entry-reliance",
        }
        self.assertEqual(set(sent), set(expected))
        for name, value in expected.items():
            self.assertEqual(sent[name], value, name)
        self.assertEqual(result["order_id"], "241026000000123")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["payload"], sent)

    def test_place_rejects_bad_payload_before_any_request(self):
        with self.assertRaises(orders.PlanError):
            self.broker.place(self.place_payload(order_type="MARKET", price=None,
                                                 market_protection=0))
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(orders.PlanError):
            self.broker.place({"quantity": 25, "order_type": "BRACKET"})
        self.assertEqual(self.transport.calls, [])

    def test_duplicate_tag_returns_first_order_without_second_call(self):
        first = self.broker.place(self.place_payload())
        second = self.broker.place(self.place_payload())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(second["duplicate"])
        self.assertFalse(first["duplicate"])
        self.assertEqual(second["order_id"], first["order_id"])
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(second["payload"], first["payload"])

    def test_live_host_blocked_without_confirmation(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["mode"] = "live"
        cfg["state_dir"] = self.cfg["state_dir"]
        live_broker = broker_mod.UpstoxBroker(
            client=Client(cfg=cfg, transport=self.transport, token="t"), cfg=cfg)
        with self.assertRaises(config.LiveGateError):
            live_broker.place(self.place_payload())
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(config.LiveGateError):
            live_broker.orders()
        self.assertEqual(self.transport.calls, [])

    def test_sandbox_mode_does_not_hit_live_gate(self):
        result = self.broker.place(self.place_payload())
        self.assertFalse(result["duplicate"])

    def test_open_window_guard(self):
        early = datetime.datetime(2026, 10, 26, 3, 0, tzinfo=core().IST)
        late = datetime.datetime(2026, 10, 26, 5, 30, tzinfo=core().IST)
        with self.assertRaises(UpstoxError) as caught:
            broker_mod.assert_open_window(early)
        self.assertIn("05:30 to 24:00 IST", str(caught.exception))
        self.assertEqual(self.transport.calls, [])
        broker_mod.assert_open_window(late)  # boundary is inclusive
        broker_mod.assert_open_window(datetime.datetime(2026, 10, 26, 23, 59, tzinfo=core().IST))
        with mock_now(broker_mod, early):
            with self.assertRaises(UpstoxError):
                self.broker.place(self.place_payload())
        self.assertEqual(self.transport.calls, [])
        with mock_now(broker_mod, late):
            self.broker.place(self.place_payload())
        self.assertEqual(len(self.transport.calls), 1)

    def test_token_expiry_surfaces_auth_error_without_retry(self):
        calls = []

        class Expired:
            def __call__(self, method, url, payload=None, headers=None, timeout=30):
                calls.append({"method": method, "url": url, "payload": payload})
                return 401, json.dumps({"status": "error", "errors": {"errorCode": "AUTH_ERROR",
                                                                      "message": "token expired"}})

        broker = broker_mod.UpstoxBroker(
            client=Client(cfg=self.cfg, transport=Expired(), token="stale"), cfg=self.cfg)
        with self.assertRaises(UpstoxAuthError) as caught:
            broker.place(self.place_payload())
        self.assertIn("re-authenticate", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_reads_wrap_documented_paths(self):
        self.assertEqual(self.broker.orders(), self.fx["orders"]["data"])
        self.assertEqual(self.broker.trades(), self.fx["trades"]["data"])
        self.assertEqual(self.broker.positions(), self.fx["positions"]["data"])
        self.assertEqual(self.broker.holdings(), self.fx["holdings"]["data"])
        self.assertEqual(self.broker.funds(), self.fx["funds"])
        self.assertEqual(self.broker.margins([KEY]), self.fx["margins"])
        paths = [call["url"].split("upstox.com", 1)[1] for call in self.transport.calls]
        for expected in ("/v2/order/retrieve-all", "/v2/order/trades/get-trades-for-day",
                         "/v2/portfolio/short-term-positions", "/v2/portfolio/long-term-holdings",
                         "/v2/user/get-funds-and-margin", "/v2/charges/margin"):
            self.assertTrue(any(p.startswith(expected) for p in paths), paths)
        margins_call = [c for c in self.transport.calls if "/v2/charges/margin" in c["url"]][0]
        self.assertEqual(margins_call["method"], "POST")
        self.assertEqual(margins_call["payload"], {"instruments": [{"instrument_key": KEY}]})

    def test_modify_and_cancel(self):
        modified = self.broker.modify("241026000000123", {"price": 1194.0, "quantity": 25})
        self.assertEqual(modified["order_id"], "241026000000123")
        sent = self.transport.bodies("PUT", "/v2/order/modify")[0]
        self.assertEqual(sent["order_id"], "241026000000123")
        self.assertEqual(sent["price"], 1194.0)
        cancelled = self.broker.cancel("241026000000123")
        self.assertEqual(cancelled["order_status"], "cancelled")
        self.assertTrue(self.transport.calls[-1]["url"].endswith("/v2/order/cancel/241026000000123"))
        self.assertEqual(self.transport.calls[-1]["method"], "DELETE")

    def test_state_stays_out_of_the_repo(self):
        self.broker.place(self.place_payload())
        with open(os.path.join(self.cfg["state_dir"], "orders.json"), "r", encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["dt-entry-reliance"]["order_id"], "241026000000123")


class mock_now:
    """Pin the broker module clock so the open-window test stays offline."""

    def __init__(self, module, dt):
        self.module = module
        self.dt = dt

    def __enter__(self):
        self._saved = self.module.now_ist
        self.module.now_ist = lambda: self.dt
        return self

    def __exit__(self, *exc):
        self.module.now_ist = self._saved
        return False


if __name__ == "__main__":
    unittest.main()
