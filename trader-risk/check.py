#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Indian markets: INR, NSE lots, IST.

Checks the two-leg plan trader-budget sized against the user's limits:
max loss in INR, daily loss from the day's recorded trades, trades per day,
reward:risk, the entry window (IST), freeze quantity, lot size, tick grid,
MIS eligibility, funds and margins through the broker seam, duplicate tags,
charges-adjusted loss, long only. PASS or BLOCK with reasons; a BLOCK is a
verdict, never a crash.

Standard library only, Python 3.8, no network in this module: the broker
seam is injected (main argv, source=None) or read from recorded state.
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import calendar as cal  # noqa: E402
from trader_core import charges as charges_mod  # noqa: E402
from trader_core import config, orders  # noqa: E402
from trader_core.http import SandboxUnsupported  # noqa: E402
from trader_core.timeutil import now_ist, parse_ist  # noqa: E402

SKILL = "trader-risk"
PLAN_PATH = "trader/plan.json"
ORDERS_STATE = "orders.json"
RISK_STATE = "risk.json"
EPS = 1e-9


class RiskError(Exception):
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


def _p(x):
    return ("%f" % float(x)).rstrip("0").rstrip(".") if float(x) % 1 else "%d" % round(float(x))


# ---------------------------------------------------------------- state

def _plan_doc(plan_):
    """The sized plan itself. trader-budget --json prints
    {"sizing", "plan", "legs"}, so a wrapper is unwrapped; anything that is
    not a dict becomes an empty plan and the checks report its gaps."""
    if isinstance(plan_, dict) and isinstance(plan_.get("plan"), dict):
        return plan_["plan"]
    return plan_ if isinstance(plan_, dict) else {}


def _plan_legs(plan_):
    legs = (plan_ or {}).get("legs") or []
    by_role = {}
    for one in legs:
        if isinstance(one, dict) and one.get("role"):
            by_role.setdefault(one["role"], one)
    return by_role


def _quantity(plan_):
    """The plan's unit quantity, else the quantity its legs carry; None when
    the plan is unsized, so a missing number is a reason, never a traceback."""
    qty = _f((plan_ or {}).get("quantity"))
    if qty is not None:
        return qty
    legs = _plan_legs(plan_)
    for role in ("entry", "target", "stop"):
        qty = _f((legs.get(role) or {}).get("quantity"))
        if qty is not None:
            return qty
    return None


def _entry_price(plan_):
    legs = _plan_legs(plan_)
    entry = legs.get("entry") or {}
    return _f(entry.get("price"))


def _stop_price(plan_):
    legs = _plan_legs(plan_)
    stop = legs.get("stop") or {}
    return _f(stop.get("trigger_price")) if stop.get("trigger_price") is not None \
        else _f(stop.get("price"))


def _target_price(plan_):
    legs = _plan_legs(plan_)
    target = legs.get("target") or {}
    return _f(target.get("price"))


def _trade_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def day_pnl(state, today):
    """The realized P&L of the day's recorded trades, INR (losses negative).
    Reads the records trader-broker logs: an explicit pnl when present, else
    a sell fill counted as an exit of a same-day entry at the plan's prices
    is beyond this function: only explicit pnl or a loss-tagged record counts."""
    pnl = 0.0
    for rec in (state or {}).get("trades", []):
        if _trade_day(rec) != today:
            continue
        value = _f(rec.get("pnl"))
        if value is not None:
            pnl += value
    return round(pnl, 2)


def _trade_tags(state, today):
    """Tags recorded (approved or placed) today, for the duplicate check."""
    tags = set()
    for rec in (state or {}).get("orders", []):
        if _trade_day(rec) != today:
            continue
        if rec.get("status") in ("approved", "placed", "filled"):
            tag = rec.get("tag")
            if tag:
                tags.add(str(tag))
    return tags


# ---------------------------------------------------------------- the check

