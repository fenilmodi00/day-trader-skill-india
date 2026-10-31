"""Config, fail-closed live gate, and atomic JSON state for the trader_core package.

Config is JSON only and never carries a token value: tokens come from the
environment, and ENV only maps logical names to env var names.
"""

import copy
import datetime
import hashlib
import json
import os

ENV = {
    "api_key": "UPSTOX_API_KEY",
    "api_secret": "UPSTOX_API_SECRET",
    "access_token": "UPSTOX_ACCESS_TOKEN",
    "sandbox_token": "UPSTOX_SANDBOX_TOKEN",
    "analytics_token": "UPSTOX_ANALYTICS_TOKEN",
}
LIVE_PHRASE = "GO LIVE WITH UPSTOX"
CONFIRM_STATE = "live_confirmation.json"
DEFAULT_PATH = "trader/config.json"
PLAN_PATH = "trader/plan.json"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODES = ("sandbox", "live")
ORDER_PRODUCTS = ("I", "D", "MTF")

DEFAULTS = {
    "broker": "upstox",
    "mode": "sandbox",
    "state_dir": "",
    "account": {
        "budget_inr": 50000.0,
        "max_loss_per_trade_inr": 250.0,
        "max_daily_loss_inr": 500.0,
        "max_trades_per_day": 3,
        "max_orders_per_day": 12,
        "min_reward_risk": 1.0,
        "long_only": True,
    },
    "session": {
        "open": "09:15",
        "entry_start": "09:30",
        "entry_cutoff": "15:00",
        "flat_by": "15:10",
        "close": "15:30",
        "mis_square_off": "15:25",
        "mis_square_off_cas": "15:10",
    },
    "universe": {
        "symbols": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "ITC"],
        "watchlist": [],
        "nifty500": False,
        "series": "EQ",
    },
    "charges": {
        "brokerage_bps": 5.0,
        "brokerage_per_order_inr": 20.0,
        "stt_intraday_sell_bps": 2.5,
        "exchange_bps": 0.297,
        "sebi_bps": 0.01,
        "stamp_bps": 0.3,
        "gst_pct": 18.0,
    },
    "data": {
        "tick_default": "1m",
        "quote_batch": 50,
        "rate_limit_per_sec": 10,
        "timeout_s": 30,
    },
    "products": {
        "default": "I",
    },
    "risk": {
        "mis_products": ["I"],
        "blocked_symbols": [],
        "asm_symbols": [],
        "gsm_symbols": [],
        "ban_symbols": [],
    },
}


class ConfigError(Exception):
    """Bad or disallowed configuration."""


class LiveGateError(ConfigError):
    """Live trading requested without an explicit, confirmed go-live."""


def default_path():
    """Absolute default location of the user config file."""
    return os.path.join(REPO_ROOT, DEFAULT_PATH)


