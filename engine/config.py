"""Single source of truth for Trove host/port/base-URL defaults.

Historically these defaults drifted across entrypoints (app.py used
``0.0.0.0:5000``, cli/mcp used ``127.0.0.1:5000``, README/Dockerfile/
trove.sh used ``127.0.0.1:8899``). That was both a UX bug (CLI
talking to the wrong port out of the box) and a security footgun
(Flask runner binding to ``0.0.0.0`` with no token by default while
the README promised localhost-only).

Everything that needs a host, port, or base URL imports from here.
"""
from __future__ import annotations

import logging
import os


_logger = logging.getLogger(__name__)


DEFAULT_HOST: str = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT: int = int(os.environ.get("PORT", "8899"))
DEFAULT_BASE_URL: str = os.environ.get(
    "TROVE_URL",
    f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
)


class UnauthenticatedPublicBindError(RuntimeError):
    """Raised when the server would bind to a non-loopback address with
    no TROVE_TOKEN set.

    Refusing to start protects users from accidentally exposing an
    unauthenticated download/transcribe API to their LAN or, worse,
    the public internet.
    """


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def trusted_proxy_hops(*, env: dict[str, str] | None = None) -> int:
    """Return the explicitly configured count of trusted right-most proxies."""
    e = env if env is not None else os.environ
    raw = e.get("TROVE_TRUST_PROXY_HOPS")
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        _logger.warning(
            "Invalid TROVE_TRUST_PROXY_HOPS=%r; defaulting to 0",
            raw,
        )
        return 0
    return value


def rate_limit_max_keys(*, env: dict[str, str] | None = None) -> int:
    """Return the bounded limiter identity capacity, defaulting safely."""
    e = env if env is not None else os.environ
    raw = e.get("TROVE_RATE_LIMIT_MAX_KEYS")
    if raw is None:
        return 4096
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        _logger.warning(
            "Invalid TROVE_RATE_LIMIT_MAX_KEYS=%r; defaulting to 4096",
            raw,
        )
        return 4096
    return value


def assert_safe_bind(host: str, *, env: dict[str, str] | None = None) -> None:
    """Raise if `host` would expose Trove publicly without a token.

    Allows the bind when either of the following is true:
      * host is loopback (127.0.0.1, ::1, localhost)
      * TROVE_TOKEN is set (every /api/* request must authenticate)

    `env` is injectable for unit tests; defaults to os.environ.
    """
    e = env if env is not None else os.environ
    if host in _LOOPBACK:
        return
    if (e.get("TROVE_TOKEN") or "").strip():
        return
    raise UnauthenticatedPublicBindError(
        f"Refusing to bind to {host!r} without authentication.\n"
        "Trove's HTTP API has no auth by default; binding to a non-\n"
        "loopback address would expose downloads/transcripts to anyone\n"
        "who can reach the port.\n\n"
        "Pick ONE:\n"
        "  1. Bind to localhost only:   export HOST=127.0.0.1\n"
        "  2. Require a bearer token:   export TROVE_TOKEN=$(openssl rand -hex 32)"
    )
