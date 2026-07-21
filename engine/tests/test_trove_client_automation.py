"""Tests for TroveClient's Phase-3 automation methods (produce / recipes / watches / brand kits)
— the surface that gives the CLI + MCP parity with the studio. Same monkeypatched-urlopen
discipline as test_trove_client_clips.py: no network, just assert the request the client sends."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trove_client import TroveClient


PHASE0_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/v1/phase0-contract.json")
    .read_text(encoding="utf-8")
)


class _FakeResp:
    def __init__(self, status=200, body=b'{"ok": true}', content_type="application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(), "data": req.data})
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _body(calls):
    return json.loads(calls[-1]["data"].decode())


@pytest.fixture
def c():
    return TroveClient(base_url="http://x", token="")


def test_produce_with_saved_recipe_id(c, captured):
    c.produce("src1", recipe_id="r1")
    assert captured[-1]["url"] == "http://x/api/v1/sources/src1/produce"
    assert captured[-1]["method"] == "POST"
    assert _body(captured) == {"recipe_id": "r1"}


def test_produce_inline_recipe(c, captured):
    c.produce("src1", content_mode="funny", count=3, aspect="1:1")
    assert _body(captured) == {"content_mode": "funny", "count": 3, "aspect": "1:1"}


def test_recipes_full_crud(c, captured):
    c.list_recipes()
    assert captured[-1]["method"] == "GET" and captured[-1]["url"].endswith("/api/v1/recipes")
    c.get_recipe("r1")
    assert captured[-1]["method"] == "GET" and captured[-1]["url"].endswith("/api/v1/recipes/r1")
    c.create_recipe({"name": "R"})
    assert captured[-1]["method"] == "POST" and _body(captured) == {"name": "R"}
    c.update_recipe("r1", {"count": 3})
    assert captured[-1]["method"] == "PATCH" and captured[-1]["url"].endswith("/api/v1/recipes/r1")
    assert _body(captured) == {"count": 3}
    c.delete_recipe("r1")
    assert captured[-1]["method"] == "DELETE" and captured[-1]["url"].endswith("/api/v1/recipes/r1")


def test_watches_full_crud_and_scan(c, captured):
    c.list_watches()
    assert captured[-1]["method"] == "GET" and captured[-1]["url"].endswith("/api/v1/watches")
    c.get_watch("w1")
    assert captured[-1]["url"].endswith("/api/v1/watches/w1")
    c.create_watch({"kind": "folder", "target": "/in", "recipe_id": "r1"})
    assert captured[-1]["method"] == "POST" and _body(captured)["kind"] == "folder"
    c.update_watch("w1", {"enabled": False})
    assert captured[-1]["method"] == "PATCH" and _body(captured) == {"enabled": False}
    c.delete_watch("w1")
    assert captured[-1]["method"] == "DELETE" and captured[-1]["url"].endswith("/api/v1/watches/w1")
    c.scan_watch("w1")
    assert captured[-1]["method"] == "POST" and captured[-1]["url"].endswith("/api/v1/watches/w1/scan")


def test_brand_kits_crud(c, captured):
    c.list_brand_kits()
    assert captured[-1]["method"] == "GET" and captured[-1]["url"].endswith("/api/v1/brand-kits")
    c.create_brand_kit({"name": "K", "watermark": "@acme"})
    assert captured[-1]["method"] == "POST" and _body(captured)["watermark"] == "@acme"
    c.update_brand_kit("k1", {"watermark": "@x"})
    assert captured[-1]["method"] == "PATCH" and captured[-1]["url"].endswith("/api/v1/brand-kits/k1")
    c.delete_brand_kit("k1")
    assert captured[-1]["method"] == "DELETE" and captured[-1]["url"].endswith("/api/v1/brand-kits/k1")


# ---- parity additions: settings / word-edit / timeline signals / render fetch ----

def test_settings_get_and_patch(c, captured):
    c.get_settings()
    assert captured[-1]["method"] == "GET" and captured[-1]["url"].endswith("/api/v1/settings")
    c.update_settings({"fast": True})
    assert captured[-1]["method"] == "PATCH" and _body(captured) == {"fast": True}


def test_contract_fixture_edit_word_and_dismiss_transcribe(c, captured):
    request = PHASE0_CONTRACT["word_edit"]["request"]
    c.edit_word("t1", 3, request["op"], w=request["w"])
    assert captured[-1]["method"] == "POST" and captured[-1]["url"].endswith("/api/v1/transcripts/t1/words/3")
    assert _body(captured) == request
    c.edit_word("t1", 4, "delete")
    assert _body(captured) == {"op": "delete"}                       # no w key when not given
    c.dismiss_transcribe("t1")
    assert captured[-1]["method"] == "POST" and captured[-1]["url"].endswith("/api/v1/transcripts/t1/dismiss")


def test_edit_word_legacy_python_keyword_still_sends_canonical_w(c, captured):
    c.edit_word("t1", 3, "set_text", text="legacy caller")
    assert _body(captured) == {"op": "set_text", "w": "legacy caller"}


def test_source_signals_windowed(c, captured):
    c.source_energy("s1", buckets=64, start=10, end=40)
    assert captured[-1]["url"] == "http://x/api/v1/sources/s1/energy?buckets=64&start=10.0&end=40.0"
    c.source_scenes("s1", start=10, end=40)
    assert captured[-1]["url"].endswith("/api/v1/sources/s1/scenes?start=10.0&end=40.0")
    c.source_filmstrip("s1", start=10, end=40, frames=8)
    assert captured[-1]["url"].endswith("/api/v1/sources/s1/filmstrip?start=10.0&end=40.0&frames=8")


def test_source_signal_client_can_explicitly_disable_durable_cache(c, captured):
    c.source_energy("s1", use_cache=False)
    assert captured[-1]["url"] == "http://x/api/v1/sources/s1/energy?buckets=96&use_cache=0"
    c.source_filmstrip("s1", start=10, end=40, use_cache=False)
    assert captured[-1]["url"].endswith(
        "/api/v1/sources/s1/filmstrip?start=10.0&end=40.0&frames=12&use_cache=0"
    )
