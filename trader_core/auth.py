"""Upstox OAuth login helper: the v2 authorise URL and the code exchange.

    python -m trader_core.auth login [--redirect-uri URI] [--state S]
    python -m trader_core.auth exchange <code>

The exchange posts the single-use code to Upstox and writes the daily access
token into tokens.json under the state dir (config.state_dir, by default
<repo>/trader/state) through config.state_write. The token is never printed:
the file is the only place it lands. TOTP login is out of scope.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from trader_core import config
from trader_core.http import LIVE, UpstoxAuthError

SKILL = "trader_core.auth"
AUTHORISE_URL = LIVE + "/v2/login/authorization/dialog"
TOKEN_URL = LIVE + "/v2/login/authorization/token"
TOKENS_FILE = "tokens.json"
DEFAULT_REDIRECT = "http://localhost:8080/callback"


def login_url(client=None, redirect_uri=None, state=None):
    """The Upstox v2 authorise URL to open in a browser. The user signs in at
    Upstox, which redirects back to redirect_uri with ?code=... once."""
    api_key = client or os.environ.get(config.ENV["api_key"])
    if not api_key:
        raise UpstoxAuthError("no Upstox API key: set %s from the Upstox developer console"
                              % config.ENV["api_key"], code="AUTH_ERROR")
    redirect = redirect_uri or os.environ.get("UPSTOX_REDIRECT_URI") or DEFAULT_REDIRECT
    params = {"response_type": "code", "client_id": api_key, "redirect_uri": redirect}
    if state:
        params["state"] = state
    return AUTHORISE_URL + "?" + urllib.parse.urlencode(params)


def _post_form(url, fields, transport=None):
    """POST a form body. transport is any callable taking (method, url,
    payload=<form encoded body>, headers=...); the default posts it itself."""
    body = urllib.parse.urlencode(fields)
    headers = {"Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    if transport is not None:
        return transport("POST", url, payload=body, headers=headers)
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "surrogateescape")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "surrogateescape")
        except Exception:
            text = ""
        return exc.code, text


def exchange_code(code, client=None, transport=None, redirect_uri=None, cfg=None):
    """Exchange the code for the daily access token and write it into
    tokens.json under the key the client reads ("access"; the read-only
    extended token, when present, goes under "analytics"). Returns the tokens
    file path. A rejected exchange raises UpstoxAuthError."""
    api_key = client or os.environ.get(config.ENV["api_key"])
    api_secret = os.environ.get(config.ENV["api_secret"])
    if not api_key or not api_secret:
        raise UpstoxAuthError("the token exchange needs %s and %s"
                              % (config.ENV["api_key"], config.ENV["api_secret"]),
                              code="AUTH_ERROR")
    redirect = redirect_uri or os.environ.get("UPSTOX_REDIRECT_URI") or DEFAULT_REDIRECT
    fields = {"code": code, "client_id": api_key, "client_secret": api_secret,
              "redirect_uri": redirect, "grant_type": "authorization_code"}
    status, body = _post_form(TOKEN_URL, fields, transport)
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        doc = None
    if not 200 <= status < 300 or not isinstance(doc, dict) or not doc.get("access_token"):
        detail = ""
        if isinstance(doc, dict):
            detail = doc.get("error") or doc.get("error_description") or ""
        raise UpstoxAuthError(
            "Upstox did not return an access token (HTTP %s)%s: check the code, %s and %s, "
            "then run login again"
            % (status, (": " + str(detail)) if detail else "",
               config.ENV["api_key"], config.ENV["api_secret"]), status=status, code="AUTH_ERROR")
    cfg = config.load() if cfg is None else cfg
    tokens = config.state_read(cfg, TOKENS_FILE, {}) or {}
    if not isinstance(tokens, dict):
        tokens = {}
    tokens["access"] = doc["access_token"]
    if isinstance(doc.get("extended_token"), str) and doc["extended_token"]:
        tokens["analytics"] = doc["extended_token"]
    return config.state_write(cfg, TOKENS_FILE, tokens)


def main(argv=None, transport=None):
    ap = argparse.ArgumentParser(
        prog="python -m trader_core.auth",
        description="Upstox OAuth login helper: print the authorise URL, exchange the code.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("login", help="print the Upstox authorise URL to open")
    p.add_argument("--client", help="API key (default %s)" % config.ENV["api_key"])
    p.add_argument("--redirect-uri")
    p.add_argument("--state")
    p = sub.add_parser("exchange", help="exchange the code Upstox redirected with for the token")
    p.add_argument("code")
    p.add_argument("--client", help="API key (default %s)" % config.ENV["api_key"])
    p.add_argument("--redirect-uri")
    p.add_argument("--state-dir", help="where tokens.json lives (default <repo>/trader/state)")
    p.add_argument("--config", default=None, help="trader config.json (defaults apply when absent)")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help(sys.stderr)
        return 2
    try:
        if args.cmd == "login":
            print(login_url(client=args.client, redirect_uri=args.redirect_uri, state=args.state))
            print("Open that URL, sign in at Upstox, then run: "
                  "python -m trader_core.auth exchange <code>")
            return 0
        cfg = config.load(args.config)
        if args.state_dir:
            cfg = dict(cfg, state_dir=args.state_dir)
        path = exchange_code(args.code, client=args.client, transport=transport,
                             redirect_uri=args.redirect_uri, cfg=cfg)
        print("Access token written to %s (key \"access\"); it expires at 03:30 IST." % path)
        return 0
    except (UpstoxAuthError, config.ConfigError, OSError) as exc:
        print("%s: %s" % (SKILL, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
