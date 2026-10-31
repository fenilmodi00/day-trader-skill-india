#!/usr/bin/env python3
"""IST price alerts for trader-alerts: session bells, stops, targets, flat-by.

    python3 alerts.py add --symbol RELIANCE --instrument-key "NSE_EQ|INE002A01018" --above 1200
    python3 alerts.py add --symbol RELIANCE --stop 1183.65
    python3 alerts.py add --opening-bell
    python3 alerts.py add --flat-by --at 15:10
    python3 alerts.py add --from-plan             # stop, target, flat-by from trader/exit.json
    python3 alerts.py check                       # ring what hit, once, then write the seen state
    python3 alerts.py check --now "2026-10-26 09:15" --json
    python3 alerts.py plan                        # print the plan the alerts follow

Clock, all IST: pre-open bell 09:00, open bell 09:15, flat-by 15:10 (the
config session.flat_by), close bell 15:30. Weekends and NSE holidays ring
nothing and the check says the market is closed. A bell rings at its time
or at the first check after it, never before it: 09:14 does not ring the
open bell.

Quotes: Upstox batch quotes through trader_core.upstox.Client (LTP, OHLC).
A check with no price rules never touches the network. A missing quote is
reported ("no quote for RELIANCE"), never guessed; an expired token asks
you to re-authenticate.

Once-only firing: every ring is recorded under trader/state/alerts.json,
keyed by symbol, alert kind and the IST date, so the next session re-arms
everything automatically.
"""

import argparse
import datetime
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import CLOSE, OPEN, PRE_OPEN
from trader_core import calendar as cal
from trader_core import config, timeutil
from trader_core.config import ConfigError
from trader_core.http import UpstoxAuthError, UpstoxError
from trader_core.upstox import Client

SKILL = "trader-alerts"
RULES_PATH = "trader/alerts.json"
PLAN_PATH = "trader/exit.json"
STATE_FILE = "alerts.json"
PRICE_KINDS = ("above", "below", "stop", "target")
BELL_TIMES = {"preopen": PRE_OPEN, "open": OPEN, "close": CLOSE}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ------------------------------------------------------------------ files

