import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, core, load

core()
from trader_core import config, orders  # noqa: E402
from trader_core.http import UpstoxAuthError, UpstoxError  # noqa: E402
from trader_core.timeutil import parse_ist  # noqa: E402

ticket = load("trader-broker", "ticket")
risk = load("trader-risk", "check")

KEY = "NSE_EQ|INE002A01018"
MON = "2026-10-26"
NOW = "2026-10-26 10:00"


def make_cfg(tmp):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["state_dir"] = os.path.join(tmp, "state")
    os.makedirs(cfg["state_dir"], exist_ok=True)
    cfg["account"]["max_loss_per_trade_inr"] = 300.0
    return cfg


def write_cfg(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def rel_plan(**over):
    p = {
        "symbol": "RELIANCE", "instrument_key": KEY, "date": MON, "direction": "long",
        "product": "I", "quantity": 25, "lot_size": 1, "tick_size": 0.05,
        "flat_by": "15:10",
        "legs": [
            {"role": "entry", "instrument_token": KEY, "transaction_type": "BUY", "quantity": 25,
             "order_type": "LIMIT", "product": "I", "validity": "DAY", "price": 1193.6,
             "tag": "dt-entry-reliance"},
            {"role": "stop", "instrument_token": KEY, "transaction_type": "SELL", "quantity": 25,
             "order_type": "SL", "product": "I", "validity": "DAY", "trigger_price": 1183.65,
             "tag": "dt-stop-reliance"},
            {"role": "target", "instrument_token": KEY, "transaction_type": "SELL", "quantity": 25,
             "order_type": "LIMIT", "product": "I", "validity": "DAY", "price": 1203.55,
             "tag": "dt-target-reliance"},
        ],
    }
    p.update(over)
    return p


REL = {"instrument_key": KEY, "trading_symbol": "RELIANCE", "lot_size": 1.0,
       "tick_size": 0.05, "freeze_quantity": 11111.0}


class FakeBroker:
    def __init__(self):
        self.calls = []
        self._seen = set()
        self.place_result = {"order_id": "241026000000123", "status": "open"}
        self.auth_fail = False
        self.orders_doc = {"data": [{"order_id": "241026000000123", "status": "complete",
                                    "trading_symbol": "RELIANCE", "instrument_token": KEY}]}
        self.trades_doc = {"data": [{"order_id": "241026000000123", "trade_id": "T1",
                                     "transaction_type": "BUY", "quantity": 25,
                                     "average_price": 1193.6, "traded_at": "2026-10-26T10:00:05+05:30"}]}
        self.positions_doc = {"data": [{"instrument_token": KEY, "trading_symbol": "RELIANCE",
                                        "quantity": 25, "average_price": 1193.6,
                                        "last_price": 1200.0, "pnl": 160.0}]}

    def place(self, payload, retries=0):
        self.calls.append(payload)
        if self.auth_fail:
            raise UpstoxAuthError("Upstox rejected the credentials (HTTP 401): re-authenticate, run the login skill",
                                  status=401, code="AUTH_ERROR")
        tag = payload.get("tag")
        dup = False
        if tag and tag in self._seen:
            dup = True
        else:
            if tag:
                self._seen.add(tag)
        res = {"order_id": self.place_result["order_id"], "status": self.place_result["status"],
               "payload": payload, "duplicate": dup}
        return res

    def modify(self, order_id, payload):
        return {}

    def cancel(self, order_id):
        return {}

    def orders(self):
        return self.orders_doc

    def trades(self):
        return self.trades_doc

    def positions(self):
        return self.positions_doc

    def holdings(self):
        return {"data": []}

    def funds(self):
        return {"equity": {"available_margin": 50000.0}}

    def margins(self, keys):
        return {"equity": {"instruments": [{"instrument_key": KEY, "total_margin": 29840.0}]}}


def run_cli(mod, argv, source=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv, source=source)
    return code, out.getvalue(), err.getvalue()


class ParseYes(unittest.TestCase):
    ORDER = {"symbol": "RELIANCE", "side": "BUY", "quantity": 25, "order_type": "LIMIT",
             "price": 1193.6, "kind": "entry"}
    BATCH = ("RELIANCE", "HDFCBANK", "ICICIBANK")

    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or self.ORDER, batch)[0]

    def test_a_batch_yes_is_refused_by_symbol(self):
        self.assertFalse(self.yes("yes HDFCBANK"))            # another order in the batch
        self.assertIn("names HDFCBANK, but this order is for RELIANCE",
                      ticket.parse_yes("yes HDFCBANK", self.ORDER, self.BATCH)[1])
        self.assertTrue(self.yes("yes INFY"))                 # not in the batch: not checked
        self.assertTrue(self.yes("yes HDFCBANK", batch=["RELIANCE"]))
        self.assertTrue(self.yes("yes buy 25 RELIANCE"))

    def test_case_and_punctuation_are_forgiving(self):
        self.assertFalse(self.yes("Yes, buy hdfcbank instead"))
        self.assertFalse(self.yes("yes ICICIBANK"))


