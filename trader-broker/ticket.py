#!/usr/bin/env python3
"""Order tickets and the three gates for trader-broker. Standard library only.

Upstox is the only broker and sandbox is the default. No order goes out
unless all three gates hold, enforced here in code:

  gate 1, risk    the exact order has a fresh PASS from trader-risk check.py
                  for today, matched by order hash (symbol, instrument_key,
                  side, quantity, entry, stop, target, tag). Edit anything and
                  the old PASS and the old approval are both void.
  gate 2, you     your typed yes, recorded word for word by approve, bound to
                  the same order hash. No approval, no execute.
  gate 3, host    the host tool card (Allow once / Deny) Claude shows before
                  the execute call. Documented, never automated here.

  show      render the ticket from trader/plan.json plus the exact Upstox v3
            payload that will be sent. --json prints it as JSON.
  approve   record the user's explicit yes for this one order (the reply word
            for word) and write the ticket with the payload. Refuses ok, sure,
            a batch yes, a doubtful yes, and anything without a fresh PASS.
  execute   place the approved order through the trader_core broker seam.
            Sandbox host by default; live needs the stored confirmation from
            trader_core.config.confirm_live. Records order id, status, fills
            and positions on the ticket and in trader/state/orders.json.
  result    read the order book, the trades and the positions through the
            same seam and update the ticket; a completed exit records its P&L.
  fill      record a fill by hand (same ledger), with an optional pnl.
  flatten   prepare the opposite MARKET order (with market_protection) that
            closes the plan's position by flat_by. It obeys the same three
            gates: risk PASS for the flatten order, then your typed yes.
  log       print today's decisions and fills from the ledger.

    python3 ticket.py show
    python3 ticket.py show --flatten --json
    python3 ticket.py approve --yes "yes, buy 25 RELIANCE"
    python3 ticket.py execute
    python3 ticket.py result
    python3 ticket.py flatten
    python3 ticket.py log

The ticket lives at trader/ticket.json (or --ticket PATH). The ledger lives
in trader/state/orders.json, the same file trader-risk reads. Dates are IST
dates; --now "2026-10-26 10:00" pins the clock. Education and tooling, not
financial advice.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import config, orders  # noqa: E402
from trader_core.broker import UpstoxBroker  # noqa: E402
from trader_core.http import LIVE, SANDBOX, UpstoxAuthError, UpstoxError  # noqa: E402
from trader_core.timeutil import now_ist, parse_ist  # noqa: E402

SKILL = "ticket.py"
PLAN_PATH = "trader/plan.json"
TICKET_PATH = "trader/ticket.json"
RISK_PATH = "trader/state/risk.json"
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
# The order fields the hash binds: any edit here voids the PASS and the approval.
TICKET_FIELDS = ("symbol", "instrument_key", "side", "quantity", "entry", "stop", "target", "tag",
                 "product", "validity", "order_type", "price", "trigger_price", "market_protection",
                 "date")
FLAT_PROTECTION = 10  # percent of market protection on the flatten MARKET order


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _rows(answer):
    """Order book, trades or positions from the seam: a list, or an envelope
    with a data list."""
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict) and isinstance(answer.get("data"), list):
        return answer["data"]
    return []


# ---------------------------------------------------------------- the order and its hash

def _legs_by_role(plan):
    by = {}
    for leg in (plan or {}).get("legs") or []:
        if isinstance(leg, dict) and leg.get("role"):
            by.setdefault(leg["role"], leg)
    missing = [r for r in ("entry", "stop", "target") if r not in by]
    if missing:
        raise TicketError("the plan has no %s leg: trader-risk checks a two-leg plan, "
                          "so run trader-budget first" % ", ".join(missing))
    return by


def _leg_price(leg):
    """The price a leg acts at: the trigger for a stop leg, else the limit."""
    if leg.get("trigger_price") is not None:
        return leg["trigger_price"]
    return leg.get("price")


def entry_order(plan):
    """The entry order the plan's entry leg places, as the gates bind it."""
    legs = _legs_by_role(plan)
    entry = legs["entry"]
    qty = entry.get("quantity", plan.get("quantity"))
    return {"kind": "entry", "symbol": plan.get("symbol"),
            "instrument_key": entry.get("instrument_token") or plan.get("instrument_key"),
            "side": entry.get("transaction_type", "BUY"), "quantity": qty,
            "product": plan.get("product", "I"), "order_type": entry.get("order_type", "LIMIT"),
            "price": entry.get("price"), "trigger_price": entry.get("trigger_price"),
            "validity": entry.get("validity", "DAY"), "tag": entry.get("tag"),
            "entry": _leg_price(entry), "stop": _leg_price(legs["stop"]),
            "target": _leg_price(legs["target"]), "date": plan.get("date"),
            "flat_by": plan.get("flat_by")}


