from __future__ import annotations
import hmac
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlparse
import os
from functools import wraps
from flask import request, jsonify, g
import time
from collections import deque
from threading import Lock
import secrets

from network_policy import NetworkPolicy


_ALLOWED_SCHEMES = {"http", "https"}

# RFC1918 + loopback + link-local + reserved ranges we never want to hit.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(net) for net in [
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "0.0.0.0/8",
        "100.64.0.0/10",
    ]
]


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _url_host_if_safe_shape(url: str) -> str | None:
    """Return the normalized host after non-network URL checks, else ``None``."""
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s or s.startswith("-"):
        return None
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    host = parsed.hostname
    if not host:
        return None
    host = host.lower()
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return None
    if _is_blocked_ip(host):
        return None
    return host


def _host_resolves_publicly(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(not _is_blocked_ip(info[4][0]) for info in infos)


def _is_safe_url_unleased(url: str) -> bool:
    """Validate under a caller-owned execution lease (runner boundary only)."""
    host = _url_host_if_safe_shape(url)
    return bool(host and _host_resolves_publicly(host))


def is_safe_url(url: str, *, network_policy: NetworkPolicy) -> bool:
    """Return True only if url is a public http(s) URL we're willing to hand to a fetcher.

    Rejects: non-http schemes, option-shaped strings (argv-injection guard), localhost,
    private/loopback/link-local/metadata IP literals, and hostnames that fail to resolve
    or resolve to those ranges.

    Best-effort, not a hard SSRF boundary: DNS is resolved ONCE here, while the actual
    fetch (yt-dlp) re-resolves and follows redirects on its own — a DNS rebind or a
    redirect to an internal address after validation is not caught. Acceptable in the
    default loopback single-user posture; do not rely on it alone on a public bind.
    """
    host = _url_host_if_safe_shape(url)
    if not host:
        return False
    with network_policy.egress("url_validation"):
        return _host_resolves_publicly(host)


def token_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TROVE_TOKEN", "").strip()
        if not token:
            return view(*args, **kwargs)
        header = request.headers.get("Authorization", "")
        if header == f"Bearer {token}":
            return view(*args, **kwargs)
        return jsonify({"error": "unauthorized"}), 401
    return wrapper


# ---------------------------------------------------------------------------
# Signed-URL system
# ---------------------------------------------------------------------------
#
# Some routes are reached via direct browser navigation (anchor clicks,
# ``<video src>``) where the Authorization header isn't carried, so we
# can't enforce the bearer token. The signed-URL escape hatch lets a
# server-rendered template mint a one-off URL that authenticates itself
# via ``?sig=&exp=`` query params signed by TROVE_TOKEN.
#
# Signature payload includes:
#   * resource_id  — the route variable being authorized
#   * scope        — explicit namespace (``media`` / ``transcript-view`` /
#                    ``transcript-export``) so a media link can't be
#                    replayed against an export endpoint and vice versa
#   * expires_at   — unix timestamp; verifier rejects past-due sigs so a
#                    leaked link stops working without rotating the token
#
# Signatures are HMAC-SHA256 over ``"{scope}|{resource_id}|{exp}"`` keyed
# by TROVE_TOKEN. When TROVE_TOKEN is unset there's no signing (auth is
# off entirely; ``signed_query`` returns ``""``).

# Public scope constants. Routes/templates should use these instead of
# raw strings so a typo can't silently produce an unverifiable sig.
SCOPE_MEDIA              = "media"
SCOPE_TRANSCRIPT_VIEW    = "transcript-view"
SCOPE_TRANSCRIPT_EXPORT  = "transcript-export"
_KNOWN_SCOPES = frozenset({SCOPE_MEDIA, SCOPE_TRANSCRIPT_VIEW, SCOPE_TRANSCRIPT_EXPORT})

# Default sig TTL: 1 hour. Long enough that a user opening a transcript
# page and clicking around for a while keeps working; short enough that
# a leaked URL pasted into chat tomorrow is a 401. Override per-call or
# globally via ``TROVE_SIG_TTL``.
def _default_ttl() -> int:
    raw = os.environ.get("TROVE_SIG_TTL", "").strip()
    if raw.isdigit():
        return int(raw)
    return 3600


def sign_resource(resource_id: str, scope: str,
                  expires_in: int | None = None) -> tuple[str, int]:
    """Mint ``(sig, exp)`` for ``resource_id`` under ``scope``.

    Returns ``("", 0)`` when TROVE_TOKEN is unset (auth disabled). The
    caller embeds both values into the URL as ``?sig=…&exp=…``. ``exp``
    is a unix timestamp; the verifier rejects requests once it passes.
    """
    if scope not in _KNOWN_SCOPES:
        raise ValueError(f"unknown signing scope: {scope!r}")
    token = os.environ.get("TROVE_TOKEN", "").strip()
    if not token:
        return ("", 0)
    ttl = int(expires_in) if expires_in is not None else _default_ttl()
    if ttl <= 0:
        raise ValueError("expires_in must be > 0 seconds")
    exp = int(time.time()) + ttl
    payload = f"{scope}|{resource_id}|{exp}".encode()
    sig = hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()
    return (sig, exp)


def verify_signature(resource_id: str, scope: str,
                     sig: str, exp: str | int) -> bool:
    """Constant-time verify ``sig`` for ``(resource_id, scope, exp)``.

    Returns False if TROVE_TOKEN is unset (signed URLs require a token
    to be meaningful), if ``sig`` or ``exp`` is missing/malformed, or
    if the signature has already expired.
    """
    token = os.environ.get("TROVE_TOKEN", "").strip()
    if not token or not sig:
        return False
    if scope not in _KNOWN_SCOPES:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    payload = f"{scope}|{resource_id}|{exp_i}".encode()
    expected = hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def signed_query(resource_id: str, scope: str,
                 expires_in: int | None = None) -> str:
    """Return ``"sig=…&exp=…"`` (no leading ``?``) or ``""``.

    Convenience helper for Jinja templates so they don't have to juggle
    the (sig, exp) tuple. Empty string when TROVE_TOKEN is unset, so the
    template can splice it unconditionally.
    """
    sig, exp = sign_resource(resource_id, scope, expires_in)
    if not sig:
        return ""
    return f"sig={sig}&exp={exp}"


def token_or_sig_required(scope: str, *, kwarg: str):
    """Decorator factory: accept either the bearer token OR a scoped sig.

    ``scope`` partitions the signature namespace (so a ``media`` sig
    can't unlock a ``transcript-export`` route). ``kwarg`` is the name
    of the Flask route variable holding the resource id — we verify
    that kwarg specifically rather than "any string kwarg in the route"
    so e.g. an export route's ``fmt`` parameter can't be misread as the
    resource id.

    When TROVE_TOKEN is unset the decorator is a passthrough.
    """
    if scope not in _KNOWN_SCOPES:
        raise ValueError(f"unknown signing scope: {scope!r}")

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            token = os.environ.get("TROVE_TOKEN", "").strip()
            if not token:
                return view(*args, **kwargs)
            header = request.headers.get("Authorization", "")
            if header == f"Bearer {token}":
                return view(*args, **kwargs)
            resource_id = kwargs.get(kwarg)
            if isinstance(resource_id, str):
                sig = request.args.get("sig", "")
                exp = request.args.get("exp", "")
                if verify_signature(resource_id, scope, sig, exp):
                    return view(*args, **kwargs)
            return jsonify({"error": "unauthorized"}), 401
        return wrapper
    return decorator


class RateLimiter:
    """Sliding-window per-key rate limiter. In-process only."""

    def __init__(self, rate: int, per_seconds: int):
        self.rate = rate
        self.per_seconds = per_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            cutoff = now - self.per_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.rate:
                return False
            q.append(now)
            return True

    def remaining(self, key: str) -> tuple[int, float]:
        """Return ``(remaining_in_window, retry_after_seconds)`` for *key*.

        Cheap, lock-held read used to populate ``X-RateLimit-Remaining``
        + ``Retry-After`` response headers. ``retry_after_seconds`` is 0
        when the key is under the limit.
        """
        if self.rate <= 0:
            return (10**9, 0.0)
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return (self.rate, 0.0)
            cutoff = now - self.per_seconds
            # Don't mutate during a read — count live hits manually.
            live = sum(1 for ts in q if ts >= cutoff)
            remaining = max(0, self.rate - live)
            if remaining > 0:
                return (remaining, 0.0)
            # Over the limit → seconds until oldest live hit ages out.
            oldest_live = next((ts for ts in q if ts >= cutoff), now)
            return (0, max(0.0, self.per_seconds - (now - oldest_live)))


_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def _cors_origin():
    """The request Origin iff it's a same-machine (loopback) browser origin, else None."""
    origin = request.headers.get("Origin", "")
    return origin if origin and _LOCAL_ORIGIN_RE.match(origin) else None


def _apply_cors(resp, origin):
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key"
    resp.headers["Access-Control-Expose-Headers"] = (
        "X-Idempotent-Replay, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Window, Retry-After"
    )
    resp.headers["Access-Control-Max-Age"] = "600"


def attach_cors(app):
    """Allow the studio (its own Next.js origin, e.g. localhost:3000) to call the API.

    Only **loopback** origins are permitted — the server is localhost-bound by default
    (config.assert_safe_bind), so this opens nothing the user's own machine can't already
    reach, while keeping the door shut to arbitrary websites. Mirrors the spec's "two
    clients of the same API": the browser studio is one of them.
    """

    @app.before_request
    def _cors_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            origin = _cors_origin()
            if origin:
                resp = app.make_default_options_response()
                _apply_cors(resp, origin)
                return resp
        return None

    @app.after_request
    def _cors_headers(response):
        if request.path.startswith("/api/"):
            origin = _cors_origin()
            if origin:
                _apply_cors(response, origin)
        return response

    return app


def attach_security_headers(app):
    """Mount per-request CSP nonce + standard security headers on a Flask app."""

    @app.before_request
    def _set_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.after_request
    def _set_headers(response):
        nonce = getattr(g, "csp_nonce", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"img-src 'self' https: data:; "
            f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
            f"font-src 'self' https://fonts.gstatic.com; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    return app