def check(plan_, cfg, now=None, broker=None, record=None, instrument=None):
    """Pure function. Returns the verdict dict:
    {"verdict", "reasons", "checks", "warnings", "plan", "symbol", "now"}.

    broker     the injected broker seam (funds(), margins(keys), orders()):
               missing answers become reasons, never a pass
    instrument a normalized instrument row; without one the lot, tick and
               freeze checks read the plan's own fields
    record     {"trades": [...], "orders": [...]} standing in for the day's
               recorded state, for tests and the dry path
    """
    reasons, warns = [], []
    checks = {}
    now = now_ist() if now is None else now
    today = now.date().isoformat()
    acct = config.limits(cfg)
    state = record if record is not None else config.state_read(cfg, ORDERS_STATE, {})
    plan_ = _plan_doc(plan_)

    # the plan as a set: trader_core.orders.validate_plan
    reasons.extend(orders.validate_plan(plan_, instrument or {}, cfg))
    checks["plan"] = "two-leg plan validates as a set" if not reasons else "; ".join(reasons)

    entry = _entry_price(plan_)
    stop = _stop_price(plan_)
    target = _target_price(plan_)
    qty = _quantity(plan_)
    qty_s = _p(qty) if qty is not None else "no quantity"
    lot = _f((instrument or {}).get("lot_size")) or _f((plan_ or {}).get("lot_size")) or 1
    tick = _f((instrument or {}).get("tick_size")) or _f((plan_ or {}).get("tick_size"))
    freeze = _f((instrument or {}).get("freeze_quantity"))
    key = (plan_ or {}).get("instrument_key") or (instrument or {}).get("instrument_key")
    sym = str((plan_ or {}).get("symbol") or (instrument or {}).get("trading_symbol") or "").upper()

    # max loss in INR
    max_loss = None
    if entry is not None and stop is not None and qty:
        per_unit = max(entry - stop, (tick or 0.05))
        max_loss = round(qty * per_unit, 2)
    cap = _f(acct.get("max_loss_per_trade_inr"))
    if max_loss is not None and cap:
        if max_loss > cap + EPS:
            reasons.append("max loss %s at the stop is over your %s max loss per trade"
                           % (money(max_loss), money(cap)))
        checks["max_loss"] = "%s of %s max loss per trade" % (money(max_loss), money(cap))
    else:
        checks["max_loss"] = "not measurable: the plan has no entry, stop and quantity"
        if max_loss is None:
            reasons.append("the plan has no measurable max loss: entry, stop and quantity are required")

    # daily loss from the day's recorded trades
    daily_cap = _f(acct.get("max_daily_loss_inr"))
    day_loss = day_pnl(state, today)
    if daily_cap:
        if day_loss <= -daily_cap - EPS:
            reasons.append("daily loss limit reached: %s realized today is at or past your %s cap; "
                           "no new entries today" % (money(-day_loss), money(daily_cap)))
        checks["daily_loss"] = "%s realized today of %s cap" % (money(-day_loss), money(daily_cap))

    # trades per day
    trades_cap = int(acct.get("max_trades_per_day") or 0)
    entries_today = [r for r in (state or {}).get("orders", [])
                     if _trade_day(r) == today and r.get("status") in ("approved", "placed", "filled")
                     and str(r.get("kind", r.get("side", ""))) in ("entry", "BUY", "buy")]
    if trades_cap and len(entries_today) >= trades_cap:
        reasons.append("%d entries recorded today, your limit is %d a day"
                       % (len(entries_today), trades_cap))
    checks["trades_today"] = "%d of %s" % (len(entries_today), trades_cap or "unlimited")

    # orders per day: every decision recorded today counts, entries and flattens alike
    orders_cap = int(acct.get("max_orders_per_day") or 0)
    orders_today = [r for r in (state or {}).get("orders", [])
                    if _trade_day(r) == today and r.get("status") in ("approved", "placed", "filled")]
    if orders_cap and len(orders_today) >= orders_cap:
        reasons.append("%d orders recorded today, your limit is %d a day"
                       % (len(orders_today), orders_cap))
    checks["orders_today"] = "%d of %s" % (len(orders_today), orders_cap or "unlimited")

    # reward:risk
    min_rr = _f(acct.get("min_reward_risk"))
    rr = None
    if entry and stop and target and entry - stop > 0:
        rr = round((target - entry) / (entry - stop), 2)
        if min_rr and rr < min_rr - EPS:
            reasons.append("reward:risk %s (target %s, entry %s, stop %s) is under your minimum %s"
                           % (rr, _p(target), _p(entry), _p(stop), min_rr))
        checks["reward_risk"] = "%s (minimum %s)" % (rr, min_rr)

    # entry window, IST, configurable; a weekend or NSE holiday has no window at all
    times = config.session_times(cfg)
    start, cutoff = times["entry_start"], times["entry_cutoff"]
    t = now.time()
    day = now.date()
    closed = None
    if not cal.is_trading_day(day):
        closed = cal.load_holidays().get(day.isoformat()) or (
            "weekend" if cal.is_weekend(day) else "closed")
        reasons.append("NSE is closed on %s (%s): no entries today" % (day.isoformat(), closed))
    elif not (start <= t < cutoff):
        reasons.append("outside the entry window: entries run %s to %s IST, it is now %s"
                       % (start.strftime("%H:%M"), cutoff.strftime("%H:%M"),
                          now.strftime("%H:%M")))
    checks["entry_window"] = ("NSE closed on %s (%s)" % (day.isoformat(), closed) if closed else
                              "%s to %s IST, now %s" % (start.strftime("%H:%M"), cutoff.strftime("%H:%M"),
                                                        now.strftime("%H:%M")))

    # freeze quantity
    if freeze and qty and qty > freeze:
        reasons.append("quantity %s is above the freeze quantity %s for %s"
                       % (int(qty), int(freeze), sym))
    checks["freeze"] = ("quantity %s of freeze %s" % (qty_s, _p(freeze))) if freeze else "no freeze data"

    # lot size
    if lot and qty and int(qty) % int(lot) != 0:
        reasons.append("quantity %s is not a whole multiple of lot_size %s" % (int(qty), int(lot)))
    checks["lot_size"] = ("quantity %s on lot %s" % (qty_s, _p(lot))) if lot else "no lot data"

    # tick grid
    if tick:
        for role, leg_ in (("entry", _plan_legs(plan_).get("entry")),
                           ("stop", _plan_legs(plan_).get("stop")),
                           ("target", _plan_legs(plan_).get("target"))):
            if not isinstance(leg_, dict):
                continue
            for field in ("price", "trigger_price"):
                value = leg_.get(field)
                if value is not None and not orders.on_grid(_f(value), tick):
                    reasons.append("%s %s %s is not on the %s tick grid" % (role, field, value, tick))
    checks["tick"] = "prices on the %s grid" % tick if tick else "no tick data"

    # MIS eligibility: product I plus risk.mis_products (product codes, with
    # the exchange string NSE_MIS kept as the older spelling of I)
    product = (plan_ or {}).get("product")
    mis_products = [str(p) for p in (cfg.get("risk") or {}).get("mis_products") or []]
    mis_ok = (not mis_products or product in mis_products
              or (product == "I" and "NSE_MIS" in mis_products))
    if product == "I":
        if not mis_ok:
            reasons.append("product I is not in your risk.mis_products list %s; not MIS eligible"
                           % mis_products)
        checks["mis"] = "product I (MIS intraday)"
    elif product in ("D", "MTF"):
        reasons.append("product %s is not MIS intraday: this skill trades the intraday product I only"
                       % product)
        checks["mis"] = "product %s is not MIS" % product
    else:
        reasons.append("the plan has no MIS product: expected product I")
        checks["mis"] = "no product on the plan"

    # funds and margins via the broker seam; a missing answer is a reason
    if broker is not None:
        try:
            funds = broker.funds()
        except Exception as exc:  # noqa: BLE001: any seam failure is a reason
            reasons.append("funds check failed: %s" % exc)
        else:
            available = _available_margin(funds)
            if available is None:
                reasons.append("funds answer has no usable available margin; add margin or check the "
                               "funds and margin segment on Upstox")
            elif entry and qty and available < entry * qty:
                reasons.append("available margin %s is under the entry cost %s for %s units"
                               % (money(available), money(entry * qty), int(qty)))
            else:
                funds_ok = True
            checks["funds"] = "available margin %s" % money(available) if available is not None \
                else "no usable funds answer"
        if key:
            margins, note = None, None
            try:
                margins = broker.margins([key])
            except SandboxUnsupported:
                note = "margins not supplied by this seam: not checked here"
            except Exception as exc:  # noqa: BLE001
                reasons.append("margin check failed: %s" % exc)
            else:
                need = _margin_required(margins, key)
                if need is None:
                    reasons.append("margin answer has no usable required margin for %s" % key)
                else:
                    if entry and qty and need > entry * qty + EPS:
                        warns.append("margin required %s exceeds the entry cost %s; check the margin window"
                                     % (money(need), money(entry * qty)))
                    note = "required margin %s" % money(need)
            checks["margins"] = note or "required margin unavailable"
        else:
            reasons.append("no instrument key on the plan: margin cannot be checked")
    else:
        checks["funds"] = "no broker seam: not checked here (the broker skill confirms funds)"
        checks["margins"] = "no broker seam: not checked here"

    # duplicate: same tag already recorded today
    tag = None
    for leg_ in (plan_ or {}).get("legs") or []:
        if isinstance(leg_, dict) and leg_.get("tag"):
            tag = leg_["tag"]
            break
    if tag and tag in _trade_tags(state, today):
        reasons.append("duplicate: a plan with tag %s was already recorded today" % tag)
    checks["duplicate"] = "tag %s %s" % (tag or "none", "already recorded today" if tag and
                                         tag in _trade_tags(state, today) else "not recorded")

    # charges-adjusted loss: the stop must still be inside the cap net of charges
    if max_loss is not None and qty and entry is not None and stop is not None:
        try:
            rt = charges_mod.round_trip(entry, stop, int(qty), cfg=cfg)
        except orders.PlanError as exc:
            reasons.append("charges check failed: %s" % exc)
        else:
            net_loss = -rt["net_pnl"]
            if net_loss > cap + EPS:
                reasons.append("charges-adjusted loss %s (charges %s) is over your %s max loss per trade; "
                               "the gross loss %s fits, the net does not"
                               % (money(net_loss), money(rt["total_charges"]), money(cap), money(max_loss)))
            checks["charges"] = "net loss at the stop %s after %s charges (gross %s)" % (
                money(net_loss), money(rt["total_charges"]), money(max_loss))

    # long only
    if (plan_ or {}).get("direction") == "short" and acct.get("long_only", True):
        reasons.append("short plans are blocked: this skill is long only")
    checks["long_only"] = "long only" if acct.get("long_only", True) else "long and short allowed"

    # optional user-supplied watch lists: blocked blocks, the rest warn; never live surveillance
    risk_cfg = cfg.get("risk") or {}
    blocked = [str(s).upper() for s in risk_cfg.get("blocked_symbols") or []]
    if sym and sym in blocked:
        reasons.append("%s is on your risk.blocked_symbols list in the config: blocked from trading"
                       % sym)
    for name, label in (("ban_symbols", "ban"), ("asm_symbols", "ASM"), ("gsm_symbols", "GSM")):
        listed = risk_cfg.get(name) or []
        if sym and sym in [str(s).upper() for s in listed]:
            warns.append("%s is on your user-supplied %s list in the config (no live surveillance feed "
                         "is checked); review before trading" % (sym, label))

    verdict = "BLOCK" if reasons else "PASS"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "checks": checks,
        "warnings": warns,
        "symbol": sym,
        "now": now.strftime("%Y-%m-%d %H:%M IST"),
        "plan": plan_,
    }