def flatten_order(plan, cfg=None, qty=None):
    """The opposite MARKET order that closes the plan's position, with
    market_protection set. The stop and target stay on the ticket for the
    record; the flatten order itself is one MARKET leg."""
    legs = _legs_by_role(plan)
    entry = legs["entry"]
    sym = str(plan.get("symbol") or "").upper()
    side = "SELL" if str(entry.get("transaction_type", "BUY")).upper() == "BUY" else "BUY"
    flat_by = plan.get("flat_by")
    if not flat_by and cfg is not None:
        flat_by = config.session_times(cfg)["flat_by"].strftime("%H:%M")
    return {"kind": "flatten", "symbol": plan.get("symbol"),
            "instrument_key": entry.get("instrument_token") or plan.get("instrument_key"),
            "side": side, "quantity": qty if qty is not None else entry.get("quantity",
                                                                            plan.get("quantity")),
            "product": plan.get("product", "I"), "order_type": "MARKET", "price": None,
            "trigger_price": None, "validity": "DAY",
            "market_protection": FLAT_PROTECTION, "tag": "dt-flat-" + sym.lower(),
            "entry": _leg_price(entry), "stop": _leg_price(legs["stop"]),
            "target": _leg_price(legs["target"]), "date": plan.get("date"), "flat_by": flat_by}


def order_core(o):
    out = {}
    for k in TICKET_FIELDS:
        if o.get(k) is not None:
            out[k] = o[k]
    return out


