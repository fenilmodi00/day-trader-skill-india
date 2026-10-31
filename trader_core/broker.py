"""Broker seam: the Broker protocol and the Upstox v2/v3 implementation.

Sandbox is the default and live trading is fail closed: config.assert_live_allowed
runs before any live request. No order goes out without the user's explicit yes
(the broker skill owns that gate; this module only enforces the machine gates).
"""

import datetime
from typing import Protocol

from trader_core import config, orders
from trader_core.http import SANDBOX, UpstoxAuthError, UpstoxError  # noqa: F401 (UpstoxAuthError re-exported)
from trader_core.timeutil import now_ist

PLACE_PATH = "/v3/order/place"
MODIFY_PATH = "/v2/order/modify"
CANCEL_PATH = "/v2/order/cancel/"
ORDER_BOOK_PATH = "/v2/order/retrieve-all"
TRADES_PATH = "/v2/order/trades/get-trades-for-day"
POSITIONS_PATH = "/v2/portfolio/short-term-positions"
HOLDINGS_PATH = "/v2/portfolio/long-term-holdings"
FUNDS_PATH = "/v2/user/get-funds-and-margin"
MARGINS_PATH = "/v2/charges/margin"
ORDERS_STATE = "orders.json"


class Broker(Protocol):
    """The seam every broker backend implements. Order ids and statuses in,
    plain dicts and lists out."""

    def place(self, payload, retries=0):
        ...

    def modify(self, order_id, payload):
        ...

    def cancel(self, order_id):
        ...

    def orders(self):
        ...

    def trades(self):
        ...

    def positions(self):
        ...

    def holdings(self):
        ...

    def funds(self):
        ...

    def margins(self, keys):
        ...


def assert_open_window(now=None):
    """Upstox serves the order APIs 05:30 to 24:00 IST; outside that window
    nothing is sent. Raises UpstoxError with the current IST time."""
    dt = now or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.timeutil.IST)
    if dt.time() < datetime.time(*orders.WINDOW_OPEN):
        raise UpstoxError(
            "the Upstox order APIs are reachable 05:30 to 24:00 IST only; it is %s IST now, so no"
            " order request was sent" % dt.strftime("%H:%M"))


def _data(envelope):
    """Unwrap the UDAPI envelope: the data payload when present, else the
    envelope itself."""
    if isinstance(envelope, dict) and "data" in envelope:
        return envelope["data"]
    return envelope


class UpstoxBroker:
    """Upstox backend for the Broker protocol. Trading uses the client's base
    host (sandbox in sandbox mode); live mode is gated by config first."""

    def __init__(self, client=None, cfg=None, transport=None):
        if client is None:
            from trader_core.upstox import Client
            client = Client(cfg=cfg, transport=transport)
        self.client = client
        self.cfg = cfg if isinstance(cfg, dict) else {}

    # ---- gates ----

    def _assert_live_allowed(self):
        """Fail closed before any live request: live mode needs the stored
        go-live confirmation, sandbox mode passes through."""
        config.assert_live_allowed(self.cfg)

    # ---- order actions ----

    def place(self, payload, retries=0):
        """Validate, guard, then POST the v3 place body. A tag already
        recorded in trader/state/orders.json returns the first result with
        duplicate True and makes no second HTTP call. retries is accepted for
        seam compatibility but a token expiry never retries."""
        orders.validate_payload(payload)
        assert_open_window()
        self._assert_live_allowed()
        tag = payload.get("tag")
        state = config.state_read(self.cfg, ORDERS_STATE, {}) or {}
        if tag and tag in state:
            prior = state[tag]
            return {"order_id": prior.get("order_id"), "status": prior.get("status"),
                    "payload": payload, "duplicate": True}
        envelope = self.client.request("POST", PLACE_PATH, payload=payload)
        data = _data(envelope)
        if not isinstance(data, dict):
            data = {}
        order_id = data.get("order_id")
        order_ids = data.get("order_ids")
        if not order_id and isinstance(order_ids, list) and order_ids:
            order_id = order_ids[0]
        status = data.get("order_status") or (envelope.get("status") if isinstance(envelope, dict) else None) or "open"
        result = {"order_id": order_id, "status": status, "payload": payload, "duplicate": False}
        if tag:
            state[tag] = {"order_id": order_id, "status": status}
            config.state_write(self.cfg, ORDERS_STATE, state)
        return result

    def modify(self, order_id, payload):
        """PUT the v2 modify body; the order id rides in the body."""
        assert_open_window()
        self._assert_live_allowed()
        body = dict(payload or {})
        body["order_id"] = order_id
        return _data(self.client.request("PUT", MODIFY_PATH, payload=body))

    def cancel(self, order_id):
        assert_open_window()
        self._assert_live_allowed()
        return _data(self.client.request("DELETE", CANCEL_PATH + str(order_id)))

    # ---- reads ----

    def orders(self):
        self._assert_live_allowed()
        return _data(self.client.request("GET", ORDER_BOOK_PATH))

    def trades(self):
        self._assert_live_allowed()
        return _data(self.client.request("GET", TRADES_PATH))

    def positions(self):
        self._assert_live_allowed()
        return _data(self.client.request("GET", POSITIONS_PATH))

    def holdings(self):
        self._assert_live_allowed()
        return _data(self.client.request("GET", HOLDINGS_PATH))

    def funds(self):
        """Raw funds-and-margin envelope; no balance is invented here."""
        self._assert_live_allowed()
        return self.client.request("GET", FUNDS_PATH)

    def margins(self, keys):
        """Required margin for the given instrument keys, raw response dict."""
        self._assert_live_allowed()
        body = {"instruments": [{"instrument_key": str(key)} for key in (keys or [])]}
        return self.client.request("POST", MARGINS_PATH, payload=body)
