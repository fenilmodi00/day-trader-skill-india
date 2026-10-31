"""Upstox instrument master: load, index, find, normalize."""

import gzip
import json


class InstrumentError(ValueError):
    """A symbol is unknown or the instrument master is malformed."""


def _to_bytes(text_or_bytes):
    if isinstance(text_or_bytes, str):
        # default_transport decodes bytes with surrogateescape, so this round trips
        # gzip bytes exactly while leaving plain text untouched.
        return text_or_bytes.encode("utf-8", "surrogateescape")
    return bytes(text_or_bytes)


def _parse_doc(data):
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise InstrumentError("instrument master gzip is corrupt: %s" % exc)
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstrumentError("instrument master is not valid JSON: %s" % exc)
    if isinstance(doc, dict):
        doc = doc.get("data", doc.get("instruments"))
    if not isinstance(doc, list) or not doc:
        raise InstrumentError("instrument master must be a non-empty JSON array or {\"data\": [...]}")
    return doc


def _first(row, *names):
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _number(row, *names):
    value = _first(row, *names)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise InstrumentError("instrument %s has a bad %s: %r"
                              % (row.get("trading_symbol"), "/".join(names), value))


def normalize(row):
    """Map a master row (or an already normalized row) to the pinned field set."""
    if not isinstance(row, dict):
        raise InstrumentError("instrument row must be a JSON object")
    symbol = _first(row, "trading_symbol", "tradingsymbol", "symbol")
    if not symbol:
        raise InstrumentError("instrument row is missing trading_symbol")
    key = _first(row, "instrument_key")
    if not key:
        raise InstrumentError("instrument %s is missing instrument_key" % symbol)
    expiry = _first(row, "expiry")
    if isinstance(expiry, str):
        expiry = expiry[:10]
    return {
        "instrument_key": str(key),
        "exchange_token": _first(row, "exchange_token", "token"),
        "trading_symbol": str(symbol),
        "isin": _first(row, "isin"),
        "instrument_type": _first(row, "instrument_type"),
        "lot_size": _number(row, "lot_size"),
        "tick_size": _number(row, "tick_size"),
        "freeze_quantity": _number(row, "freeze_quantity"),
        "expiry": expiry,
        "strike_price": _number(row, "strike_price", "strike"),
        "underlying_key": _first(row, "underlying_key", "underlying_instrument_key"),
    }


def load(text_or_bytes):
    """Parse an instrument master (JSON array, {"data": [...]}, plain or .json.gz bytes)."""
    doc = _parse_doc(_to_bytes(text_or_bytes))
    return [normalize(row) for row in doc]


def is_equity(row):
    key = row.get("instrument_key") or ""
    return key.startswith("NSE_EQ|") or row.get("instrument_type") == "EQ"


def index(rows):
    """Map trading_symbol to row, preferring the NSE_EQ row when a symbol appears twice."""
    out = {}
    for row in rows:
        symbol = row.get("trading_symbol")
        if not symbol:
            continue
        current = out.get(symbol)
        if current is None or (is_equity(row) and not is_equity(current)):
            out[symbol] = row
    return out


def find(rows, symbol, exchange="NSE_EQ"):
    """Normalized row for symbol on exchange, InstrumentError when absent."""
    wanted = str(symbol).strip().upper()
    if not wanted:
        raise InstrumentError("empty instrument symbol")
    prefix = exchange + "|"
    fallback = None
    for row in rows:
        if str(row.get("trading_symbol", "")).upper() != wanted:
            continue
        if str(row.get("instrument_key", "")).startswith(prefix):
            return normalize(row)
        if fallback is None:
            fallback = row
    if fallback is None:
        raise InstrumentError("unknown instrument: %s" % symbol)
    return normalize(fallback)


def key(rows, symbol, exchange="NSE_EQ"):
    """The instrument_key for symbol on exchange, InstrumentError when absent.
    Fail closed: NSE_EQ|SYMBOL is not a valid Upstox key, so callers must never
    invent one when the master has no row."""
    return find(rows, symbol, exchange)["instrument_key"]
