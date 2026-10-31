"""Upstox v3 place-order payloads, client-side validation, and two-leg plans.

The place payload uses Upstox's documented v3 field names exactly; nothing is
renamed and no bracket-order field exists. Everything here is client side: the
network seam lives in trader_core.broker.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

ORDER_PRODUCTS = ("I", "D", "MTF")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
TRANSACTION_TYPES = ("BUY", "SELL")
VALIDITY = ("DAY", "IOC")

# The v3 place-order body, field for field. Non-relevant keys are omitted
# (price on MARKET, trigger_price on LIMIT, market_protection on LIMIT/SL).
PAYLOAD_FIELDS = ("quantity", "product", "validity", "price", "instrument_token",
                  "order_type", "transaction_type", "disclosed_quantity",
                  "trigger_price", "is_amo", "slice", "market_protection", "tag")

TAG_MAX = 40
WINDOW_OPEN = (5, 30)  # the Upstox order APIs answer 05:30 to 24:00 IST only

_ROUNDS = {"nearest": ROUND_HALF_UP, "up": ROUND_CEILING, "down": ROUND_FLOOR}


class PlanError(ValueError):
    """A client-side order or plan validation failure. Never a network error."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError("%s must be a number, got %r" % (name, value))
    return float(value)


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError("%s must be an integer, got %r" % (name, value))
    if value <= 0:
        raise PlanError("%s must be positive, got %r" % (name, value))
    return value


def validate_payload(payload):
    """Validate a v3 place-order body; raises PlanError. leg() builds payloads
    that already pass, so this guards hand-built bodies reaching broker.place."""
    if not isinstance(payload, dict):
        raise PlanError("place payload must be a dict, got %r" % (payload,))
    unknown = sorted(set(payload) - set(PAYLOAD_FIELDS))
    if unknown:
        raise PlanError("unknown place-order field(s): %s" % ", ".join(unknown))
    order_type = payload.get("order_type")
    if order_type not in ORDER_TYPES:
        raise PlanError("order_type must be one of %s, got %r" % (", ".join(ORDER_TYPES), order_type))
    if payload.get("transaction_type") not in TRANSACTION_TYPES:
        raise PlanError("transaction_type must be one of %s, got %r"
                        % (", ".join(TRANSACTION_TYPES), payload.get("transaction_type")))
    if payload.get("product") not in ORDER_PRODUCTS:
        raise PlanError("product must be one of %s, got %r" % (", ".join(ORDER_PRODUCTS), payload.get("product")))
    if payload.get("validity") not in VALIDITY:
        raise PlanError("validity must be one of %s, got %r" % (", ".join(VALIDITY), payload.get("validity")))
    _positive_int(payload.get("quantity"), "quantity")
    key = payload.get("instrument_token")
    if not isinstance(key, str) or not key.strip():
        raise PlanError("instrument_token must be a non-empty string, got %r" % (key,))
    tag = payload.get("tag")
    if tag is not None and (not isinstance(tag, str) or len(tag) > TAG_MAX):
        raise PlanError("tag must be a string of at most %d characters, got %r" % (TAG_MAX, tag))
    if order_type in ("MARKET", "SL-M"):
        _validate_market_protection(payload.get("market_protection"))
    else:
        if _number(payload.get("price"), "price") <= 0:
            raise PlanError("%s requires a positive price, got %r" % (order_type, payload.get("price")))
    if order_type in ("SL", "SL-M"):
        if _number(payload.get("trigger_price"), "trigger_price") <= 0:
            raise PlanError("%s requires a positive trigger_price, got %r"
                            % (order_type, payload.get("trigger_price")))
    dq = payload.get("disclosed_quantity", 0)
    if isinstance(dq, bool) or not isinstance(dq, int) or dq < 0:
        raise PlanError("disclosed_quantity must be a non-negative integer, got %r" % (dq,))


def _validate_market_protection(value):
    """MARKET and SL-M: 1..25 percent, or -1 for the Upstox default; 0 is
    rejected outright (Upstox rejects API market orders without protection)."""
    if isinstance(value, bool) or value == "-1":
        value = -1 if value == "-1" else value
    if value == "-1":
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError("market_protection must be an integer 1..25 or -1, got %r" % (value,))
    if not 1 <= value <= 25 and value != -1:
        raise PlanError("market_protection must be 1..25 or -1 (0 is rejected by the exchange), got %r" % (value,))


def leg(instrument_key, transaction_type, quantity, order_type, product="I", price=None,
        trigger_price=None, market_protection=0, tag=None, validity="DAY",
        is_amo=False, slice=False, disclosed_quantity=0):
    """Build and validate one v3 place-order body. Payload keys use the
    documented Upstox names exactly; market_protection -1 is sent as "-1"."""
    payload = {
        "quantity": _positive_int(quantity, "quantity"),
        "product": product,
        "validity": validity,
        "instrument_token": instrument_key,
        "order_type": order_type,
        "transaction_type": transaction_type,
        "disclosed_quantity": disclosed_quantity,
        "is_amo": bool(is_amo),
        "slice": bool(slice),
    }
    if price is not None:
        payload["price"] = price
    if trigger_price is not None:
        payload["trigger_price"] = trigger_price
    if order_type in ("MARKET", "SL-M"):
        payload["market_protection"] = "-1" if market_protection == -1 else market_protection
    if tag is not None:
        payload["tag"] = tag
    validate_payload(payload)
    return payload