def _read_json(path):
    """Parse a JSON file, ConfigError on anything unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))


def _write_json(path, obj):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read_rules_doc(path):
    """The rules document, {"rules": []} when the file does not exist yet."""
    if not os.path.exists(path):
        return {"rules": []}
    doc = _read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise ConfigError("%s must be a JSON object with a \"rules\" list" % path)
    return doc


# ------------------------------------------------------------------ rules

def _hhmm(text):
    if not HHMM.match(text or ""):
        raise argparse.ArgumentTypeError("use HH:MM (24h IST), for example 15:10")
    return text


def _hhmm_time(text):
    return datetime.time(*[int(p) for p in text.split(":", 1)]) if HHMM.match(text or "") else None


def _rule_from_args(args):
    if args.preopen_bell or args.opening_bell or args.close_bell:
        bell = "preopen" if args.preopen_bell else ("open" if args.opening_bell else "close")
        return {"id": args.id or ("bell-" + bell), "type": "bell", "bell": bell}
    if args.flat_by:
        at = args.at
        if not at:
            cfg = config.load(args.config)
            at = config.session_times(cfg)["flat_by"].strftime("%H:%M")
        rule = {"id": args.id or "flat-by", "type": "flat_by", "at": at,
                "label": args.label or ("flat by %s IST" % at)}
        if args.date:
            rule["date"] = args.date
        return rule
    if not args.symbol:
        raise ConfigError("--symbol is required for a price alert "
                          "(or use --from-plan, --opening-bell, --flat-by)")
    kind, price = (("above", args.above) if args.above is not None else
                   ("below", args.below) if args.below is not None else
                   ("stop", args.stop) if args.stop is not None else
                   ("target", args.target))
    if kind in ("stop", "target"):
        rid = args.id or ("%s-%s" % (args.symbol, kind))
    else:
        rid = args.id or ("%s-%s-%g" % (args.symbol, kind, price))
    rule = {"id": rid, "symbol": args.symbol, "type": kind, "price": price}
    if args.instrument_key:
        rule["instrument_key"] = args.instrument_key
    if args.note:
        rule["note"] = args.note
    if args.date:
        rule["date"] = args.date
    return rule


def _plan_rules(plan, cfg):
    """Stop, target and flat-by rules from the trader/exit.json plan."""
    symbol = plan.get("symbol") or "plan"
    base = {}
    if plan.get("symbol"):
        base["symbol"] = plan["symbol"]
    if plan.get("instrument_key"):
        base["instrument_key"] = plan["instrument_key"]
    if plan.get("date"):
        base["date"] = plan["date"]
    out = []
    if plan.get("stop") is not None:
        out.append(dict(base, id="%s-stop" % symbol, type="stop", price=plan["stop"],
                        note="plan stop"))
    if plan.get("target") is not None:
        out.append(dict(base, id="%s-target" % symbol, type="target", price=plan["target"],
                        note="plan target"))
    flat = plan.get("flat_by")
    if not flat and cfg is not None:
        flat = config.session_times(cfg)["flat_by"].strftime("%H:%M")
    if flat:
        out.append(dict(base, id="%s-flat-by" % symbol, type="flat_by", at=flat,
                        label="flat by %s IST" % flat))
    return out


def cmd_add(args):
    doc = _read_rules_doc(args.rules)
    rules = doc["rules"]
    if args.from_plan:
        if not os.path.exists(args.plan):
            raise ConfigError("no plan at %s: run trader-exit first, or add rules one by one"
                              % args.plan)
        cfg = config.load(args.config)
        new_rules = _plan_rules(_read_json(args.plan), cfg)
        if not new_rules:
            raise ConfigError("the plan at %s carries no stop, target or flat_by" % args.plan)
    else:
        new_rules = [_rule_from_args(args)]
    for rule in new_rules:
        for i, old in enumerate(rules):
            if old.get("id") == rule["id"]:
                rules[i] = rule
                break
        else:
            rules.append(rule)
    _write_json(args.rules, doc)
    for rule in new_rules:
        print("added %s" % rule["id"])
    return 0


def cmd_plan(args):
    if os.path.exists(args.plan):
        doc = _read_json(args.plan)
    else:
        doc = _read_json(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "alerts.example.json"))
    print(json.dumps(doc, indent=2))
    return 0


# ------------------------------------------------------------------ check

def _quote_key(rule):
    """The instrument key a rule is quoted on: the rule's own key, never an
    NSE_EQ|SYMBOL guess (Upstox has no such key). None when the rule has none."""
    return rule.get("instrument_key") or None


def cmd_check(args, source=None):
    cfg = config.load(args.config)
    now = timeutil.to_ist(timeutil.parse_ist(args.now) if args.now else timeutil.now_ist())
    day = now.date().isoformat()
    rules = _read_rules_doc(args.rules)["rules"]
    state = config.state_read(cfg, STATE_FILE, {"seen": {}})
    seen = state["seen"] if isinstance(state.get("seen"), dict) else {}
    flat_default = config.session_times(cfg)["flat_by"].strftime("%H:%M")
    trading = cal.is_trading_day(now.date())
    closed_note = None
    if not trading and any(r.get("type") in ("bell", "flat_by") for r in rules):
        why = cal.load_holidays().get(day) or ("weekend" if cal.is_weekend(now.date()) else "closed")
        closed_note = "NSE closed today (%s): no bells, no flat-by" % why

    fired, pending, missing, errors = [], [], [], []

    # one batch quote for every price rule that is live today
    quotes = {}
    price_rules = []
    keys = []
    for rule in rules:
        if rule.get("type") in PRICE_KINDS and not (rule.get("date") and rule["date"] != day):
            qkey = _quote_key(rule)
            if not qkey:
                if rule.get("symbol"):
                    rule["_missing_key"] = True
                    errors.append("no instrument_key for %s: add --instrument-key to the rule or arm "
                                  "it with --from-plan; the quote was not requested"
                                  % rule["symbol"])
                    continue
                rule["_qkey"] = None
                price_rules.append(rule)
                continue
            rule["_qkey"] = qkey
            price_rules.append(rule)
            if qkey not in keys:
                keys.append(qkey)
    quote_failed = False
    if keys:
        try:
            src = source if source is not None else Client(cfg)
            quotes = src.quote(keys) or {}
        except UpstoxAuthError as exc:
            quote_failed = True
            errors.append("Upstox rejected the access token (%s): re-authenticate with Upstox "
                          "to refresh the token, then run the check again" % exc)
        except (UpstoxError, OSError) as exc:
            quote_failed = True
            errors.append("quote request failed: %s" % exc)

    if closed_note:
        pending.append({"kind": "closed", "detail": closed_note})
    for rule in rules:
        kind = rule.get("type")
        rid = str(rule.get("id") or kind)
        if rule.get("date") and rule["date"] != day:
            continue
        if rule.get("_missing_key"):
            continue
        # seen key: symbol (or bell), alert kind, rule id, IST date; a new date re-arms
        key = "%s|%s|%s|%s" % (rule.get("symbol") or rule.get("bell") or "market",
                               kind, rid, day)
        if key in seen:
            continue
        if kind == "bell":
            at = BELL_TIMES.get(rule.get("bell"))
            if at is None:
                errors.append("%s: unknown bell %r, use preopen, open or close"
                              % (rid, rule.get("bell")))
                continue
            if not trading:
                continue
            if now.time() >= at:
                fired.append({"id": rid, "kind": kind, "bell": rule.get("bell"),
                              "at": timeutil.fmt_ist(now)})
                seen[key] = timeutil.fmt_ist(now)
            else:
                pending.append({"id": rid, "kind": kind, "bell": rule.get("bell"),
                                "at": at.strftime("%H:%M")})
        elif kind == "flat_by":
            if not trading:
                continue
            at_text = rule.get("at") or flat_default
            at = _hhmm_time(at_text)
            if at is None:
                errors.append("%s: bad flat-by time %r, use HH:MM" % (rid, at_text))
                continue
            if now.time() >= at:
                fired.append({"id": rid, "kind": kind, "symbol": rule.get("symbol"),
                              "label": rule.get("label") or ("flat by %s IST" % at_text),
                              "at": timeutil.fmt_ist(now)})
                seen[key] = timeutil.fmt_ist(now)
            else:
                pending.append({"id": rid, "kind": kind, "at": at_text})
        elif kind in PRICE_KINDS:
            if quote_failed:
                continue
            if rule.get("price") is None:
                errors.append("%s: the rule carries no price" % rid)
                continue
            sym = rule.get("symbol") or rule.get("_qkey") or rid
            bar = quotes.get(rule.get("_qkey")) if rule.get("_qkey") else None
            if not bar or bar.get("ltp") is None:
                missing.append({"id": rid, "symbol": sym,
                                "detail": "no quote for %s" % sym})
                continue
            ltp = bar["ltp"]
            hit = ltp >= rule["price"] if kind in ("above", "target") else ltp <= rule["price"]
            if hit:
                fired.append({"id": rid, "kind": kind, "symbol": rule.get("symbol"),
                              "price": ltp, "at": timeutil.fmt_ist(now)})
                seen[key] = timeutil.fmt_ist(now)
            else:
                pending.append({"id": rid, "kind": kind, "symbol": rule.get("symbol"),
                                "ltp": ltp})
        else:
            errors.append("%s: unknown rule type %r" % (rid, kind))

    config.state_write(cfg, STATE_FILE, {"seen": seen})
    result = {"now": timeutil.fmt_ist(now), "fired": fired, "pending": pending,
              "missing": missing, "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if closed_note:
            print(closed_note)
        for f in fired:
            what = f.get("symbol") or f.get("bell") or f.get("label") or ""
            print("RING %s %s at %s" % (f["id"], what, f["at"]))
        for m in missing:
            print(m["detail"])
    for err in errors:
        print("%s: %s" % (SKILL, err), file=sys.stderr)
    return 2 if errors else 0


# ------------------------------------------------------------------ cli

def _parser():
    p = argparse.ArgumentParser(prog="alerts.py",
                                description="IST price alerts: bells, stops, targets, flat-by")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="add a rule")
    a.add_argument("--rules", default=RULES_PATH)
    a.add_argument("--config", default=None, help="config file, for the flat-by default")
    a.add_argument("--plan", default=PLAN_PATH, help="plan for --from-plan (default trader/exit.json)")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--above", type=float, help="ring when the LTP is at or above this")
    g.add_argument("--below", type=float, help="ring when the LTP is at or below this")
    g.add_argument("--stop", type=float, help="plan stop: ring at or below this")
    g.add_argument("--target", type=float, help="plan target: ring at or above this")
    g.add_argument("--preopen-bell", action="store_true", help="ring at 09:00 IST")
    g.add_argument("--opening-bell", action="store_true", help="ring at 09:15 IST")
    g.add_argument("--close-bell", action="store_true", help="ring at 15:30 IST")
    g.add_argument("--flat-by", action="store_true", help="ring at the flat-by time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    a.add_argument("--at", type=_hhmm, help="flat-by time HH:MM IST (default: config flat_by)")
    a.add_argument("--symbol")
    a.add_argument("--instrument-key", dest="instrument_key",
                   help="Upstox instrument key, e.g. NSE_EQ|INE002A01018")
    a.add_argument("--label", help="what a flat-by alert says")
    a.add_argument("--note")
    a.add_argument("--id")
    a.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")

    c = sub.add_parser("check", help="evaluate every rule now, ring each once")
    c.add_argument("--rules", default=RULES_PATH)
    c.add_argument("--config", default=None)
    c.add_argument("--now", help="pretend it is this IST time")
    c.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="print the plan the alerts follow")
    pl.add_argument("--plan", default=PLAN_PATH)
    return p


def main(argv=None, source=None):
    """CLI entry. source stands in for the Upstox client in tests: it only
    needs .quote(keys) -> {instrument_key: {"ltp", ...}}."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        if args.cmd == "add":
            return cmd_add(args)
        if args.cmd == "plan":
            return cmd_plan(args)
        return cmd_check(args, source)
    except ConfigError as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
