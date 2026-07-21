"""Tests for config.py — defaults + the unauthenticated public-bind guard."""
from __future__ import annotations

import importlib

import pytest

import config


def test_defaults_are_localhost_8899(monkeypatch):
    # Reload with a clean env so module-level defaults pick up the right values.
    for k in ("HOST", "PORT", "TROVE_URL"):
        monkeypatch.delenv(k, raising=False)
    cfg = importlib.reload(config)
    try:
        assert cfg.DEFAULT_HOST == "127.0.0.1"
        assert cfg.DEFAULT_PORT == 8899
        assert cfg.DEFAULT_BASE_URL == "http://127.0.0.1:8899"
    finally:
        importlib.reload(config)


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.delenv("TROVE_URL", raising=False)
    cfg = importlib.reload(config)
    try:
        assert cfg.DEFAULT_HOST == "0.0.0.0"
        assert cfg.DEFAULT_PORT == 9000
        assert cfg.DEFAULT_BASE_URL == "http://0.0.0.0:9000"
    finally:
        importlib.reload(config)


def test_trove_url_overrides_host_port(monkeypatch):
    monkeypatch.setenv("TROVE_URL", "https://trove.example.com")
    cfg = importlib.reload(config)
    try:
        assert cfg.DEFAULT_BASE_URL == "https://trove.example.com"
    finally:
        importlib.reload(config)


# ----- assert_safe_bind --------------------------------------------------

def test_loopback_bind_always_allowed():
    for host in ("127.0.0.1", "::1", "localhost"):
        config.assert_safe_bind(host, env={})


def test_public_bind_without_token_refused():
    with pytest.raises(config.UnauthenticatedPublicBindError):
        config.assert_safe_bind("0.0.0.0", env={})


def test_public_bind_with_token_allowed():
    config.assert_safe_bind("0.0.0.0", env={"TROVE_TOKEN": "secret"})


def test_public_bind_rejects_legacy_unauthenticated_optin():
    with pytest.raises(config.UnauthenticatedPublicBindError):
        config.assert_safe_bind(
            "0.0.0.0",
            env={"TROVE_ALLOW_UNAUTH_PUBLIC": "1"},
        )


def test_empty_token_does_not_count():
    with pytest.raises(config.UnauthenticatedPublicBindError):
        config.assert_safe_bind("0.0.0.0", env={"TROVE_TOKEN": "   "})


def test_arbitrary_public_address_refused():
    # Any non-loopback bind triggers the guard, not just 0.0.0.0.
    with pytest.raises(config.UnauthenticatedPublicBindError):
        config.assert_safe_bind("192.168.1.10", env={})


def test_error_message_lists_only_authenticated_or_loopback_remedies():
    try:
        config.assert_safe_bind("0.0.0.0", env={})
    except config.UnauthenticatedPublicBindError as e:
        msg = str(e)
        assert "HOST=127.0.0.1" in msg
        assert "TROVE_TOKEN" in msg
        assert "TROVE_ALLOW_UNAUTH_PUBLIC" not in msg
    else:
        pytest.fail("expected UnauthenticatedPublicBindError")


def test_trusted_proxy_hops_defaults_to_zero_and_accepts_nonnegative_values():
    assert config.trusted_proxy_hops(env={}) == 0
    assert config.trusted_proxy_hops(env={"TROVE_TRUST_PROXY_HOPS": "0"}) == 0
    assert config.trusted_proxy_hops(env={"TROVE_TRUST_PROXY_HOPS": " 2 "}) == 2


@pytest.mark.parametrize("value", ["not-an-int", "-1", ""])
def test_invalid_trusted_proxy_hops_falls_back_with_warning(caplog, value):
    with caplog.at_level("WARNING"):
        result = config.trusted_proxy_hops(
            env={"TROVE_TRUST_PROXY_HOPS": value},
        )

    assert result == 0
    assert "TROVE_TRUST_PROXY_HOPS" in caplog.text
    assert "defaulting to 0" in caplog.text


def test_rate_limit_max_keys_defaults_to_4096_and_accepts_positive_values():
    assert config.rate_limit_max_keys(env={}) == 4096
    assert config.rate_limit_max_keys(env={"TROVE_RATE_LIMIT_MAX_KEYS": " 4 "}) == 4


@pytest.mark.parametrize("value", ["not-an-int", "0", "-1", ""])
def test_invalid_rate_limit_max_keys_falls_back_with_warning(caplog, value):
    with caplog.at_level("WARNING"):
        result = config.rate_limit_max_keys(
            env={"TROVE_RATE_LIMIT_MAX_KEYS": value},
        )

    assert result == 4096
    assert "TROVE_RATE_LIMIT_MAX_KEYS" in caplog.text
    assert "defaulting to 4096" in caplog.text