def order_hash(o):
    """The exact order as one hash: the same number trader-risk's PASS is
    bound to. An edited quantity or price changes it and voids everything."""
    blob = json.dumps(order_core(o), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def payload_for(o):
    """The exact Upstox v3 place body for a ticket order, built and validated
    by trader_core.orders.leg. Shown and sent are the same dict."""
    return orders.leg(o.get("instrument_key"), o.get("side"), int(o.get("quantity")),
                      o.get("order_type", "LIMIT"), product=o.get("product", "I"),
                      price=o.get("price"), trigger_price=o.get("trigger_price"),
                      market_protection=o.get("market_protection") or 0, tag=o.get("tag"),
                      validity=o.get("validity", "DAY"))


def _money(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "%s" % (v,)
    return "Rs " + "{:,.2f}".format(f)


def plain(o):
    """The ticket in plain words."""
    parts = ["%s %s %s" % (o.get("side"), o.get("quantity"), o.get("symbol"))]
    ot = str(o.get("order_type", "LIMIT")).upper()
    if ot == "MARKET":
        parts.append("market order, %s%% market protection" % (o.get("market_protection") or 0))
    elif o.get("price") is not None:
        parts.append("limit %s" % _money(o["price"]))
    elif o.get("trigger_price") is not None:
        parts.append("stop, trigger %s" % _money(o["trigger_price"]))
    parts.append("product %s" % o.get("product", "I"))
    if o.get("kind") != "flatten":
        parts.append("stop %s" % _money(o.get("stop")))
        parts.append("target %s" % _money(o.get("target")))
    return ", ".join(parts)


def amount_numbers(o):
    nums = []
    for k in ("quantity", "price", "entry", "stop", "target", "trigger_price"):
        try:
            nums.append(float(o[k]))
        except (TypeError, ValueError, KeyError):
            pass
    return nums


def parse_yes(reply, order):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why).
    The user's chat reply is recorded word for word; ok, sure, a batch yes and
    a doubtful yes all count as no."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, never a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join("%g" % n for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- gates

def load_plan(path, symbol=None):
    plan = read_json(path, "plan (run trader-budget, then trader-risk check.py, first)")
    if symbol and str(plan.get("symbol", "")).upper() != symbol.upper():
        raise TicketError("the plan at %s is for %s, not %s" % (path, plan.get("symbol"), symbol))
    return plan


def risk_record(risk, today):
    """The verdict as the gates read it: today's only, with the plan attached."""
    if not isinstance(risk, dict) or risk.get("verdict") not in ("PASS", "BLOCK"):
        return None
    day = str(risk.get("now", ""))[:10] or str(risk.get("generated_at", ""))[:10]
    if day != today:
        return None
    plan = risk.get("plan")
    if not isinstance(plan, dict) or not plan:
        return None
    return {"verdict": risk["verdict"], "reasons": risk.get("reasons") or [], "plan": plan}


def order_from_plan(plan, kind):
    return flatten_order(plan) if kind == "flatten" else entry_order(plan)


def risk_gate(o, risk, today):
    """Gate 1: a fresh trader-risk PASS for this exact order. Raises."""
    rec = risk_record(risk, today)
    if rec is None:
        raise TicketError("gate 1 (risk): no trader-risk PASS for today (%s); run "
                          "trader-risk check.py for this exact order first" % today)
    if rec["verdict"] != "PASS":
        raise TicketError("gate 1 (risk): trader-risk BLOCKED this plan: %s"
                          % "; ".join(rec["reasons"][:3]))
    if order_hash(order_from_plan(rec["plan"], o.get("kind"))) != order_hash(o):
        raise TicketError("gate 1 (risk): the PASS on file is for a different order (the plan "
                          "changed since the check); run trader-risk check.py again")
    return rec


def approval_gate(o, ticket, today):
    """Gate 2: your typed yes, recorded today, bound to this order hash. Raises."""
    rec = ticket.get("approval")
    if not isinstance(rec, dict) or rec.get("date") != today:
        raise TicketError("gate 2 (approval): no typed yes recorded today; run approve --yes first, "
                          "then execute")
    if rec.get("order_hash") != order_hash(o):
        raise TicketError("gate 2 (approval): the yes on record is for a different order (the "
                          "order changed since you approved); approve again")
    return rec


# ---------------------------------------------------------------- ticket and ledger

def _read_ticket(path):
    doc = read_json(path, "ticket", required=False)
    return doc if isinstance(doc, dict) else {}


def ticket_view(o, ticket, risk, today):
    """(ready, status, risk note) for show, without raising: a BLOCK is shown,
    not hidden."""
    fp = order_hash(o)
    view_kind = o.get("kind")
    rec = risk_record(risk, today)
    if rec is None:
        ready, status = False, "NOT READY: no fresh trader-risk PASS for today; run check.py"
    elif rec["verdict"] != "PASS":
        ready = False
        status = "BLOCKED by trader-risk: " + "; ".join(rec["reasons"][:3])
    else:
        if order_hash(order_from_plan(rec["plan"], view_kind)) != fp:
            ready, status = False, ("NOT READY: the PASS is for a different order (the plan changed); "
                                    "run check.py again")
        else:
            ready, status = True, "READY: trader-risk PASS, waiting for your typed yes"
    appr = ticket.get("approval")
    if isinstance(appr, dict) and appr.get("order_hash") == fp:
        ready = False
        if ticket.get("status") in ("placed", "filled"):
            res = ticket.get("result") or {}
            status = "PLACED: order id %s, use result to follow it" % res.get("order_id")
        else:
            status = "APPROVED by you at %s, waiting for execute" % appr.get("at")
    return {"kind": o.get("kind"), "symbol": o.get("symbol"), "date": o.get("date"),
            "order": o, "order_hash": fp, "plain": plain(o), "payload": payload_for(o),
            "ready": ready, "status": status}


def _ledger_add(cfg, key, rec):
    state = config.state_read(cfg, "orders.json", {}) or {}
    state.setdefault(key, []).append(rec)
    config.state_write(cfg, "orders.json", state)


# ---------------------------------------------------------------- clock

def _clock(a):
    try:
        now = parse_ist(a.now) if getattr(a, "now", None) else now_ist()
    except ValueError as e:
        raise TicketError("--now wants an IST time like \"2026-10-26 10:00\": %s" % e)
    today = getattr(a, "today", None) or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-26")
    return now, today


# ---------------------------------------------------------------- commands

def cmd_show(a, source=None):
    cfg = config.load(a.config)
    _, today = _clock(a)
    plan = load_plan(a.plan, a.symbol)
    flatten_mode = bool(a.flatten) or (_read_ticket(a.ticket).get("kind") == "flatten")
    o = flatten_order(plan) if flatten_mode else entry_order(plan)
    view = ticket_view(o, _read_ticket(a.ticket), read_json(a.risk, "risk verdict",
                                                            required=False), today)
    if a.json:
        print(json.dumps(view, indent=2))
        return 0 if view["ready"] else 1
    mode = config.mode(cfg)
    label = "SANDBOX, no real money" if mode == "sandbox" else "LIVE, REAL MONEY"
    lines = ["TICKET for %s, %s, %s: %s" % (view["symbol"], view["kind"], label, view["plain"])]
    lines.append("  order hash %s: every gate binds this exact order; edit anything and the "
                 "PASS and the approval are void" % view["order_hash"])
    lines.append("  " + view["status"])
    lines.append("  gate 3, host tool: Claude shows an Allow once / Deny card before the execute "
                 "call; that card is the host approval and is never automated here")
    lines.append("  Upstox will receive exactly:")
    lines.extend("    " + l for l in json.dumps(view["payload"], indent=2).splitlines())
    if view["ready"]:
        lines.append("  Your move: record your yes with approve --yes \"your reply word for word\"")
    print("\n".join(lines))
    print("One ticket at a time. Education and tooling, not financial advice.")
    return 0 if view["ready"] else 1


def cmd_approve(a, source=None):
    cfg = config.load(a.config)
    now, today = _clock(a)
    plan = load_plan(a.plan, a.symbol)
    flatten_mode = bool(a.flatten) or (_read_ticket(a.ticket).get("kind") == "flatten")
    o = flatten_order(plan, cfg) if flatten_mode else entry_order(plan)
    risk_gate(o, read_json(a.risk, "risk verdict", required=False), today)
    ok, why = parse_yes(a.yes, o)
    if not ok:
        print("%s: not recorded: %s. Show the ticket and ask again." % (SKILL, why), file=sys.stderr)
        return 2
    fp = order_hash(o)
    payload = payload_for(o)
    ticket = {"kind": o.get("kind"), "symbol": o.get("symbol"), "date": today, "order": o,
              "order_hash": fp, "plain": plain(o), "payload": payload, "status": "approved",
              "approval": {"at": now.isoformat(), "date": today, "reply": a.yes.strip(),
                           "order_hash": fp, "risk": "PASS", "mode": config.mode(cfg)},
              "result": None}
    write_json(a.ticket, ticket)
    _ledger_add(cfg, "orders", {"at": now.isoformat(), "date": today, "status": "approved",
                                "kind": o.get("kind"), "symbol": o.get("symbol"),
                                "side": o.get("side"), "quantity": o.get("quantity"),
                                "tag": o.get("tag"), "order_hash": fp})
    print("Recorded your yes for this exact order (hash %s): %s" % (fp, plain(o)))
    print("Upstox will receive exactly this payload when you execute:")
    print(json.dumps(payload, indent=2))
    print("Gate 3, host tool: the Allow once / Deny card Claude shows before the call.")
    print("Now: python3 ticket.py execute --ticket %s" % a.ticket)
    return 0


def cmd_execute(a, source=None):
    cfg = config.load(a.config)
    now, today = _clock(a)
    ticket = _read_ticket(a.ticket)
    o = ticket.get("order")
    if not isinstance(o, dict):
        raise TicketError("gate 2 (approval): no order on the ticket at %s; approve one first" % a.ticket)
    if ticket.get("status") in ("placed", "filled"):
        # already placed: re-submitting returns the existing order id (duplicate)
        pass
    risk_gate(o, read_json(a.risk, "risk verdict", required=False), today)
    approval_gate(o, ticket, today)
    payload = payload_for(o)
    if isinstance(ticket.get("payload"), dict) and ticket["payload"] != payload:
        raise TicketError("the ticket's stored payload no longer matches its order; approve again")
    config.assert_live_allowed(cfg)  # fail closed before any broker call
    host = LIVE if config.is_live(cfg) else SANDBOX
    broker = source if source is not None else UpstoxBroker(cfg=cfg)
    try:
        res = broker.place(payload)
    except UpstoxAuthError as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        print("%s: the Upstox access token is missing, expired or wrong: refresh "
              "UPSTOX_ACCESS_TOKEN (it expires daily at 03:30 IST) or UPSTOX_SANDBOX_TOKEN, "
              "then run execute again. Nothing was retried." % SKILL, file=sys.stderr)
        return 2
    except UpstoxError as exc:
        print("%s: the order was not placed: %s" % (SKILL, exc), file=sys.stderr)
        return 2
    except orders.PlanError as exc:
        raise TicketError("the order payload does not validate: %s" % exc)
    order_id = res.get("order_id")
    dup = bool(res.get("duplicate"))
    rec = {"at": now.isoformat(), "date": today, "status": "placed", "kind": o.get("kind"),
           "symbol": o.get("symbol"), "side": o.get("side"), "quantity": o.get("quantity"),
           "tag": o.get("tag"), "order_id": order_id, "order_hash": ticket.get("order_hash"),
           "duplicate": dup, "host": host}
    _ledger_add(cfg, "orders", rec)
    ticket["status"] = "placed"
    ticket["result"] = {"order_id": order_id, "status": res.get("status"), "duplicate": dup,
                        "host": host, "at": now.isoformat(), "fills": [], "positions": []}
    write_json(a.ticket, ticket)
    if dup:
        print("Upstox already had this tag: existing order id %s, no new order was created." % order_id)
    print("Sent to %s: %s" % (host, plain(o)))
    print("order id %s, status %s" % (order_id, res.get("status")))
    print("Follow it: python3 ticket.py result --ticket %s" % a.ticket)
    return 0


def cmd_result(a, source=None):
    cfg = config.load(a.config)
    now, today = _clock(a)
    ticket = _read_ticket(a.ticket)
    res = ticket.get("result")
    o = ticket.get("order")
    if not isinstance(res, dict) or not res.get("order_id") or not isinstance(o, dict):
        raise TicketError("nothing was placed from this ticket; execute first")
    config.assert_live_allowed(cfg)
    broker = source if source is not None else UpstoxBroker(cfg=cfg)
    order_id = str(res["order_id"])
    try:
        book = _rows(broker.orders())
        trades = _rows(broker.trades())
        positions = _rows(broker.positions())
    except UpstoxAuthError as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        print("%s: refresh the token, then run result again. Nothing was retried." % SKILL,
              file=sys.stderr)
        return 2
    row = next((r for r in book if str(r.get("order_id")) == order_id), None)
    book_status = (row or {}).get("status") or res.get("status")
    fills = []
    for t in trades:
        if str(t.get("order_id")) != order_id:
            continue
        fills.append({"trade_id": t.get("trade_id"), "side": t.get("transaction_type"),
                      "quantity": t.get("quantity"), "price": t.get("average_price"),
                      "at": t.get("traded_at")})
    filled_qty = sum(float(f["quantity"] or 0) for f in fills)
    avg = (round(sum(float(f["price"] or 0) * float(f["quantity"] or 0) for f in fills)
                 / filled_qty, 2) if filled_qty else None)
    pos = next((p for p in positions if p.get("instrument_token") == o.get("instrument_key")), None)
    position = ({"quantity": pos.get("quantity"), "average_price": pos.get("average_price"),
                 "last_price": pos.get("last_price"), "pnl": pos.get("pnl")} if pos else None)
    res["book_status"] = book_status
    res["fills"] = fills
    res["positions"] = position or []
    res["checked_at"] = now.isoformat()
    if str(book_status).lower() in ("complete", "filled"):
        ticket["status"] = "filled"
        if fills and str(o.get("side", "")).upper() == "SELL" and avg is not None:
            pnl = a.pnl if a.pnl is not None else round((avg - float(o.get("entry"))) * filled_qty, 2)
            _ledger_add(cfg, "trades", {"at": now.isoformat(), "date": today,
                                        "symbol": o.get("symbol"), "side": "SELL",
                                        "qty": filled_qty, "price": avg, "pnl": pnl,
                                        "tag": o.get("tag")})
    else:
        ticket["status"] = "placed"
    write_json(a.ticket, ticket)
    print("order %s (%s %s %s): book status %s, %s fill(s)" % (order_id, o.get("side"),
                                                               o.get("quantity"), o.get("symbol"),
                                                               book_status, len(fills)))
    for f in fills:
        print("  fill %s: %s @ %s" % (f.get("trade_id"), f.get("quantity"), _money(f.get("price"))))
    if position is not None:
        print("  position: %s @ avg %s, last %s, pnl %s" % (position.get("quantity"),
                                                            _money(position.get("average_price")),
                                                            _money(position.get("last_price")),
                                                            _money(position.get("pnl"))))
    return 0


def cmd_fill(a, source=None):
    cfg = config.load(a.config)
    now, today = _clock(a)
    qty, price = float(a.qty), float(a.price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if a.side not in ("buy", "sell"):
        raise TicketError("--side is buy or sell")
    rec = {"at": now.isoformat(), "date": today, "symbol": a.symbol.upper(), "side": a.side,
           "qty": qty, "price": price}
    if a.pnl is not None:
        rec["pnl"] = float(a.pnl)
    state_path = getattr(a, "state", None)
    if state_path:
        state = read_json(state_path, "ledger", required=False) or {}
        state.setdefault("trades", []).append(rec)
        write_json(state_path, state)
    else:
        _ledger_add(cfg, "trades", rec)
    print("Recorded %s %s %s at %s on %s." % (a.side, "%g" % qty, rec["symbol"],
                                              _money(price), today))
    return 0


def cmd_flatten(a, source=None):
    cfg = config.load(a.config)
    now, today = _clock(a)
    plan = load_plan(a.plan, a.symbol)
    qty = int(a.qty) if a.qty is not None else None
    if a.qty is not None and qty <= 0:
        raise TicketError("--qty wants the number you hold, like --qty 25")
    o = flatten_order(plan, cfg, qty=qty)
    ticket = {"kind": o["kind"], "symbol": o["symbol"], "date": today, "order": o,
              "order_hash": order_hash(o), "plain": plain(o), "payload": payload_for(o),
              "status": "proposed", "approval": None, "result": None}
    write_json(a.ticket, ticket)
    print("Flatten %s: %s (flat by %s IST)." % (o["symbol"], plain(o), o.get("flat_by")))
    print("Wrote the proposed ticket to %s. Nothing is sent yet: the same three gates apply." % a.ticket)
    print("  1. trader-risk check.py: a fresh PASS must cover this flatten order (same plan, "
          "so run it before you approve the entry, or again after any edit).")
    print("  2. ticket.py approve --flatten --yes \"your reply word for word\".")
    print("  3. ticket.py execute. The host tool card asks Allow once / Deny.")
    print("If the stop or target leg is still open at Upstox, cancel it first: the flatten is a "
          "separate order and the open leg can fill against it.")
    return 0


def cmd_log(a, source=None):
    cfg = config.load(a.config)
    _, today = _clock(a)
    state = config.state_read(cfg, "orders.json", {}) or {}
    rows = [r for r in state.get("orders", []) if a.all or r.get("date") == today]
    trades = [t for t in state.get("trades", []) if a.all or t.get("date") == today]
    if not rows and not trades:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" tag " + str(r["tag"]) if r.get("tag") else "")
        extra += (" id " + str(r["order_id"]) if r.get("order_id") else "")
        print("%s  %-9s %-8s %s %s x %s%s" % (r.get("at"), r.get("status"), r.get("kind"),
                                              r.get("side"), r.get("symbol"), r.get("quantity"),
                                              extra))
    for t in trades:
        pnl = (" pnl %s" % _money(t["pnl"])) if t.get("pnl") is not None else ""
        print("%s  fill       %-8s %s x %s @ %s%s" % (t.get("at"), t.get("symbol"), t.get("qty"),
                                                      _money(t.get("price")), pnl))
    print("%d order(s) placed%s." % (sum(1 for r in rows if r.get("status") == "placed"),
                                     "" if a.all else " today"))
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None, source=None):
    """CLI entry. source is the injected broker seam for tests (place, orders,
    trades, positions like UpstoxBroker); when it is None the real Upstox
    broker is built from the config, sandbox host by default."""
    ap = argparse.ArgumentParser(prog="ticket.py",
        description="Order tickets and your yes (trader-broker): risk PASS, typed yes, Upstox. "
                    "Sandbox by default.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default=PLAN_PATH)
        p.add_argument("--ticket", default=TICKET_PATH)
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default=RISK_PATH)
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-26 10:00\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    p = sub.add_parser("show", help="render the ticket and the exact payload")
    common(p)
    p.add_argument("--symbol")
    p.add_argument("--flatten", action="store_true", help="show the flatten order instead of the entry")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes, bound to the order hash")
    common(p)
    p.add_argument("--symbol")
    p.add_argument("--flatten", action="store_true")
    p.add_argument("--yes", required=True, help="the user's reply, word for word")
    p = sub.add_parser("execute", help="place the approved order through the broker seam")
    common(p)
    p.add_argument("--symbol")
    p = sub.add_parser("result", help="read the order book and update the ticket")
    common(p)
    p.add_argument("--pnl", type=float, help="override the realized P&L recorded for a filled exit")
    p = sub.add_parser("fill", help="record a fill in the ledger by hand")
    common(p)
    p.add_argument("--state", default=None)
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--pnl", type=float)
    p = sub.add_parser("flatten", help="prepare the opposite MARKET order under the same gates")
    common(p)
    p.add_argument("--symbol")
    p.add_argument("--qty", help="override the quantity (re-run the risk check: this changes the hash)")
    p = sub.add_parser("log", help="today's decisions and fills")
    common(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "execute": cmd_execute,
                "result": cmd_result, "fill": cmd_fill, "flatten": cmd_flatten,
                "log": cmd_log}[a.cmd](a, source)
    except TicketError as e:
        print("%s: %s" % (SKILL, e), file=sys.stderr)
        return 2
    except (config.ConfigError, orders.PlanError, ValueError) as e:
        print("%s: %s" % (SKILL, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
