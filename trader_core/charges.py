"""INR charge model for intraday equity on Upstox.

Every rate comes from config.charges_cfg so no rate is hard-coded here and in
the user config at the same time. Heads: brokerage, sell-side STT, exchange
transaction charge, SEBI turnover fee, buy-side stamp duty, and GST on
brokerage plus exchange plus SEBI.
"""

from trader_core import config
from trader_core.orders import PlanError

CHARGE_HEADS = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst")


def _rates(cfg):
    return config.charges_cfg(cfg) if cfg else dict(config.DEFAULTS["charges"])


def charges(side, price, quantity, cfg=None):
    """Per-head INR cost of one intraday order, each rounded to 2 decimals.
    STT is charged on the sell side only; stamp duty on the buy side only.
    GST applies to brokerage, the exchange charge, and the SEBI fee."""
    if side not in ("BUY", "SELL"):
        raise PlanError("side must be BUY or SELL, got %r" % (side,))
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise PlanError("quantity must be a positive integer, got %r" % (quantity,))
    rates = _rates(cfg)
    turnover = price * quantity
    bps_brokerage = turnover * rates["brokerage_bps"] / 10000.0
    brokerage = min(max(bps_brokerage, 0.0), float(rates["brokerage_per_order_inr"]))
    stt = (turnover * rates["stt_intraday_sell_bps"] / 10000.0) if side == "SELL" else 0.0
    exchange = turnover * rates["exchange_bps"] / 10000.0
    sebi = turnover * rates["sebi_bps"] / 10000.0
    stamp = (turnover * rates["stamp_bps"] / 10000.0) if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * rates["gst_pct"] / 100.0
    out = {"side": side, "turnover": round(turnover, 2)}
    for name, value in (("brokerage", brokerage), ("stt", stt), ("exchange", exchange),
                        ("sebi", sebi), ("stamp", stamp), ("gst", gst)):
        out[name] = max(round(value, 2), 0.0)
    out["total"] = round(sum(out[name] for name in CHARGE_HEADS), 2)
    return out


def round_trip(entry, exit_price, quantity, cfg=None):
    """Both sides of one round trip: charges per side, total charges, and the
    gross and net P&L (gross minus charges)."""
    buy = charges("BUY", entry, quantity, cfg)
    sell = charges("SELL", exit_price, quantity, cfg)
    total = round(buy["total"] + sell["total"], 2)
    gross = round((exit_price - entry) * quantity, 2)
    return {
        "buy": buy,
        "sell": sell,
        "total_charges": total,
        "gross_pnl": gross,
        "net_pnl": round(gross - total, 2),
    }