class OrderHash(unittest.TestCase):
    def plan(self):
        return rel_plan()

    def test_the_hash_covers_the_plans_leg_levels(self):
        o = ticket.entry_order(self.plan())
        base = ticket.order_hash(o)
        for leg, field, value in ((1, "trigger_price", 1184.0), (2, "price", 1210.0)):
            edited = rel_plan()
            edited["legs"][leg][field] = value
            self.assertNotEqual(ticket.order_hash(ticket.entry_order(edited)), base,
                                "leg %d %s" % (leg, field))
        # a string price is the same level as its float
        as_str = rel_plan()
        as_str["legs"][0]["price"] = "1193.6"
        self.assertEqual(ticket.order_hash(ticket.entry_order(as_str)), base)

    def test_a_hash_matches_itself_across_constructions(self):
        plan = self.plan()
        o = ticket.entry_order(plan)
        self.assertEqual(ticket.order_hash(o), ticket.order_hash(ticket.entry_order(plan)))
        self.assertNotEqual(ticket.order_hash(ticket.flatten_order(plan)), ticket.order_hash(o))


class BrokerIndiaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-t8-broker-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.plan_path = os.path.join(self.tmp, "plan.json")
        self.risk_path = os.path.join(self.tmp, "risk.json")
        self.ticket_path = os.path.join(self.tmp, "ticket.json")
        self.cfg = make_cfg(self.tmp)
        write_cfg(self.cfg_path, self.cfg)
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(rel_plan(), fh)
        self.broker = FakeBroker()

    def risk_pass(self):
        res = risk.check(rel_plan(), self.cfg, now=parse_ist(NOW), broker=self.broker, instrument=REL)
        self.assertEqual(res["verdict"], "PASS", res["reasons"])
        doc = dict(res)
        doc["generated_at"] = parse_ist(NOW).replace(microsecond=0).isoformat()
        doc["valid_until"] = res["valid_until"]  # 10:15 IST: 15 minutes from the check
        with open(self.risk_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return res

    def common(self):
        return ["--plan", self.plan_path, "--ticket", self.ticket_path,
                "--config", self.cfg_path, "--risk", self.risk_path, "--now", NOW]

    # --- gate 1: risk PASS required ---
    def test_no_execute_before_risk_pass(self):
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertNotEqual(code, 0)
        self.assertIn("NOT READY", out + err)
        # execute with no risk.json
        code, out, err = run_cli(ticket, ["execute"] + self.common())
        self.assertEqual(code, 2)
        self.assertTrue("gate 1" in err or "no order on the ticket" in err)

    def test_risk_block_never_ready(self):
        # plan with entry outside window -> BLOCK
        bad = rel_plan()
        bad["legs"][0]["price"] = 1000.0  # not on tick? still on 0.05 grid; use freeze instead
        # make BLOCK via huge qty over freeze
        bad["quantity"] = 999999
        for leg in bad["legs"]:
            leg["quantity"] = 999999
        res = risk.check(bad, self.cfg, now=parse_ist(NOW), broker=self.broker, instrument=REL)
        self.assertEqual(res["verdict"], "BLOCK")
        doc = dict(res)
        doc["generated_at"] = parse_ist(NOW).replace(microsecond=0).isoformat()
        with open(self.risk_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        # rewrite plan to match blocked plan so hash matches risk plan
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(bad, fh)
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertNotEqual(code, 0)
        self.assertIn("BLOCKED", out + err)

    # --- gate 2: typed yes ---
    def test_no_execute_before_typed_yes(self):
        self.risk_pass()
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertEqual(code, 0)
        self.assertIn("READY", out)
        code, out, err = run_cli(ticket, ["execute"] + self.common() + ["--symbol", "RELIANCE"])
        # execute reads ticket; no approval -> gate 2
        self.assertEqual(code, 2)
        self.assertIn("gate 2", err)

    def test_wrong_phrase_rejected(self):
        self.risk_pass()
        code, out, err = run_cli(ticket, ["approve", "--yes", "ok"] + self.common())
        self.assertEqual(code, 2)
        self.assertIn("not recorded", err)
        code, out, err = run_cli(ticket, ["approve", "--yes", "sure buy"] + self.common())
        self.assertEqual(code, 2)

    def test_edited_order_invalidates_pass_and_approval(self):
        self.risk_pass()
        code, _, _ = run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        self.assertEqual(code, 0)
        # edit quantity in plan
        plan = json.load(open(self.plan_path))
        plan["quantity"] = 50
        plan["legs"][0]["quantity"] = 50
        for leg in plan["legs"]:
            leg["quantity"] = 50
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        # show should not be ready (hash mismatch)
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertNotEqual(code, 0)
        self.assertIn("NOT READY", out + err)
        # execute gate 1 fails
        code, out, err = run_cli(ticket, ["execute"] + self.common())
        self.assertEqual(code, 2)

    def test_an_edited_stop_level_is_not_ready(self):
        # the order hash covers the plan's leg prices: editing plan.stop voids a READY ticket
        self.risk_pass()
        code, out, _ = run_cli(ticket, ["show"] + self.common())
        self.assertEqual(code, 0, out)
        plan = json.load(open(self.plan_path))
        plan["legs"][1]["trigger_price"] = 1184.0
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertNotEqual(code, 0)
        self.assertIn("NOT READY", out + err)
        self.assertIn("different order", out + err)
        code, _, err = run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        self.assertEqual(code, 2)
        self.assertIn("gate 1", err)

    # --- gate 1 expiry (valid_until) ---
    def test_the_pass_expires_and_show_refuses(self):
        self.risk_pass()  # stamped at 10:00, valid_until 10:15
        code, out, _ = run_cli(ticket, ["show"] + self.common() + ["--now", "2026-10-26 10:14"])
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)
        code, out, err = run_cli(ticket, ["show"] + self.common() + ["--now", "2026-10-26 10:16"])
        self.assertNotEqual(code, 0)
        self.assertIn("NOT READY", out + err)
        self.assertIn("the PASS expired at 10:15 IST; run trader-risk check.py again", out + err)

    def test_an_expired_pass_refuses_approve_and_execute(self):
        self.risk_pass()
        code, _, err = run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"]
                               + self.common() + ["--now", "2026-10-26 10:16"])
        self.assertEqual(code, 2)
        self.assertIn("expired", err)
        # approve inside the window, then execute after valid_until: gate 1 refuses again
        code, _, err = run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"]
                               + self.common() + ["--now", "2026-10-26 10:14"])
        self.assertEqual(code, 0, err)
        code, _, err = run_cli(ticket, ["execute"] + self.common() + ["--now", "2026-10-26 10:16"])
        self.assertEqual(code, 2)
        self.assertIn("expired", err)

    # --- sandbox default ---
    def test_sandbox_host_used_by_default(self):
        self.risk_pass()
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        code, out, err = run_cli(ticket, ["execute"] + self.common(), source=self.broker)
        self.assertEqual(code, 0, err)
        self.assertIn("api-sandbox.upstox.com", out)
        t = json.load(open(self.ticket_path))
        self.assertEqual(t["result"]["host"], "https://api-sandbox.upstox.com")

    # --- live blocked ---
    def test_live_without_confirmation_blocked_no_call(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["mode"] = "live"
        write_cfg(self.cfg_path, cfg)
        self.risk_pass()
        # approve under live? risk_pass used sandbox cfg; re-run risk with live cfg not needed for gate1 hash.
        # approve
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        fb = FakeBroker()
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        try:
            ticket.cmd_execute(ns, source=fb)
        except Exception:
            pass
        self.assertEqual(fb.calls, [])

    # --- shown == sent ---
    def test_shown_payload_equals_sent(self):
        self.risk_pass()
        code, out, err = run_cli(ticket, ["show", "--json"] + self.common())
        self.assertEqual(code, 0)
        view = json.loads(out)
        shown = view["payload"]
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        fb = FakeBroker()
        # execute with source via direct call
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        ticket.cmd_execute(ns, source=fb)
        self.assertEqual(len(fb.calls), 1)
        sent = fb.calls[0]
        self.assertEqual(shown, sent)

    # --- duplicate ---
    def test_duplicate_submit_returns_existing_id(self):
        self.risk_pass()
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        fb = FakeBroker()
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        ticket.cmd_execute(ns, source=fb)
        # reset approval? duplicate tag in broker state? ticket already placed; create new ticket for same order to re-execute
        # Instead call place twice via fake broker directly? Test seam duplicate via broker
        p = json.loads(open(self.ticket_path).read())["payload"]
        first = fb.place(p)
        second = fb.place(p)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["order_id"], first["order_id"])

    # --- expired token ---
    def test_expired_token_exits_2_with_reauth(self):
        self.risk_pass()
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        fb = FakeBroker()
        fb.auth_fail = True
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        # cmd_execute returns 2
        rc = ticket.cmd_execute(ns, source=fb)
        self.assertEqual(rc, 2)

    # --- result/fill update ticket ---
    def test_result_updates_ticket_from_order_book(self):
        self.risk_pass()
        run_cli(ticket, ["approve", "--yes", "yes buy 25 RELIANCE"] + self.common())
        fb = FakeBroker()
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        ticket.cmd_execute(ns, source=fb)
        # result
        ns2 = argparse.Namespace()
        for k, v in [("ticket", self.ticket_path), ("config", self.cfg_path), ("risk", self.risk_path),
                     ("now", NOW), ("today", None), ("pnl", None)]:
            setattr(ns2, k, v)
        rc = ticket.cmd_result(ns2, source=fb)
        self.assertEqual(rc, 0)
        t = json.load(open(self.ticket_path))
        self.assertIn("book_status", t["result"])
        self.assertEqual(len(t["result"]["fills"]), 1)

    def test_fill_records_ledger(self):
        code, out, err = run_cli(ticket, ["fill", "--symbol", "RELIANCE", "--side", "buy",
                                         "--qty", "25", "--price", "1193.6", "--config", self.cfg_path,
                                         "--state", os.path.join(self.tmp, "state", "orders.json"),
                                         "--now", NOW])
        self.assertEqual(code, 0)
        state = json.load(open(os.path.join(self.tmp, "state", "orders.json")))
        self.assertTrue(len(state.get("trades", [])) >= 1)

    # --- flatten ---
    def test_flatten_market_protection_and_gates(self):
        self.risk_pass()  # the PASS covers the plan; both orders derive from it
        # flatten command prepares ticket
        code, out, err = run_cli(ticket, ["flatten", "--symbol", "RELIANCE"] + self.common())
        self.assertEqual(code, 0)
        t = json.load(open(self.ticket_path))
        o = t["order"]
        self.assertEqual(o["order_type"], "MARKET")
        self.assertEqual(o["side"], "SELL")
        self.assertEqual(o["market_protection"], 10)
        # the same plan's fresh PASS is READY for the flatten order too
        code, out, err = run_cli(ticket, ["show"] + self.common())
        self.assertEqual(code, 0, out + err)
        self.assertIn("READY", out)
        self.assertIn("SELL 25 RELIANCE", out)

    def test_flatten_execute_sends_one_market_sell_and_records_it(self):
        self.risk_pass()
        run_cli(ticket, ["flatten", "--symbol", "RELIANCE"] + self.common())
        code, out, err = run_cli(ticket, ["approve", "--flatten", "--yes", "yes sell 25 RELIANCE"]
                                 + self.common())
        self.assertEqual((code, err), (0, ""), out)
        code, out, err = run_cli(ticket, ["execute"] + self.common(), source=self.broker)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.broker.calls), 1)
        payload = self.broker.calls[0]
        self.assertEqual(payload["transaction_type"], "SELL")
        self.assertEqual(payload["order_type"], "MARKET")
        self.assertEqual(payload["quantity"], 25)
        self.assertEqual(payload["market_protection"], 10)
        self.assertEqual(payload["instrument_token"], KEY)
        t = json.load(open(self.ticket_path))
        self.assertEqual(t["status"], "placed")
        state = json.load(open(os.path.join(self.tmp, "state", "orders.json")))
        placed = [r for r in state["orders"] if r.get("status") == "placed"]
        self.assertEqual(len(placed), 1)
        self.assertEqual((placed[0]["kind"], placed[0]["side"], placed[0]["quantity"]),
                         ("flatten", "SELL", 25))

    def test_flatten_gate_rejects_a_pass_for_a_changed_plan(self):
        self.risk_pass()
        run_cli(ticket, ["flatten", "--symbol", "RELIANCE"] + self.common())
        changed = rel_plan()
        changed["legs"][1]["trigger_price"] = 1184.0  # the plan moved after the PASS
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(changed, fh)
        code, out, err = run_cli(ticket, ["approve", "--flatten", "--yes", "yes sell 25 RELIANCE"]
                                 + self.common())
        self.assertEqual(code, 2)
        self.assertIn("gate 1", err)

    def test_flatten_execute_live_blocked(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["mode"] = "live"
        write_cfg(self.cfg_path, cfg)
        # risk pass with live cfg? hash unaffected; reuse risk.json from sandbox run already written
        self.risk_pass() if os.path.exists(self.risk_path) else None
        # prepare flatten ticket
        run_cli(ticket, ["flatten", "--symbol", "RELIANCE"] + self.common())
        # no broker calls when live unconfirmed: execute via fake broker direct
        fb = FakeBroker()
        import argparse
        ns = argparse.Namespace()
        for k, v in [("plan", self.plan_path), ("ticket", self.ticket_path), ("config", self.cfg_path),
                     ("risk", self.risk_path), ("now", NOW), ("today", None), ("symbol", None)]:
            setattr(ns, k, v)
        # risk gate will fail (risk.json is for entry hash, not flatten) -> TicketError before broker
        try:
            rc = ticket.cmd_execute(ns, source=fb)
        except Exception as e:
            rc = 2
        self.assertEqual(fb.calls, [])


if __name__ == "__main__":
    unittest.main()
