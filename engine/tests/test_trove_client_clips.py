"""Tests for TroveClient's clip methods — assert each builds the right URL / verb / body.

Same monkeypatched-urlopen discipline as test_trove_client.py: no network, just the
request the client would send.
"""
from __future__ import annotations

import json

import pytest

from trove_client import TroveClient


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


def test_find_moments(c, captured):
    c.find_moments("src1", mode="insightful", count=3)
    assert captured[-1]["url"] == "http://x/api/v1/sources/src1/moments"
    assert captured[-1]["method"] == "POST"
    assert _body(captured) == {"mode": "insightful", "count": 3}


def test_find_moments_with_window(c, captured):
    c.find_moments("src1", window=(10.0, 40.0))
    assert _body(captured)["window"] == [10.0, 40.0]


def test_cut_clip(c, captured):
    c.cut_clip("src1", start=2.0, end=12.0)
    assert captured[-1]["url"] == "http://x/api/v1/sources/src1/cut"
    assert _body(captured) == {"start": 2.0, "end": 12.0}


def test_reframe_clip(c, captured):
    c.reframe_clip("clipA", aspect="9:16", mode="pan")
    assert captured[-1]["url"] == "http://x/api/v1/clips/clipA/reframe"
    assert _body(captured) == {"aspect": "9:16", "mode": "pan"}


def test_reframe_clip_with_rois(c, captured):
    rois = {"left": {"x": 0, "y": 0, "w": 1, "h": 1}, "right": {"x": 1, "y": 0, "w": 1, "h": 1}}
    c.reframe_clip("clipA", rois=rois)
    assert _body(captured)["rois"] == rois


def test_caption_clip(c, captured):
    c.caption_clip("clipA", style="karaoke")
    assert captured[-1]["url"] == "http://x/api/v1/clips/clipA/captions"
    assert _body(captured) == {"style": "karaoke"}


def test_render_clip(c, captured):
    c.render_clip("clipA", preset="reels", fast=False)
    assert captured[-1]["url"] == "http://x/api/v1/clips/clipA/renders"
    assert _body(captured) == {"preset": "reels", "fast": False}


def test_render_pipeline(c, captured):
    c.render_pipeline("src1", start=1.0, end=9.0, aspect="1:1", style="minimal", preset="shorts")
    assert captured[-1]["url"] == "http://x/api/v1/sources/src1/render"
    b = _body(captured)
    assert b["start"] == 1.0 and b["end"] == 9.0 and b["aspect"] == "1:1"
    assert b["style"] == "minimal" and b["preset"] == "shorts" and b["mode"] == "pan"


def test_list_clip_jobs_default_url(c, captured):
    c.list_clip_jobs()
    assert captured[-1]["url"] == "http://x/api/v1/clip-jobs"
    assert captured[-1]["method"] == "GET"


def test_list_clip_jobs_with_kind_and_status(c, captured):
    c.list_clip_jobs(kind="cut", status="done")
    url = captured[-1]["url"]
    assert url.startswith("http://x/api/v1/clip-jobs?")
    assert "kind=cut" in url and "status=done" in url


def test_get_clip_job(c, captured):
    c.get_clip_job("j1")
    assert captured[-1]["url"] == "http://x/api/v1/clip-jobs/j1"
    assert captured[-1]["method"] == "GET"


def test_cancel_and_dismiss_clip_job(c, captured):
    c.cancel_clip_job("j1")
    assert captured[-1]["url"] == "http://x/api/v1/clip-jobs/j1/cancel" and captured[-1]["method"] == "POST"
    c.dismiss_clip_job("j2")
    assert captured[-1]["url"] == "http://x/api/v1/clip-jobs/j2/dismiss"


def test_download_render_streams_to_file(c, captured, tmp_path):
    out = tmp_path / "r.mp4"
    res = c.download_render("clipA", "rend1", stream_to=str(out))
    assert captured[-1]["url"] == "http://x/api/v1/clips/clipA/renders/rend1/file"
    assert res == {"saved_to": str(out)}
