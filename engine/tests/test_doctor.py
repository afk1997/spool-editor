"""Tests for the dependency-doctor endpoint (/api/v1/doctor).

Drives the onboarding / dependency-doctor screen (spec §5.10). Like /health
and /capabilities it must be reachable without a token. Mirrors the fixture +
shape-contract style of test_api_v1.py.
"""
from __future__ import annotations

import pytest
from app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    import app as _app_module
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", tmp_path / "downloads")
    app = create_app()
    return app, app.test_client()


def test_doctor_reports_machine_tools_encoders(client):
    _, c = client
    r = c.get("/api/v1/doctor")
    assert r.status_code == 200
    body = r.get_json()

    # machine probe block
    for k in ("os_name", "arch", "cpu_cores", "gpu"):
        assert k in body["machine"], k

    # tool entries — fixed shape; present/ok are always bools
    for t in ("python", "ffmpeg", "yt_dlp", "whisper_cpp"):
        entry = body["tools"][t]
        assert set(entry.keys()) == {"present", "version", "ok"}, t
        assert isinstance(entry["present"], bool)
        assert isinstance(entry["ok"], bool)

    # python is the one tool guaranteed present (we're running on it)
    assert body["tools"]["python"]["present"] is True
    assert body["tools"]["python"]["version"]

    assert isinstance(body["encoders"], list)
    assert isinstance(body["ok"], bool)


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_doctor_requires_configured_token(monkeypatch, tmp_path, authorization):
    """A protected/public engine cannot leak its machine fingerprint."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("TROVE_TOKEN", "secret-xyz")
    app = create_app()
    c = app.test_client()
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = c.get("/api/v1/doctor", headers=headers)
    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}


def test_doctor_accepts_configured_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("TROVE_TOKEN", "secret-xyz")
    app = create_app()
    c = app.test_client()
    response = c.get(
        "/api/v1/doctor",
        headers={"Authorization": "Bearer secret-xyz"},
    )
    assert response.status_code == 200
    assert "machine" in response.get_json()


def test_openapi_marks_doctor_as_token_protected(client):
    _, c = client
    summary = c.get("/api/v1/openapi.json").get_json()["paths"]["/doctor"]["get"][
        "summary"
    ]
    assert "token" in summary.lower()
    assert "unauthenticated" not in summary.lower()