def plan(entry_leg, stop_leg, target_leg, symbol=None):
    """Bundle the three legs into a trade plan. Each stored leg is a copy with
    its position role: entry, stop, target. Missing legs stay missing so
    validate_plan can report them; nothing here prices the trade."""
    roles = ("entry", "stop", "target")
    legs = []
    for role, one in zip(roles, (entry_leg, stop_leg, target_leg)):
        if one is None:
            legs.append(None)
            continue
        stored = dict(one)
        stored["role"] = role
        legs.append(stored)
    entry = entry_leg if isinstance(entry_leg, dict) else {}
    if symbol is None:
        symbol = entry.get("symbol")
    return {
        "legs": legs,
        "symbol": symbol,
        "instrument_key": entry.get("instrument_token") or entry.get("instrument_key"),
        "product": entry.get("product", "I"),
        "quantity": entry.get("quantity"),
        "direction": "short" if entry.get("transaction_type") == "SELL" else "long",
    }


def on_grid(price, tick):
    """True when price sits exactly on the tick grid (Decimal arithmetic)."""
    tick_d = Decimal(str(tick))
    if tick_d <= 0:
        return True
    units = Decimal(str(price)) / tick_d
    return units == units.to_integral_value()


def round_tick(price, tick, mode="nearest"):
    """Snap price onto the tick grid: mode nearest, up, or down. Decimal-safe
    so 1193.65 with tick 0.05 comes back as exactly 1193.65, not 1193.6499..."""
    if mode not in _ROUNDS:
        raise PlanError("round_tick mode must be one of %s, got %r" % (", ".join(sorted(_ROUNDS)), mode))
    tick_d = Decimal(str(tick))
    if tick_d <= 0:
        raise PlanError("tick must be positive, got %r" % (tick,))
    units = (Decimal(str(price)) / tick_d).quantize(Decimal("1"), rounding=_ROUNDS[mode])
    return float(units * tick_d)


def _exit_price(leg_):
    """The price a protective leg acts at: the trigger for stops, the limit
    price for a LIMIT stop or target."""
    if leg_.get("trigger_price") is not None:
        return leg_["trigger_price"]
    return leg_.get("price")


def validate_plan(plan_, instrument, cfg):
    """Check a two-leg plan as a set. Returns a list of reason strings; empty
    means the plan is tradable. instrument is a trader_core.instruments
    normalized row (lot_size, tick_size, freeze_quantity)."""
    reasons = []
    legs = plan_.get("legs") or []
    by_role = {}
    for one in legs:
        if isinstance(one, dict) and one.get("role"):
            by_role.setdefault(one["role"], []).append(one)
    for role in ("entry", "stop", "target"):
        if len(by_role.get(role, [])) > 1:
            reasons.append("duplicate %s leg: only one leg may carry the %s role" % (role, role))
        elif not by_role.get(role):
            reasons.append("missing %s leg" % role)
    if reasons:
        return reasons

    entry, stop, target = (by_role["entry"][0], by_role["stop"][0], by_role["target"][0])
    direction = plan_.get("direction", "long")
    account = (cfg or {}).get("account") or {}
    if direction == "short" and account.get("long_only", True):
        reasons.append("short plans are blocked: this skill is long only")
        return reasons
    entry_side = "BUY" if direction == "long" else "SELL"
    exit_side = "SELL" if direction == "long" else "BUY"
    for role, leg_ in (("entry", entry), ("stop", stop), ("target", target)):
        expected = entry_side if role == "entry" else exit_side
        if leg_.get("transaction_type") != expected:
            reasons.append("wrong side: %s leg must be %s for a %s plan, got %s"
                           % (role, expected, direction, leg_.get("transaction_type")))
    if reasons:
        # sides decide what the rest of the checks even mean
        return reasons
    if stop.get("order_type") not in ("SL", "SL-M", "LIMIT"):
        reasons.append("stop leg must be SL, SL-M, or LIMIT, got %s" % stop.get("order_type"))
    if target.get("order_type") != "LIMIT":
        reasons.append("target leg must be LIMIT, got %s" % target.get("order_type"))

    lot = instrument.get("lot_size")
    if lot:
        for role, leg_ in (("entry", entry), ("stop", stop), ("target", target)):
            if leg_.get("quantity", 0) % lot != 0:
                reasons.append("%s quantity %s is not a whole multiple of lot_size %s"
                               % (role, leg_.get("quantity"), lot))
    freeze = instrument.get("freeze_quantity")
    if freeze:
        for role, leg_ in (("entry", entry), ("stop", stop), ("target", target)):
            if leg_.get("quantity", 0) > freeze:
                reasons.append("%s quantity %s is above the freeze quantity %s"
                               % (role, leg_.get("quantity"), freeze))
    tick = instrument.get("tick_size")
    if tick:
        for role, leg_ in (("entry", entry), ("stop", stop), ("target", target)):
            for field in ("price", "trigger_price"):
                value = leg_.get(field)
                if value is not None and not on_grid(value, tick):
                    reasons.append("%s %s %s is not on the %s tick grid" % (role, field, value, tick))

    entry_price = entry.get("price")
    if entry_price and direction == "long":
        stop_price = _exit_price(stop)
        target_price = _exit_price(target)
        if stop_price is not None:
            if stop_price > entry_price:
                reasons.append("stop %s must be at or below the entry %s for a long plan"
                               % (stop_price, entry_price))
            elif stop_price == entry_price:
                reasons.append("stop %s must be below the entry %s, not equal to it"
                               % (stop_price, entry_price))
        if target_price is not None and target_price <= entry_price:
            reasons.append("target %s must be above the entry %s for a long plan"
                           % (target_price, entry_price))
    products = (cfg or {}).get("products") or {}
    if products.get("default") == "I" and plan_.get("product") != "I":
        reasons.append("product %s is not allowed: the config restricts trading to MIS (I) intraday equity"
                       % plan_.get("product"))
    return reasons
