#!/usr/bin/env python3
"""Position sizing for trader-budget. Indian markets: INR, NSE lots, IST.

Reads the trade plan the exit skill wrote (trader/plan.json by default),
sizes it against the INR budget and the per-trade max loss, rounds every
price onto the instrument's tick grid without widening the risk, rounds the
quantity down to whole lots (equity lot is 1, F&O lots come from the Upstox
instrument row), and emits the two-leg plan contract: legs built with
trader_core.orders, ready for trader-risk and trader-broker.

Standard library only, Python 3.8, no network in this module.
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import config, orders  # noqa: E402
from trader_core.timeutil import now_ist, parse_ist  # noqa: E402

SKILL = "trader-budget"
EPS = 1e-9


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return "Rs " + "{:,.0f}".format(round(x))
    return "Rs " + "{:,.2f}".format(x)


def _price(x):
    return ("%f" % float(x)).rstrip("0").rstrip(".") if float(x) % 1 else "%d" % round(float(x))


def read_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("%s not found: %s (%s)" % (what, path, _MISSING[what]))
    except (OSError, ValueError) as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


_MISSING = {
    "plan": "run trader-exit first, then size it here",
    "instrument master": "give --instruments (a recorded Upstox master slice)",
    "budget config": "start from trader-budget/config.example.json",
}


# ---------------------------------------------------------------- sizing

def size_plan(plan_doc, cfg, instrument, budget=None, max_loss=None, product=None, today=None):
    """Pure function: plan + rules + instrument -> the sized plan (a dict).

    today is the IST date (default: the real clock); a plan for another day
    is refused.

    Instrument is a trader_core.instruments normalized row (lot_size,
    tick_size, freeze_quantity, instrument_key). Quantity is whole lots,
    rounded down so the risk never rounds up; entry, stop and target are
    snapped onto the tick grid in the direction that never widens the risk:
    the stop moves away from the entry (never closer), the target never
    moves closer to the entry, the entry itself never above its plan level.
    Minimum risk is one tick.
    """
    if not isinstance(plan_doc, dict):
        raise SizeError("the plan is not a JSON object")
    today = today or now_ist().date().isoformat()
    sym = str(plan_doc.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    if plan_doc.get("date") and plan_doc["date"] != today:
        raise SizeError("the plan is for %s and today is %s IST: make a new plan with trader-exit"
                        % (plan_doc["date"], today))
    if instrument is None:
        raise SizeError("no instrument row for %s: pass --instruments" % sym)

    key = instrument.get("instrument_key")
    tick = _f(instrument.get("tick_size")) or _f(cfg.get("data", {}).get("tick_default")) or 0.05
    if tick is None or tick <= 0:
        raise SizeError("instrument %s has no usable tick_size" % sym)
    try:
        lot = int(instrument.get("lot_size") or 1)
    except (TypeError, ValueError):
        lot = 1
    if lot <= 0:
        lot = 1
    freeze = _f(instrument.get("freeze_quantity"))

    acct = config.limits(cfg)
    budget = _f(budget if budget is not None else acct.get("budget_inr"))
    max_loss_inr = _f(max_loss if max_loss is not None else acct.get("max_loss_per_trade_inr"))
    if not budget or budget <= 0:
        raise SizeError("no budget: pass --budget or set account.budget_inr in the config")
    if not max_loss_inr or max_loss_inr <= 0:
        raise SizeError("no max loss: set account.max_loss_per_trade_inr in the config")

    product = product or cfg.get("products", {}).get("default") or "I"

    entry = _f(plan_doc.get("entry"))
    stop = _f(plan_doc.get("stop"))
    target = _f(plan_doc.get("target"))
    for name, value in (("entry", entry), ("stop", stop), ("target", target)):
        if value is None or value <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit)" % name)

    # Tick snap: stop never closer to the entry, target never closer, entry never higher.
    stop = orders.round_tick(stop, tick, "down")
    target = orders.round_tick(target, tick, "up")
    entry = orders.round_tick(entry, tick, "down")
    if stop >= entry:
        raise SizeError("the stop %s is not below the entry %s after tick rounding; "
                        "a long trade needs its stop underneath" % (_price(stop), _price(entry)))
    if target <= entry:
        raise SizeError("the target %s is not above the entry %s after tick rounding; skip this trade"
                        % (_price(target), _price(entry)))
    risk_per_unit = entry - stop
    if risk_per_unit < tick - EPS:
        risk_per_unit = tick  # minimum risk is one tick
    reward_risk = round((target - entry) / risk_per_unit, 2)

    # Whole lots: risk per lot is per-unit risk x lot size, floored.
    per_lot_risk = risk_per_unit * lot
    by_loss = max_loss_inr / per_lot_risk if per_lot_risk > 0 else 0.0
    by_budget = budget / (entry * lot) if entry > 0 else 0.0
    lots = int(min(by_loss, by_budget))
    bound = "max loss" if by_loss <= by_budget else "budget"
    if freeze:
        lots = min(lots, int(freeze // lot))
        if by_loss <= by_budget and by_loss > freeze / lot:
            bound = "freeze quantity"
    if lots < 0:
        lots = 0
    qty = lots * lot
    cost = round(entry * qty, 2)
    risk_inr = round(qty * risk_per_unit, 2)
    if freeze and qty > freeze:
        raise SizeError("%s lots of %s (%s units) is above the freeze quantity %s; no order"
                        % (lots, sym, qty, int(freeze)))

    refusal = None
    if qty <= 0:
        if by_budget < 1:
            refusal = ("one lot of %s (%s units) costs %s at the entry, more than your %s budget. "
                       "No order. Raise the budget, or pick a cheaper instrument."
                       % (sym, lot, money(entry * lot), money(budget)))
        else:
            refusal = ("one lot of %s (%s units) risks %s at the stop, more than your %s max loss "
                       "per trade. No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (sym, lot, money(per_lot_risk), money(max_loss_inr)))

    plan_out = orders.plan(None, None, None, symbol=sym)
    if qty > 0:
        legs = _legs(key, qty, product, entry, stop, target, tick, plan_doc)
        plan_out = orders.plan(legs["entry"], legs["stop"], legs["target"], symbol=sym)
    plan_out.update({
        "instrument_key": key,
        "date": plan_doc.get("date") or today,
        "direction": "long",
        "entry": entry,
        "stop": stop,
        "target": target,
        "tick_size": tick,
        "lot_size": lot,
        "quantity": qty,
        "risk_per_unit": round(risk_per_unit, 4),
        "risk_inr": risk_inr,
        "max_loss_per_trade_inr": max_loss_inr,
        "reward_risk": reward_risk,
        "product": product,
        "budget_inr": round(budget, 2),
        "cost_inr": cost,
        "bound_by": bound,
        "flat_by": config.session_times(cfg)["flat_by"].strftime("%H:%M"),
        "source": SKILL,
        "generated_at": now_ist().replace(microsecond=0).isoformat(),
        "refusal": refusal,
    })
    return plan_out


def _legs(key, qty, product, entry, stop, target, tick, plan_doc):
    """The three v3 payloads: entry LIMIT, protective stop SL, target LIMIT."""
    tag = str(plan_doc.get("setup") or "orb")[:20]
    return {
        "entry": orders.leg(key, "BUY", qty, "LIMIT", product=product, price=entry, tag="dt-entry-" + tag),
        "stop": orders.leg(key, "SELL", qty, "SL", product=product, price=stop,
                           trigger_price=stop, tag="dt-stop-" + tag),
        "target": orders.leg(key, "SELL", qty, "LIMIT", product=product, price=target,
                             tag="dt-target-" + tag),
    }


# ---------------------------------------------------------------- report

def report(s):
    lines = []
    if s.get("refusal"):
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("%s long, entry %s, stop %s, target %s (tick %s)"
                 % (s["symbol"], _price(s["entry"]), _price(s["stop"]), _price(s["target"]),
                    _price(s["tick_size"])))
    lines.append("Size: %d units of %s = %d lots (lot size %s). Cost %s."
                 % (s["quantity"], s["symbol"], s["quantity"] // max(s["lot_size"], 1),
                    int(s["lot_size"]), money(s["cost_inr"])))
    lines.append("Risk at the stop: %s of your %s max loss. Reward:risk %s. Bound by the %s."
                 % (money(s["risk_inr"]), money(s["max_loss_per_trade_inr"]), s["reward_risk"],
                    s["bound_by"]))
    lines.append("Flat by %s IST, before the MIS square-off. Product %s (intraday)."
                 % (s["flat_by"], s["product"]))
    lines.append("")
    lines.append("Next: trader-risk checks this plan, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def run(a):
    cfg = config.load(a.config)
    plan_doc = read_json(a.plan, "plan")
    sym = str(plan_doc.get("symbol") or "").strip().upper()
    try:
        instrument = None
        if a.instruments:
            from trader_core import instruments as instruments_mod
            with open(a.instruments, "rb") as fh:
                rows = instruments_mod.load(fh.read())
            instrument = instruments_mod.find(rows, sym)
        if instrument is None:
            raise SizeError("no instrument row for %s: pass --instruments "
                            "(a recorded Upstox master slice)" % sym)
    except ValueError as e:
        raise SizeError(str(e))
    s = size_plan(plan_doc, cfg, instrument, budget=a.budget, max_loss=a.max_loss, product=a.product,
                  today=parse_ist(a.now).date().isoformat() if a.now else None)
    if a.json:
        doc = {"sizing": {k: v for k, v in s.items() if k != "legs"}, "plan": s, "legs": s["legs"]}
        print(json.dumps(doc, indent=2))
    else:
        print(report(s))
    return 0 if not s.get("refusal") else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="size.py",
        description="Size a trade plan to your INR budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--instruments", help="Upstox instrument master slice (JSON), for lot, tick, freeze")
    ap.add_argument("--budget", type=float, help="override account.budget_inr")
    ap.add_argument("--max-loss", type=float, help="override account.max_loss_per_trade_inr")
    ap.add_argument("--product", help="override products.default (I, D or MTF)")
    ap.add_argument("--now", help="pretend it is this IST date/time, e.g. \"2026-10-26 10:00\"")
    ap.add_argument("--json", action="store_true", help="print the plan JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except SizeError as e:
        print("%s: %s" % (SKILL, e), file=sys.stderr)
        return 2
    except config.ConfigError as e:
        print("%s: %s" % (SKILL, e), file=sys.stderr)
        return 2
    except ValueError as e:
        print("%s: %s" % (SKILL, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