def _available_margin(funds):
    """available_margin from an Upstox funds envelope, across known shapes."""
    if not isinstance(funds, dict):
        return None
    for seg in ("equity", "SECURITIES"):
        data = funds.get(seg)
        if isinstance(data, dict):
            value = _f(data.get("available_margin"))
            if value is not None:
                return value
    data = funds.get("data")
    if isinstance(data, dict):
        return _available_margin(data)
    return None


def _margin_required(margins, key):
    """total margin required for key, across known Upstox margin shapes."""
    if not isinstance(margins, dict):
        return None
    for cont in ("equity", "commodity"):
        data = margins.get(cont)
        if isinstance(data, dict):
            for inst in data.get("instruments") or []:
                if isinstance(inst, dict) and inst.get("instrument_key") == key:
                    return _f(inst.get("total_margin"))
    for inst in (margins.get("data") or {}).get("instruments") or []:
        if isinstance(inst, dict) and inst.get("instrument_key") == key:
            return _f(inst.get("total_margin"))
    return None


class FundsSeam(object):
    """Offline broker seam from one recorded JSON file: the Upstox funds
    envelope, or {"funds": ..., "margins": ...}. When no margins segment is
    recorded, margins() says so and the check reports it, never invents one."""

    def __init__(self, doc):
        if isinstance(doc, dict) and ("funds" in doc or "margins" in doc):
            self._funds, self._margins = doc.get("funds"), doc.get("margins")
        else:
            self._funds, self._margins = doc, None

    def funds(self):
        return self._funds

    def margins(self, keys):
        if self._margins is None:
            raise SandboxUnsupported("the funds fixture carries no margins segment")
        return self._margins


