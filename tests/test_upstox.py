"""T2: Upstox data client, instruments master, and resampling, all offline."""

import datetime
import gzip
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _load import FIX, core, load

core()  # put the repo root on sys.path
from trader_core.http import (SandboxUnsupported, UpstoxAPIError, UpstoxAuthError, UpstoxError,
                              UpstoxRateLimit, parse_udapi)  # noqa: E402
from trader_core.instruments import InstrumentError  # noqa: E402
from trader_core.upstox import LIVE, SANDBOX, Client, resample  # noqa: E402

FX = os.path.join(FIX, "upstox")
KEY = "NSE_EQ|INE002A01018"
REL = "NSE_EQ|RELIANCE"


def read_json(name):
    with open(os.path.join(FX, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def as_body(raw):
    """Pretend bytes that went through default_transport's utf-8 surrogateescape."""
    return raw.decode("utf-8", "surrogateescape")


class Transport:
    """Fake transport: handler(method, url) -> (status, body_str)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, payload=None, headers=None, timeout=30):
        self.calls.append({"method": method, "url": url, "payload": payload,
                           "headers": headers or {}, "timeout": timeout})
        return self.handler(method, url)


def candles_handler(body):
    def handler(method, url):
        if "/candles/intraday/" in url.replace("historical-candle", "candles"):
            raise AssertionError("intraday endpoint hit from candles(): " + url)
        return 200, json.dumps(body)
    return handler


def intraday_handler(body):
    def handler(method, url):
        return 200, json.dumps(body)
    return handler


class ResampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FX, "RELIANCE-1m-2026-10-26.json"), "r", encoding="utf-8") as fh:
            rows = json.load(fh)["data"]["candles"]
        cls.bars = _normalize_fixture(rows)

    def test_full_session_resamples_to_75_bars(self):
        out = resample(self.bars, 5)
        self.assertEqual(len(out), 75)
        self.assertEqual(out[0]["dt"], datetime.datetime(2026, 10, 26, 9, 15, tzinfo=core().IST))
        self.assertEqual(out[-1]["dt"], datetime.datetime(2026, 10, 26, 15, 25, tzinfo=core().IST))
        stamps = [bar["dt"].strftime("%H:%M") for bar in out]
        self.assertEqual(stamps[0], "09:15")
        self.assertEqual(stamps[-1], "15:25")
        self.assertEqual(stamps[1], "09:20")

    def test_resampled_regression_numbers(self):
        out = {bar["dt"].strftime("%H:%M"): bar for bar in resample(self.bars, 5)}
        first = out["09:15"]
        self.assertEqual(first["o"], 1186.1)
        self.assertEqual(first["c"], 1183.3)  # last close inside the bucket wins
        self.assertEqual(out["09:20"]["o"], 1183.1)  # the next bucket opens at its own open
        self.assertEqual(out["09:20"]["l"], 1176.8)  # the 09:23 dip starts the range low
        self.assertAlmostEqual(out["09:55"]["c"], 1193.6)
        # opening range 09:15-09:30 is the first three buckets
        first3 = resample(self.bars, 5)[:3]
        self.assertAlmostEqual(max(bar["h"] for bar in first3), 1192.0)
        self.assertAlmostEqual(min(bar["l"] for bar in first3), 1175.3)

    def test_partial_trailing_bucket_is_dropped(self):
        out = resample(self.bars[:-2], 5)  # feed ends at 15:27
        self.assertEqual(len(out), 74)
        self.assertEqual(out[-1]["dt"].strftime("%H:%M"), "15:20")

    def test_late_start_and_gap_buckets_are_dropped(self):
        bars = [bar for bar in self.bars if bar["dt"].time() >= datetime.time(9, 17)]
        out = resample(bars, 5)
        self.assertEqual(len(out), 74)
        stamps = [bar["dt"].strftime("%H:%M") for bar in out]
        self.assertNotIn("09:15", stamps)  # the first bucket lost two minutes, it is gone
        self.assertEqual(stamps[0], "09:20")
        # a mid-session gap empties its bucket too
        bars = [bar for bar in self.bars if bar["dt"].time() < datetime.time(9, 36)
                or bar["dt"].time() > datetime.time(9, 38)]
        out = resample(bars, 5)
        self.assertNotIn("09:35", [bar["dt"].strftime("%H:%M") for bar in out])
        self.assertIn("09:40", [bar["dt"].strftime("%H:%M") for bar in out])

    def test_empty_bucket_is_dropped_and_none_volumes_sum(self):
        bars = [bar for bar in self.bars if bar["dt"].time() != datetime.time(9, 22)]
        for bar in bars:
            bar["v"] = None
        out = resample(bars, 5)
        self.assertEqual(len(out), 74)  # the 09:20 bucket lost a minute, it is gone
        third = [bar for bar in out if bar["dt"].strftime("%H:%M") == "09:25"][0]
        self.assertIsNone(third["v"])  # all its minutes were None, nothing was summed

    def test_bad_minutes_are_rejected(self):
        for minutes in (0, -5, 7, 2.5, "5"):
            with self.assertRaises(UpstoxError):
                resample(self.bars, minutes)

    def test_15m_resample_spans_three_buckets(self):
        out = resample(self.bars, 15)
        self.assertEqual(len(out), 25)
        self.assertEqual(out[0]["dt"].strftime("%H:%M"), "09:15")
        self.assertEqual(out[-1]["dt"].strftime("%H:%M"), "15:15")


def _normalize_fixture(rows):
    """Turn recorded [ts,o,h,l,c,v,oi] rows into pinned bars without the network."""
    client = Client({}, token="test-tok")
    transport = Transport(lambda method, url: (200, json.dumps(
        {"status": "success", "data": {"candles": rows}})))
    client._transport = transport
    return client.candles(KEY, "1m", start="2026-10-26", end="2026-10-26")


class CandlesTest(unittest.TestCase):
    def test_seeded_fixture_becomes_75_bars(self):
        client = Client({}, token="test-tok")
        transport = Transport(candles_handler(read_json("RELIANCE-1m-2026-10-26.json")))
        client._transport = transport
        bars = client.candles(KEY, "1m", start="2026-10-26", end="2026-10-26")
        self.assertEqual(len(bars), 375)
        resampled = resample(bars, 5)
        self.assertEqual(len(resampled), 75)
        self.assertEqual(resampled[0]["dt"].strftime("%H:%M"), "09:15")
        self.assertEqual(resampled[-1]["dt"].strftime("%H:%M"), "15:25")
        self.assertAlmostEqual(resampled[0]["l"], 1182.5)  # first bucket low
        self.assertAlmostEqual(min(bar["l"] for bar in resampled[:3]), 1175.3)  # range low
        self.assertAlmostEqual(max(bar["h"] for bar in resampled[:3]), 1192.0)  # range high
        self.assertAlmostEqual([bar for bar in resampled
                                if bar["dt"].strftime("%H:%M") == "09:55"][0]["c"], 1193.6)
        url = transport.calls[0]["url"]
        self.assertTrue(url.startswith(LIVE + "/v3/historical-candle/"), url)
        self.assertIn(KEY.replace("|", "%7C"), url)
        self.assertIn("/minutes/1/2026-10-26/2026-10-26", url)

    def test_second_shape_normalizes_sorted_and_filtered(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(candles_handler(read_json("candles-1m-response.json")))
        bars = client.candles(KEY, "1m", start="2026-10-23", end="2026-10-26")
        self.assertEqual([bar["dt"].strftime("%H:%M") for bar in bars],
                         ["15:29", "09:15", "09:16", "09:18"])  # 09:14 preopen and null row gone
        self.assertEqual(bars[0]["dt"].utcoffset(), datetime.timedelta(hours=5, minutes=30))
        self.assertIsInstance(bars[1]["o"], float)
        self.assertIsNone(bars[2]["oi"])  # null oi tolerated, kept as None
        self.assertEqual(bars[2]["v"], 500.0)

    def test_5m_history_is_rejected_with_guidance(self):
        client = Client({})
        with self.assertRaises(UpstoxError) as ctx:
            client.candles(KEY, "5m")
        message = str(ctx.exception)
        self.assertIn("5m", message)
        self.assertIn("intraday", message)
        self.assertIn("resample", message)

    def test_default_windows_shape_the_url(self):
        client = Client({}, token="test-tok")
        transport = Transport(candles_handler({"status": "success", "data": {"candles": []}}))
        client._transport = transport
        self.assertEqual(client.candles(KEY, "1m"), [])
        self.assertEqual(client.candles(KEY, "day"), [])
        urls = [call["url"] for call in transport.calls]
        self.assertIn("/minutes/1/", urls[0])
        self.assertIn("/days/1/", urls[1])
        # day window is one year back from today
        today = datetime.datetime.now(core().IST).date()
        self.assertIn("/%s/%s" % (today.isoformat(), today.replace(year=today.year - 1).isoformat()),
                      urls[1])

    def test_start_after_end_is_rejected(self):
        client = Client({})
        with self.assertRaises(UpstoxError):
            client.candles(KEY, "1m", start="2026-10-26", end="2026-10-20")

    def test_date_objects_are_accepted(self):
        client = Client({}, token="test-tok")
        transport = Transport(candles_handler({"status": "success", "data": {"candles": []}}))
        client._transport = transport
        client.candles(KEY, "1m", start=datetime.date(2026, 10, 1), end=datetime.date(2026, 10, 26))
        self.assertIn("/2026-10-26/2026-10-01", transport.calls[0]["url"])


class IntradayTest(unittest.TestCase):
    def test_intraday_uses_its_own_path_and_resamples(self):
        client = Client({}, token="test-tok")
        transport = Transport(intraday_handler(read_json("intraday-5m.json")))
        client._transport = transport
        bars = client.intraday(KEY, "5m")
        url = transport.calls[0]["url"]
        self.assertIn("/v3/historical-candle/intraday/", url)
        self.assertIn("/minutes/1", url)
        self.assertTrue(url.startswith(LIVE), url)
        self.assertEqual([bar["dt"].strftime("%H:%M") for bar in bars],
                         ["09:15", "09:20"])  # 09:25 bucket lacks its last minute, dropped
        first = bars[0]
        self.assertEqual((first["o"], first["h"], first["l"], first["c"]),
                         (1186.1, 1192.0, 1182.5, 1183.3))
        self.assertEqual(first["dt"].utcoffset(), datetime.timedelta(hours=5, minutes=30))

    def test_intraday_1m_passes_through(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(intraday_handler(read_json("intraday-5m.json")))
        bars = client.intraday(KEY, "1m")
        self.assertEqual(len(bars), 12)
        self.assertEqual(bars[0]["dt"].strftime("%H:%M"), "09:15")

    def test_intraday_bad_interval(self):
        client = Client({})
        with self.assertRaises(UpstoxError):
            client.intraday(KEY, "week")


class QuoteTest(unittest.TestCase):
    def test_quotes_normalize_keys_and_skip_absent(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(lambda method, url: (200, json.dumps(read_json("quotes-ohlc.json"))))
        out = client.quote([REL, "NSE_EQ|TCS", "NSE_EQ|SBIN"])
        self.assertEqual(sorted(out), [REL, "NSE_EQ|SBIN"])  # null last_price stays absent
        reliance = out[REL]
        self.assertEqual(reliance["ltp"], 1193.6)
        self.assertEqual((reliance["open"], reliance["high"], reliance["low"]), (1186.1, 1192.0, 1175.3))
        self.assertEqual(reliance["prev_close"], 1185.4)
        self.assertEqual(reliance["close"], 1193.6)
        self.assertEqual(reliance["volume"], 43170.0)
        self.assertEqual(reliance["ts"], datetime.datetime(2026, 10, 26, 10, 0, tzinfo=core().IST))
        sbin = out["NSE_EQ|SBIN"]
        self.assertEqual(sbin["high"], 815.9)

    def test_missing_keys_stay_absent(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(lambda method, url: (200, json.dumps(read_json("quotes-ohlc.json"))))
        out = client.quote([REL, "NSE_EQ|NOPE"])
        self.assertEqual(sorted(out), [REL, "NSE_EQ|SBIN"])  # NOPE missing, TCS null last_price

    def test_over_500_keys_are_chunked(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(lambda method, url: (200, json.dumps(read_json("quotes-ohlc.json"))))
        keys = ["NSE_EQ|SYM%04d" % i for i in range(1200)]
        client.quote(keys)
        urls = [call["url"] for call in client._transport.calls]
        self.assertEqual(len(urls), 3)
        first = urls[0].split("instrument_key=")[1].split("&")[0]
        from urllib.parse import unquote
        self.assertEqual(len(unquote(first).split(",")), 500)
        self.assertTrue(urls[0].startswith(LIVE + "/v2/market-quote/quotes"), urls[0])

    def test_ltp_fields_subset(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(lambda method, url: (200, json.dumps(read_json("quotes-ohlc.json"))))
        out = client.quote([REL], fields="ltp")
        self.assertEqual(sorted(out[REL]), ["ltp", "ts"])


class ErrorTest(unittest.TestCase):
    def setUp(self):
        self.client = Client({}, token="test-tok")
        self.bodies = {
            401: json.dumps(read_json("error-401.json")),
            429: json.dumps(read_json("error-429.json")),
            400: json.dumps(read_json("error-udapi.json")),
        }

    def test_401_raises_auth_error(self):
        self.client._transport = Transport(lambda m, u: (401, self.bodies[401]))
        with self.assertRaises(UpstoxAuthError) as ctx:
            self.client.quote([REL])
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("re-authenticate", str(ctx.exception))

    def test_403_raises_auth_error(self):
        self.client._transport = Transport(lambda m, u: (403, "forbidden"))
        with self.assertRaises(UpstoxAuthError):
            self.client.quote([REL])

    def test_429_raises_rate_limit(self):
        self.client._transport = Transport(lambda m, u: (429, self.bodies[429]))
        with self.assertRaises(UpstoxRateLimit) as ctx:
            self.client.quote([REL])
        self.assertEqual(ctx.exception.status, 429)  # HTTP status wins over the UDAPI code

    def test_udapi_body_raises_api_error_with_code(self):
        self.client._transport = Transport(lambda m, u: (400, self.bodies[400]))
        with self.assertRaises(UpstoxAPIError) as ctx:
            self.client.quote([REL])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "UDAPI100050")
        self.assertIn("Invalid instrument key", str(ctx.exception))

    def test_non_udapi_error_body_still_raises(self):
        self.client._transport = Transport(lambda m, u: (502, "<html>bad gateway</html>"))
        with self.assertRaises(UpstoxError) as ctx:
            self.client.quote([REL])
        self.assertEqual(ctx.exception.status, 502)
        self.assertNotIsInstance(ctx.exception, (UpstoxAuthError, UpstoxRateLimit))

    def test_parse_udapi_tolerates_garbage(self):
        self.assertIsNone(parse_udapi("not json"))
        self.assertIsNone(parse_udapi('{"status": "success"}'))

    def test_error_envelope_on_http_200_raises(self):
        client = Client({}, token="test-tok")
        client._transport = Transport(lambda m, u: (200, self.bodies[400]))
        with self.assertRaises(UpstoxAPIError):
            client.quote([REL])


class AuthTest(unittest.TestCase):
    def setUp(self):
        names = ("UPSTOX_SANDBOX_TOKEN", "UPSTOX_ANALYTICS_TOKEN", "UPSTOX_ACCESS_TOKEN")
        patcher = mock.patch.dict(os.environ, {name: "" for name in names})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _env_token(self, name, value):
        os.environ[name] = value

    def _token_file(self, doc):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        with open(os.path.join(state, "tokens.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return state

    def test_missing_market_token_names_the_env_var(self):
        client = Client({})
        with self.assertRaises(UpstoxAuthError) as ctx:
            client.quote([REL])
        self.assertIn("UPSTOX_ANALYTICS_TOKEN", str(ctx.exception))

    def test_missing_sandbox_trading_token_names_the_env_var(self):
        client = Client({})
        with self.assertRaises(UpstoxAuthError) as ctx:
            client.request("POST", "/v2/order/place", payload={})
        self.assertIn("UPSTOX_SANDBOX_TOKEN", str(ctx.exception))

    def test_missing_live_trading_token_names_the_env_var(self):
        client = Client({}, mode="live")
        with self.assertRaises(UpstoxAuthError) as ctx:
            client.request("POST", "/v2/order/place", payload={})
        self.assertIn("UPSTOX_ACCESS_TOKEN", str(ctx.exception))

    def test_analytics_token_wins_for_market_data(self):
        client = Client({})
        client._transport = Transport(lambda m, u: (200, "{}"))
        self._env_token("UPSTOX_ANALYTICS_TOKEN", "analytics-tok")
        self._env_token("UPSTOX_ACCESS_TOKEN", "access-tok")
        client.quote([REL])
        headers = client._transport.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer analytics-tok")

    def test_explicit_token_beats_env_and_file(self):
        state = self._token_file({"analytics": "file-tok", "access": "file-access"})
        client = Client({"state_dir": state}, token="explicit-tok")
        client._transport = Transport(lambda m, u: (200, "{}"))
        client.quote([REL])
        self.assertEqual(client._transport.calls[0]["headers"]["Authorization"], "Bearer explicit-tok")

    def test_token_file_backs_market_data(self):
        state = self._token_file({"analytics": "file-tok", "totp_secret": "never-read",
                                  "pin": "never-read", "api_secret": "never-read"})
        client = Client({"state_dir": state})
        client._transport = Transport(lambda m, u: (200, "{}"))
        client.quote([REL])
        headers = client._transport.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer file-tok")

    def test_sandbox_token_file_backs_trading(self):
        state = self._token_file({"sandbox": "sbx-tok"})
        client = Client({"state_dir": state})
        client._transport = Transport(lambda m, u: (200, "{}"))
        client.request("POST", "/v2/order/place", payload={"quantity": 1})
        call = client._transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer sbx-tok")
        self.assertEqual(call["url"], SANDBOX + "/v2/order/place")
        self.assertEqual(call["payload"], {"quantity": 1})


class HostsTest(unittest.TestCase):
    def test_market_data_always_uses_live_host_even_in_sandbox(self):
        client = Client({"mode": "sandbox"}, token="test-tok")
        client._transport = Transport(lambda m, u: (200, json.dumps(read_json("quotes-ohlc.json"))))
        client.quote([REL])
        self.assertTrue(client._transport.calls[0]["url"].startswith(LIVE))
        client._transport = Transport(candles_handler({"status": "success", "data": {"candles": []}}))
        client.candles(KEY, "1m", start="2026-10-26", end="2026-10-26")
        self.assertTrue(client._transport.calls[-1]["url"].startswith(LIVE))

    def test_trading_base_follows_mode(self):
        self.assertEqual(Client({}).trading_base, SANDBOX)
        self.assertEqual(Client({}, mode="live").trading_base, LIVE)
        self.assertEqual(Client({"mode": "live"}).trading_base, LIVE)
        with self.assertRaises(UpstoxError):
            Client({}, mode="paper")

    def test_explicit_sandbox_market_data_raises(self):
        client = Client({})
        with self.assertRaises(SandboxUnsupported):
            client.request("GET", "/v3/historical-candle/%s/minutes/1/2026-10-26/2026-10-01" % KEY,
                           base=SANDBOX)
        with self.assertRaises(SandboxUnsupported):
            client.request("GET", "/v2/market-quote/quotes?instrument_key=" + KEY, base=SANDBOX)

    def test_sandbox_orders_never_fall_back_to_live(self):
        client = Client({"mode": "sandbox"}, token="test-tok")
        client._transport = Transport(lambda m, u: (200, "{}"))
        client.request("POST", "/v2/order/place", payload={})
        self.assertTrue(client._transport.calls[0]["url"].startswith(SANDBOX))

    def test_bearer_token_sent_on_data_calls(self):
        client = Client({}, token="tok-123")
        client._transport = Transport(lambda m, u: (200, "{}"))
        client.quote([REL])
        call = client._transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(call["method"], "GET")


class InstrumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FX, "instruments-NSE.json.gz"), "rb") as fh:
            cls.raw = fh.read()

    def test_fixture_parses_gz_and_plain(self):
        import trader_core.instruments as instruments
        rows = instruments.load(self.raw)
        self.assertEqual(len(rows), 4)
        rel = instruments.find(rows, "RELIANCE")
        self.assertEqual(rel["instrument_key"], "NSE_EQ|INE002A01018")
        self.assertEqual(rel["tick_size"], 0.05)
        self.assertEqual(rel["lot_size"], 1.0)
        self.assertEqual(rel["freeze_quantity"], 11111.0)
        self.assertEqual(rel["isin"], "INE002A01018")
        nifty = instruments.find(rows, "NIFTY", exchange="NSE_FO")
        self.assertEqual(nifty["instrument_type"], "FUT")
        self.assertEqual(nifty["expiry"], "2026-12-31")
        self.assertEqual(nifty["lot_size"], 75.0)
        self.assertEqual(nifty["underlying_key"], "NSE_INDEX|NIFTY 50")
        self.assertIsNone(nifty["strike_price"])
        # the same bytes round trip through the text transport
        self.assertEqual(instruments.load(as_body(self.raw)), rows)

    def test_index_prefers_nse_eq(self):
        import trader_core.instruments as instruments
        rows = instruments.load(self.raw)
        idx = instruments.index(rows)
        self.assertEqual(idx["RELIANCE"]["instrument_key"], "NSE_EQ|INE002A01018")
        self.assertEqual(idx["NIFTY"]["trading_symbol"], "NIFTY")

    def test_find_unknown_symbol_raises(self):
        import trader_core.instruments as instruments
        rows = instruments.load(self.raw)
        with self.assertRaises(InstrumentError):
            instruments.find(rows, "INFY")
        with self.assertRaises(InstrumentError):
            instruments.find([], "RELIANCE")

    def test_malformed_masters_raise(self):
        import trader_core.instruments as instruments
        for bad in (b"", b"{not json", b"[]", b'{"data": "nope"}', b'[{"trading_symbol": "X"}]'):
            with self.assertRaises(InstrumentError):
                instruments.load(bad)

    def test_client_downloads_parses_and_caches(self):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        client = Client({"state_dir": state}, token="test-tok")
        transport = Transport(lambda m, u: (200, as_body(self.raw)))
        client._transport = transport
        rows = client.instruments()
        self.assertEqual(len(rows), 4)
        self.assertTrue(transport.calls[0]["url"].startswith("https://assets.upstox.com/"))
        cache = os.path.join(state, "instruments-NSE.json")
        self.assertTrue(os.path.exists(cache))
        transport.calls = []
        again = client.instruments()  # served from the cache, no second download
        self.assertEqual(len(again), 4)
        self.assertEqual(transport.calls, [])
        forced = client.instruments(refresh=True)
        self.assertEqual(len(forced), 4)
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
