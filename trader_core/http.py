"""HTTP seam for Upstox: an injectable transport plus typed error mapping."""

import json
import urllib.error
import urllib.request

SANDBOX = "https://api-sandbox.upstox.com"
LIVE = "https://api.upstox.com"


class UpstoxError(Exception):
    """Base Upstox error. .status is the HTTP status (or None), .code the UDAPI errorCode."""

    def __init__(self, message, status=None, code=None):
        super(UpstoxError, self).__init__(message)
        self.status = status
        self.code = code


class UpstoxAuthError(UpstoxError):
    """401/403: the token is missing, expired, or not valid for this host."""


class UpstoxRateLimit(UpstoxError):
    """429: backed off by Upstox, retry later."""


class UpstoxAPIError(UpstoxError):
    """Upstox answered with a UDAPI error body."""


class SandboxUnsupported(UpstoxError):
    """The sandbox host has no market data; the caller asked for it anyway."""


def default_transport(method, url, payload=None, headers=None, timeout=30):
    """POST/GET over urllib. Returns (status, body_text, response_headers);
    raises on network errors."""
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in req_headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "surrogateescape"), \
                dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "surrogateescape")
        except Exception:
            body = ""
        return exc.code, body, dict(exc.headers.items()) if exc.headers else {}


RETRY_AFTER_CAP = 30.0  # seconds: a scan polls on fixed cadences, never camp on one


def retry_after(headers):
    """HTTP Retry-After header -> seconds, capped at RETRY_AFTER_CAP. None when
    the header is absent, unparseable, or an HTTP-date (that would mean an
    unknown wait, so it is treated as no guidance)."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            try:
                return max(0.0, min(float(str(value).strip()), RETRY_AFTER_CAP))
            except (TypeError, ValueError):
                return None
    return None


def parse_udapi(body_text):
    """Return the first {"errorCode","message"} from a UDAPI error body, else None."""
    try:
        doc = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("status") != "error":
        return None
    errors = doc.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if not isinstance(first, dict):
        return None
    return {"errorCode": first.get("errorCode"), "message": first.get("message")}


def raise_for_response(status, body_text, headers=None):
    """Map an HTTP response to a typed UpstoxError, or return None on success."""
    if status in (401, 403):
        raise UpstoxAuthError(
            "Upstox rejected the credentials (HTTP %d): re-authenticate, run the login skill" % status,
            status=status, code="AUTH_ERROR")
    if status == 429:
        err = UpstoxRateLimit("Upstox rate limit hit (HTTP 429), slow down and retry", status=status,
                              code="RATE_LIMIT")
        err.retry_after = retry_after(headers)
        raise err
    udapi = parse_udapi(body_text)
    if udapi is not None:
        code = udapi.get("errorCode") or "UDAPI_1000"
        message = udapi.get("message") or "Upstox API error"
        raise UpstoxAPIError("%s: %s" % (code, message), status=status, code=code)
    if 200 <= status < 300:
        return None
    snippet = (body_text or "").strip()[:200]
    detail = (": %s" % snippet) if snippet else ""
    raise UpstoxAPIError("Upstox returned HTTP %d%s" % (status, detail), status=status,
                         code="HTTP_%d" % status)