# ---------------------------------------------------------------- report

def report(res):
    lines = []
    for w in res.get("warnings", []):
        lines.append("WARNING: " + w)
    if res.get("warnings"):
        lines.append("")
    lines.append("%s %s at %s" % (res["verdict"], res.get("symbol") or "plan", res.get("now", "")))
    for name, value in res["checks"].items():
        lines.append("  %-12s %s" % (name, value))
    for r in res["reasons"]:
        lines.append("  - " + r)
    lines.append("")
    if res["verdict"] == "BLOCK":
        lines.append("Blocked: do not send this plan to the broker. Fix the reasons above, then check again.")
    else:
        lines.append("Every check passed. trader-broker still asks you yes or no before anything is placed.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main(argv=None, source=None):
    """CLI entry. source is the injected broker seam for tests (funds(),
    margins(keys), orders()); when it is None the checks that need the
    broker are reported as not checked, never silently passed."""
    parser = argparse.ArgumentParser(
        prog="check.py",
        description="Check a trade plan against your INR and IST limits (trader-risk).")
    parser.add_argument("--plan", default=PLAN_PATH, help="the plan JSON (default trader/plan.json)")
    parser.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    parser.add_argument("--instruments", help="Upstox instrument master slice, for lot, tick, freeze")
    parser.add_argument("--broker", action="store_true",
                        help="check funds and margins through the Upstox broker (uses your token)")
    parser.add_argument("--funds", metavar="FILE",
                        help="recorded Upstox funds JSON, or {funds, margins}, instead of the broker")
    parser.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-26 10:00\"")
    parser.add_argument("--out", default="trader/state/" + RISK_STATE, help="where the verdict is recorded")
    parser.add_argument("--no-write", action="store_true", help="do not record the verdict")
    parser.add_argument("--json", action="store_true", help="print the verdict JSON to stdout")
    args = parser.parse_args(argv)
    try:
        with open(args.plan, "r", encoding="utf-8") as fh:
            plan_ = json.load(fh)
    except (OSError, ValueError) as exc:
        print("%s: cannot read the plan %s: %s (run trader-budget first)" % (SKILL, args.plan, exc),
              file=sys.stderr)
        return 2
    plan_ = _plan_doc(plan_)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        return 2
    instrument = None
    if args.instruments:
        try:
            from trader_core import instruments as instruments_mod
            with open(args.instruments, "rb") as fh:
                rows = instruments_mod.load(fh.read())
            instrument = instruments_mod.find(rows, plan_.get("symbol") or "")
        except (OSError, ValueError) as exc:
            print("%s: cannot use the instrument master %s: %s" % (SKILL, args.instruments, exc),
                  file=sys.stderr)
            return 2
    now = None
    if args.now:
        try:
            now = parse_ist(args.now)
        except ValueError as exc:
            print("%s: %s" % (SKILL, exc), file=sys.stderr)
            return 2
    broker = source
    if broker is None and getattr(args, "funds", None):
        try:
            with open(args.funds, "r", encoding="utf-8") as fh:
                funds_doc = json.load(fh)
        except (OSError, ValueError) as exc:
            print("%s: cannot read the funds fixture %s: %s" % (SKILL, args.funds, exc),
                  file=sys.stderr)
            return 2
        broker = FundsSeam(funds_doc)
    if broker is None and getattr(args, "broker", False):
        from trader_core.broker import UpstoxBroker
        from trader_core.upstox import Client
        broker = UpstoxBroker(Client(cfg), cfg=cfg)
    res = check(plan_, cfg, now=now, broker=broker, instrument=instrument)
    doc = dict(res)
    doc["generated_at"] = now_ist().replace(microsecond=0).isoformat()
    if not args.no_write:
        d = os.path.dirname(args.out)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, args.out)
    print(json.dumps(doc, indent=2) if args.json else report(doc))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
