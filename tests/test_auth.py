"""Tests for trader_core.auth: Upstox authorise URL, code exchange, token file. No network."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from _load import core

core()  # repo root on sys.path
from trader_core import auth, config  # noqa: E402
from trader_core.http import UpstoxAuthError  # noqa: E402

SECRET_TOKEN = "tok-abc-123"
ENV = {config.ENV["api_key"]: "app-key", config.ENV["api_secret"]: "app-secret"}


class FakeTransport(object):
    """The injectable transport: records the POST and returns a canned answer."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else json.dumps(
            {"access_token": SECRET_TOKEN, "extended_token": "analytics-xyz"})
        self.calls = []

    def __call__(self, method, url, payload=None, headers=None, timeout=30):
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        return self.status, self.body


class LoginUrl(unittest.TestCase):
    def test_url_shape(self):
        url = auth.login_url(client="app-key", redirect_uri="https://example.test/cb", state="s1")
        self.assertTrue(url.startswith("https://api.upstox.com/v2/login/authorization/dialog?"))
        self.assertIn("client_id=app-key", url)
        self.assertIn("response_type=code", url)
        self.assertIn("state=s1", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fexample.test%2Fcb", url)

    def test_login_url_needs_an_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(UpstoxAuthError):
                auth.login_url()


class Exchange(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dt-auth-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.cfg = {"state_dir": self.state}

    def exchange(self, transport):
        with mock.patch.dict(os.environ, ENV, clear=True):
            return auth.exchange_code("code-1", transport=transport, cfg=self.cfg)

    def test_code_exchange_writes_the_token_file(self):
        transport = FakeTransport()
        path = self.exchange(transport)
        self.assertEqual(path, os.path.join(self.state, "tokens.json"))
        with open(path, encoding="utf-8") as fh:
            tokens = json.load(fh)
        self.assertEqual(tokens["access"], SECRET_TOKEN)
        self.assertEqual(tokens["analytics"], "analytics-xyz")
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].startswith(
            "https://api.upstox.com/v2/login/authorization/token"))
        self.assertIn("code=code-1", call["payload"])
        self.assertIn("grant_type=authorization_code", call["payload"])
        self.assertIn("client_secret=app-secret", call["payload"])
        self.assertIn("application/x-www-form-urlencoded", call["headers"]["Content-Type"])

    def test_bad_response_raises_auth_error(self):
        with self.assertRaises(UpstoxAuthError):
            self.exchange(FakeTransport(400, '{"error": "invalid_grant"}'))
        self.assertFalse(os.path.exists(os.path.join(self.state, "tokens.json")))

    def test_cli_exchange_never_prints_the_token(self):
        transport = FakeTransport()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, ENV, clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            code = auth.main(["exchange", "code-1", "--state-dir", self.state],
                             transport=transport)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertNotIn(SECRET_TOKEN, out.getvalue())
        self.assertNotIn(SECRET_TOKEN, err.getvalue())
        self.assertIn("tokens.json", out.getvalue())

    def test_cli_bad_exchange_exits_2_one_line(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, ENV, clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            code = auth.main(["exchange", "code-1", "--state-dir", self.state],
                             transport=FakeTransport(400, '{"error": "invalid_grant"}'))
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().startswith("trader_core.auth: "), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn(SECRET_TOKEN, out.getvalue())


if __name__ == "__main__":
    unittest.main()