def _deep_merge(base, over):
    """Merge user values over the defaults: dicts merge, lists and scalars replace."""
    for key, value in over.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def validate(cfg):
    """Raise ConfigError on bad types or values; return cfg untouched."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a JSON object")
    for key in DEFAULTS:
        if key not in cfg:
            raise ConfigError("missing config key: %s" % key)
    for key in cfg:
        if key not in DEFAULTS:
            raise ConfigError("unknown config key: %s" % key)
    if cfg.get("broker") != "upstox":
        raise ConfigError("broker must be \"upstox\", got %r" % (cfg.get("broker"),))
    if cfg.get("mode") not in MODES:
        raise ConfigError("mode must be one of %s (got %r); refusing to guess" % (", ".join(MODES), cfg.get("mode")))
    if not isinstance(cfg.get("state_dir"), str):
        raise ConfigError("state_dir must be a string")

    session = cfg["session"]
    for key in DEFAULTS["session"]:
        raw = session.get(key)
        if not isinstance(raw, str):
            raise ConfigError("session.%s must be a \"HH:MM\" string" % key)
        try:
            datetime.time.fromisoformat(raw)
        except ValueError:
            raise ConfigError("session.%s is not a valid HH:MM time: %r" % (key, raw))

    account = cfg["account"]
    for key in ("budget_inr", "max_loss_per_trade_inr", "max_daily_loss_inr"):
        value = account.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError("account.%s must be a positive number, got %r" % (key, value))
    for key in ("max_trades_per_day", "max_orders_per_day"):
        value = account.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError("account.%s must be a positive integer, got %r" % (key, value))
    reward = account.get("min_reward_risk")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward < 0:
        raise ConfigError("account.min_reward_risk must be a non-negative number, got %r" % (reward,))
    if not isinstance(account.get("long_only"), bool):
        raise ConfigError("account.long_only must be a boolean")

    universe = cfg["universe"]
    for key in ("symbols", "watchlist"):
        value = universe.get(key)
        if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
            raise ConfigError("universe.%s must be a list of strings" % key)
    if not isinstance(universe.get("nifty500"), bool):
        raise ConfigError("universe.nifty500 must be a boolean")
    if not isinstance(universe.get("series"), str) or not universe["series"]:
        raise ConfigError("universe.series must be a non-empty string")

    for key, value in cfg["charges"].items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError("charges.%s must be a non-negative number, got %r" % (key, value))

    data = cfg["data"]
    if data.get("tick_default") not in ("1m", "30m", "day", "week", "month"):
        raise ConfigError("data.tick_default must be one of 1m, 30m, day, week, month")
    for key in ("quote_batch", "rate_limit_per_sec", "timeout_s"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError("data.%s must be a positive number, got %r" % (key, value))

    if cfg["products"].get("default") not in ORDER_PRODUCTS:
        raise ConfigError("products.default must be one of %s" % ", ".join(ORDER_PRODUCTS))
    for key, value in cfg["risk"].items():
        if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
            raise ConfigError("risk.%s must be a list of strings" % key)
    return cfg


def load(path=None):
    """DEFAULTS deep-merged with the JSON file at path, then validated.

    A missing file is fine: the sandbox defaults stand. A present but unreadable
    or invalid file raises ConfigError.
    """
    if path is None:
        path = default_path()
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ConfigError("cannot read config %s: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("config must be a JSON object")
        _deep_merge(cfg, user)
    return validate(cfg)


def mode(cfg):
    """Return "sandbox" or "live"; anything else fails closed."""
    value = cfg.get("mode")
    if value not in MODES:
        raise ConfigError("mode must be one of %s (got %r)" % (", ".join(MODES), value))
    return value


def is_live(cfg):
    return mode(cfg) == "live"


def _phrase_sha():
    return hashlib.sha256(LIVE_PHRASE.encode("utf-8")).hexdigest()


def _confirm_path(cfg):
    return os.path.join(state_dir(cfg), CONFIRM_STATE)


def assert_live_allowed(cfg):
    """Raise LiveGateError unless mode is live AND the stored confirmation hash matches."""
    if not is_live(cfg):
        return
    record = state_read(cfg, CONFIRM_STATE)
    if not isinstance(record, dict) or record.get("sha256") != _phrase_sha():
        raise LiveGateError(
            "live mode is gated: type the exact phrase %r in the live confirmation step" % LIVE_PHRASE
        )


def confirm_live(cfg, typed):
    """Verify the typed phrase and store its sha256 atomically; returns the file path."""
    if typed != LIVE_PHRASE:
        raise LiveGateError("live confirmation rejected: the typed phrase does not match")
    record = {
        "sha256": _phrase_sha(),
        "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return state_write(cfg, CONFIRM_STATE, record)


def state_dir(cfg):
    """Absolute state directory: cfg["state_dir"] when set, else <repo>/trader/state."""
    value = cfg.get("state_dir") or ""
    if not value:
        value = os.path.join("trader", "state")
    if not os.path.isabs(value):
        value = os.path.join(REPO_ROOT, value)
    return os.path.abspath(value)


def _state_path(cfg, name):
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        raise ConfigError("state name must be a plain file name: %r" % (name,))
    return os.path.join(state_dir(cfg), name)


def state_read(cfg, name, default=None):
    """Read a JSON state file; missing or corrupt returns default, never raises."""
    path = _state_path(cfg, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def state_write(cfg, name, obj):
    """Write a JSON state file atomically (temp file in the same dir, then os.replace)."""
    path = _state_path(cfg, name)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def _times(cfg):
    out = {}
    for key in DEFAULTS["session"]:
        raw = cfg["session"].get(key)
        try:
            out[key] = datetime.time.fromisoformat(raw)
        except (TypeError, ValueError):
            raise ConfigError("session.%s is not a valid HH:MM time: %r" % (key, raw))
    return out


def session_times(cfg):
    """Session cutoffs as datetime.time values."""
    return _times(cfg)


def limits(cfg):
    """The INR risk limits from cfg["account"]."""
    return cfg["account"]


def charges_cfg(cfg):
    """Cost model parameters from cfg["charges"]."""
    return cfg["charges"]


def universe_cfg(cfg):
    """Tradables from cfg["universe"]."""
    return cfg["universe"]
