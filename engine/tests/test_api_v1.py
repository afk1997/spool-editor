"""Tests for the /api/v1 JSON blueprint (CLI + MCP backbone).

These cover the stable contract: shapes, status codes, idempotence
guards, and the auth boundary. Heavy operations (real downloads,
real whisper) are stubbed via the same monkeypatch points the
existing endpoint tests use.
"""
from __future__ import annotations
import os
import time
import pytest
from app import create_app
from jobs import Job, JobStatus
import transcribe_jobs


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    # Isolate download dir so storage / search tests don't see real
    # files (and don't write transcribe_jobs.json into the repo).
    import app as _app_module
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", tmp_path / "downloads")
    app = create_app()
    return app, app.test_client()


# ---- meta -----------------------------------------------------------

def test_health_is_unauthenticated_and_ok(client):
    _, c = client
    r = c.get("/api/v1/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["version"] == "v1"


# ---- capabilities ---------------------------------------------------

def test_capabilities_shape_and_unauthenticated(client, monkeypatch):
    """The registry must be reachable without a token (clients use it
    to *discover* whether auth is required) and must expose the keys
    downstream callers branch on. Pin the contract — adding fields is
    fine, removing or renaming is a wire-breaking change."""
    # Ensure auth is off for this assertion path.
    monkeypatch.delenv("TROVE_TOKEN", raising=False)
    _, c = client
    r = c.get("/api/v1/capabilities")
    assert r.status_code == 200
    body = r.get_json()
    assert body["api_version"] == "v1"
    assert body["schema_version"] >= 2
    assert body["auth_required"] is False
    # Top-level shape contract.
    for key in ("features", "formats", "scopes", "limits", "openapi_url"):
        assert key in body, key
    # Feature flags every client expects to see, regardless of value.
    for f in ("diarization", "transcripts", "sse_events",
              "idempotency_keys", "transcript_chunk", "transcript_search"):
        assert f in body["features"], f
    # Export formats == the ones /chunk + /export.<fmt> accept.
    assert set(body["formats"]["transcript_export"]) == {"txt", "srt", "vtt", "json"}
    # Scopes match safety.SCOPE_* — required so MCP/CLI can name them
    # without re-importing the server module.
    assert body["scopes"]["transcript_export"] == "transcript-export"
    # Chunk caps are surfaced so callers can size their pagination
    # loop without round-tripping a probe page first.
    chunk = body["limits"]["transcript_chunk"]
    assert chunk["text_default_bytes"] == 4000
    assert chunk["text_max_bytes"] == 64000
    assert chunk["json_default_segments"] == 50
    assert chunk["json_max_segments"] == 500


def test_capabilities_reflects_auth_required(client, monkeypatch):
    """Setting ``TROVE_TOKEN`` must flip ``auth_required`` so a fresh
    client knows to start sending Authorization headers — this is the
    whole point of an unauthenticated capabilities probe."""
    monkeypatch.setenv("TROVE_TOKEN", "secret-xyz")
    _, c = client
    body = c.get("/api/v1/capabilities").get_json()
    assert body["auth_required"] is True
    assert body["features"]["signed_urls"] is True


def test_capabilities_reflects_live_runtime_objects(client):
    """Architect P1 regression: limits MUST be sourced from the live
    JobManager / RateLimiter / extensions dict, not from import-time
    module globals. A per-process tweak (e.g. swapping the JobManager
    worker count for a test) must show through to capability
    consumers, otherwise automation reads stale limits and over-/
    under-shoots them."""
    app, c = client
    # Mutate the live runtime objects (not env) and assert the
    # registry reflects the change.
    app.extensions["trove.jobs"].max_workers = 7
    app.extensions["trove.jobs"].ttl_seconds = 1234
    app.extensions["trove.rate_limiter"].rate = 99
    app.extensions["trove.batch_max"] = 13

    body = c.get("/api/v1/capabilities").get_json()
    lim = body["limits"]
    assert lim["max_workers"] == 7
    assert lim["job_ttl_seconds"] == 1234
    assert lim["rate_limit_per_minute"] == 99
    assert lim["batch_max_urls"] == 13


def test_capabilities_exposes_idempotency_policy(client):
    """Operators wiring retry logic need the header name + TTL +
    capacity surfaced explicitly so they don't have to read the
    source to size their retry strategy."""
    _, c = client
    idem = c.get("/api/v1/capabilities").get_json()["idempotency"]
    assert idem["header_name"] == "Idempotency-Key"
    assert idem["ttl_seconds"] >= 60
    assert idem["capacity"] >= 1


def test_capabilities_reflects_diarization_flag(client, monkeypatch):
    """``TROVE_DIARIZATION=on`` is the operator-facing toggle; a
    capabilities consumer must see it flip without restarting."""
    monkeypatch.setenv("TROVE_DIARIZATION", "off")
    _, c = client
    assert c.get("/api/v1/capabilities").get_json()["features"]["diarization"] is False
    monkeypatch.setenv("TROVE_DIARIZATION", "on")
    # Note: ``is_enabled()`` still returns False if heavy deps aren't
    # installed in the test env — that's correct, the capability is
    # gated on (flag AND deps).  We only assert the read path works
    # and returns a bool (not e.g. None or an env string).
    body = c.get("/api/v1/capabilities").get_json()
    assert isinstance(body["features"]["diarization"], bool)


# ---- jobs read ------------------------------------------------------

def test_list_jobs_returns_list(client):
    # Note: DOWNLOAD_DIR / jobs.json is anchored at __file__'s parent,
    # so persisted state from a running dev server can leak into tests.
    # We assert shape (list of dicts), not emptiness.
    app, c = client
    app.extensions["trove.jobs"]._jobs.clear()
    r = c.get("/api/v1/jobs")
    assert r.status_code == 200
    body = r.get_json()
    assert body["jobs"] == []
    assert body["total"] == 0 and body["returned"] == 0


def test_get_job_404(client):
    _, c = client
    r = c.get("/api/v1/jobs/nope")
    assert r.status_code == 404
    assert r.get_json()["error"] == "not_found"


def test_get_job_returns_view_shape(client):
    app, c = client
    jm = app.extensions["trove.jobs"]
    j = Job(id="abc", url="https://x", title="t", status=JobStatus.DONE,
            filename="t.mp4", file_path="/tmp/whatever")
    jm._jobs["abc"] = j
    r = c.get("/api/v1/jobs/abc")
    assert r.status_code == 200
    body = r.get_json()
    assert body["id"] == "abc"
    assert body["status"] == "done"
    assert body["url"] == "https://x"
    # Field set is part of the contract; do not silently drop fields.
    assert {"filename", "downloaded_bytes", "auto_transcribe"}.issubset(body.keys())


# ---- jobs write -----------------------------------------------------

def test_submit_job_validates_url(client):
    _, c = client
    r = c.post("/api/v1/jobs", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "missing_url"


def test_submit_job_rejects_argument_injection(client):
    _, c = client
    r = c.post("/api/v1/jobs", json={"url": "--exec=touch /tmp/pwned"})
    assert r.status_code == 400


def test_submit_job_calls_enqueue_with_supplied_title(client, monkeypatch):
    app, c = client
    captured = {}

    def fake_enqueue(url, fmt, fmt_id, title, thumbnail="", *, auto_transcribe=False,
                     subtitles=False, chapters=False, embed=False, resolve_title=False):
        captured.update(dict(
            url=url, fmt=fmt, fmt_id=fmt_id, title=title,
            thumbnail=thumbnail, auto_transcribe=auto_transcribe,
        ))
        # mimic real submit
        jm = app.extensions["trove.jobs"]
        j = Job(id="newid1", url=url, title=title, status=JobStatus.QUEUED)
        jm._jobs["newid1"] = j
        return "newid1"

    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    r = c.post("/api/v1/jobs", json={
        "url": "https://example.com/video",
        "format": "audio",
        "title": "My clip",
        "auto_transcribe": True,
    })
    assert r.status_code == 201
    body = r.get_json()
    assert body["id"] == "newid1"
    assert captured["title"] == "My clip"
    assert captured["fmt"] == "audio"
    assert captured["auto_transcribe"] is True


def test_submit_job_forwards_download_opts(client):
    """subtitles/chapters/embed in the body reach enqueue_download (→ yt-dlp flags)."""
    app, c = client
    captured = {}

    def fake_enqueue(url, fmt, fmt_id, title, thumbnail="", *, auto_transcribe=False,
                     subtitles=False, chapters=False, embed=False, resolve_title=False):
        captured.update(subtitles=subtitles, chapters=chapters, embed=embed)
        jm = app.extensions["trove.jobs"]
        jm._jobs["optid"] = Job(id="optid", url=url, title=title, status=JobStatus.QUEUED)
        return "optid"

    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    r = c.post("/api/v1/jobs", json={
        "url": "https://example.com/v", "title": "t",
        "subtitles": True, "chapters": True, "embed": True,
    })
    assert r.status_code == 201
    assert captured == {"subtitles": True, "chapters": True, "embed": True}


def test_submit_job_busy_returns_503(client, monkeypatch):
    app, c = client

    def fake_enqueue(*a, **kw):
        raise RuntimeError("pool full")

    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    r = c.post("/api/v1/jobs", json={"url": "https://e.com", "title": "x"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "busy"


def test_pause_resume_cancel_dismiss(client):
    app, c = client
    jm = app.extensions["trove.jobs"]
    jm._jobs["jid"] = Job(id="jid", url="u", title="t",
                          status=JobStatus.DOWNLOADING)
    # pause
    r = c.post("/api/v1/jobs/jid/pause")
    assert r.status_code == 200
    assert r.get_json()["status"] == "paused"
    # resume — stub the action so we don't really call yt-dlp
    called = {}
    app.extensions["trove.actions"]["resume_job"] = (
        lambda jid: called.setdefault("jid", jid) or True
    )
    r = c.post("/api/v1/jobs/jid/resume")
    assert r.status_code == 200
    assert called["jid"] == "jid"
    # cancel
    r = c.post("/api/v1/jobs/jid/cancel")
    assert r.status_code == 200
    assert r.get_json()["status"] == "cancelled"
    # dismiss
    r = c.post("/api/v1/jobs/jid/dismiss")
    assert r.status_code == 204


def test_pause_404_for_unknown(client):
    _, c = client
    r = c.post("/api/v1/jobs/missing/pause")
    assert r.status_code == 404


def test_dismiss_refuses_active_job(client):
    app, c = client
    jm = app.extensions["trove.jobs"]
    jm._jobs["live"] = Job(id="live", url="u", title="t",
                           status=JobStatus.DOWNLOADING)
    r = c.post("/api/v1/jobs/live/dismiss")
    assert r.status_code == 404


# ---- transcripts ----------------------------------------------------

def test_list_transcripts_returns_list(client):
    app, c = client
    app.extensions["trove.transcribe"]._jobs.clear()
    r = c.get("/api/v1/transcripts")
    assert r.status_code == 200
    body = r.get_json()
    assert body["transcripts"] == []
    assert body["total"] == 0 and body["returned"] == 0


def test_start_transcribe_404_if_parent_not_done(client):
    app, c = client
    jm = app.extensions["trove.jobs"]
    jm._jobs["p"] = Job(id="p", url="u", title="t", status=JobStatus.DOWNLOADING)
    r = c.post("/api/v1/jobs/p/transcribe")
    assert r.status_code == 404
    assert r.get_json()["error"] == "parent_not_done"


def test_start_transcribe_409_when_no_active_model(client, monkeypatch):
    app, c = client
    jm = app.extensions["trove.jobs"]
    jm._jobs["p"] = Job(id="p", url="u", title="t",
                        status=JobStatus.DONE, file_path="/tmp/whatever")
    monkeypatch.setattr("models_store.get_active_path", lambda: None)
    r = c.post("/api/v1/jobs/p/transcribe")
    assert r.status_code == 409
    assert r.get_json()["error"] == "no_active_model"


def test_start_transcribe_idempotent_on_existing(client, monkeypatch, tmp_path):
    app, c = client
    jm = app.extensions["trove.jobs"]
    tm = app.extensions["trove.transcribe"]
    media = tmp_path / "m.mp4"
    media.write_bytes(b"x")
    jm._jobs["p"] = Job(id="p", url="u", title="t",
                        status=JobStatus.DONE, file_path=str(media))
    monkeypatch.setattr("models_store.get_active_path",
                        lambda: tmp_path / "model.bin")
    # Existing in-flight transcribe → return same id, don't spawn.
    existing = transcribe_jobs.TranscribeJob(
        id="t1", parent_job_id="p", model_used="m",
        status=transcribe_jobs.TranscribeStatus.RUNNING,
    )
    tm._jobs["t1"] = existing
    called = {"n": 0}
    app.extensions["trove.actions"]["start_transcribe"] = (
        lambda pid: (called.update(n=called["n"] + 1) or "should_not_use")
    )
    r = c.post("/api/v1/jobs/p/transcribe")
    assert r.status_code == 200
    assert r.get_json()["id"] == "t1"
    assert called["n"] == 0  # idempotent


# ---- models ---------------------------------------------------------

def test_list_models_shape(client):
    _, c = client
    r = c.get("/api/v1/models")
    assert r.status_code == 200
    body = r.get_json()
    assert "active" in body
    assert isinstance(body["models"], list)
    assert all({"name", "label", "is_installed", "is_active"}.issubset(m.keys())
               for m in body["models"])


def test_use_model_unknown_400(client):
    _, c = client
    r = c.post("/api/v1/models/bogus/use")
    assert r.status_code == 400


def test_use_model_not_installed_409(client, monkeypatch, tmp_path):
    _, c = client
    monkeypatch.setattr("models_store.MODELS_DIR", tmp_path)
    r = c.post("/api/v1/models/ggml-tiny.bin/use")
    assert r.status_code == 409
    assert r.get_json()["error"] == "not_installed"


def test_install_progress_endpoint(client):
    _, c = client
    r = c.get("/api/v1/models/install-progress")
    assert r.status_code == 200
    body = r.get_json()
    assert "downloading" in body


# ---- progress / human fields ---------------------------------------

def test_job_view_includes_human_progress(client, monkeypatch):
    """The MCP / CLI clients rely on a ``human`` block + computed
    ``progress_pct`` / ``elapsed_seconds`` so they can give a useful
    live status without re-implementing formatting on every poll."""
    app, c = client
    jm = app.extensions["trove.jobs"]
    from jobs import Job
    job = Job(
        id="hview1", url="https://example.com/v", title="Big sample",
        status=JobStatus.DOWNLOADING,
        downloaded_bytes=12_400_000, total_bytes=29_700_000,
        speed=5_200_000.0, eta=3,
    )
    with jm._lock:
        jm._jobs["hview1"] = job

    r = c.get(f"/api/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.get_json()
    # Raw machine-readable fields
    assert body["progress_pct"] == 41
    assert body["elapsed_seconds"] >= 0
    assert body["speed_bps"] == 5_200_000.0
    # Human-readable block
    h = body["human"]
    assert h["progress"] == "41%"
    assert h["downloaded"] == "11.8 MB"  # 12.4M binary
    assert h["size"] == "28.3 MB"        # 29.7M binary
    assert h["speed"] == "5.0 MB/s"
    assert h["eta"] == "0:03"
    assert "downloading" in h["summary"]
    assert "41%" in h["summary"]
    assert "5.0 MB/s" in h["summary"]


def test_transcript_view_includes_human_progress(client):
    app, c = client
    tm = app.extensions["trove.transcribe"]
    tj = transcribe_jobs.TranscribeJob(
        id="t1", parent_job_id="p1", model_used="ggml-tiny.bin",
        progress_pct=42, duration_seconds=552.0, language_detected="en",
        status=transcribe_jobs.TranscribeStatus.RUNNING,
    )
    with tm._lock:
        tm._jobs["t1"] = tj
    r = c.get("/api/v1/transcripts/t1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["progress_pct"] == 42
    assert body["duration_seconds"] == 552.0
    assert body["elapsed_seconds"] >= 0
    h = body["human"]
    assert h["progress"] == "42%"
    assert h["audio_duration"] == "9:12"
    assert "running" in h["summary"]
    assert "42%" in h["summary"]
    assert "ggml-tiny.bin" in h["summary"]


# ---- rate-limit exemption scope ------------------------------------

def _swap_rate_limiter(app, rate=2, window=60):
    """Replace the live rate limiter so we can test the rate-limit
    branch deterministically without reaching for module-level env
    state (RATE_LIMIT_PER_MIN is read at import time)."""
    from safety import RateLimiter
    new = RateLimiter(rate=rate, per_seconds=window)
    # The /before_request closure reads `rate_limiter` from the
    # enclosing scope, but it's also stashed on app.extensions for
    # exactly this purpose. Patch both surfaces.
    app.extensions["trove.rate_limiter"] = new
    # Patch every closure that captures `rate_limiter` — the
    # before_request gate AND the after_request header hook.
    patched = 0
    targets = [
        *app.before_request_funcs.get(None, []),
        *app.after_request_funcs.get(None, []),
    ]
    for fn in targets:
        cells = fn.__closure__ or ()
        names = fn.__code__.co_freevars
        for name, cell in zip(names, cells):
            if name == "rate_limiter":
                cell.cell_contents = new
                patched += 1
    if patched == 0:
        raise RuntimeError("could not find rate_limiter closures to patch")


def test_rate_limit_exempts_status_polls(client):
    app, c = client
    _swap_rate_limiter(app, rate=2)
    # 50 status polls in a row must all succeed (poll exemption).
    for _ in range(50):
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/api/v1/jobs").status_code == 200
        assert c.get("/api/v1/jobs/no-such-id").status_code == 404
        assert c.get("/api/v1/transcripts").status_code == 200


def test_rate_limit_does_not_exempt_file_or_export(client):
    """Bandwidth-heavy GETs must stay rate-limited so a token-less
    deployment isn't a free egress vector. The exemption helper has
    a regression-prone history (see _is_poll_exempt comments)."""
    app, c = client
    _swap_rate_limiter(app, rate=2)
    seen = [c.get("/api/v1/jobs/x/file").status_code for _ in range(5)]
    assert 429 in seen, f"file GET should hit rate limit; got {seen}"
    _swap_rate_limiter(app, rate=2)  # fresh bucket
    seen = [c.get("/api/v1/transcripts/x/export.txt").status_code for _ in range(5)]
    assert 429 in seen, f"export GET should hit rate limit; got {seen}"


# ---- auth boundary --------------------------------------------------

def test_token_required_when_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("TROVE_TOKEN", "secret-xyz")
    app = create_app()
    c = app.test_client()
    # /health is open
    assert c.get("/api/v1/health").status_code == 200
    # /jobs requires the token
    assert c.get("/api/v1/jobs").status_code in (401, 403)
    # With the right token it works
    r = c.get("/api/v1/jobs", headers={"Authorization": "Bearer secret-xyz"})
    assert r.status_code == 200


# ---- pagination / filtering ----------------------------------------

def _seed_jobs(app, n: int):
    """Drop *n* fake jobs into the JobManager (skipping the worker)."""
    jm = app.extensions["trove.jobs"]
    jm._jobs.clear()
    for i in range(n):
        jid = f"j{i:03d}"
        st = JobStatus.DONE if i % 2 == 0 else JobStatus.ERROR
        jm._jobs[jid] = Job(id=jid, url=f"https://x/{i}",
                             title=f"Clip {i}", status=st)


def test_list_jobs_pagination(client):
    app, c = client
    _seed_jobs(app, 25)
    r = c.get("/api/v1/jobs?limit=10&offset=5&order=oldest")
    body = r.get_json()
    assert body["total"] == 25
    assert body["returned"] == 10
    assert body["limit"] == 10 and body["offset"] == 5
    # oldest order: j005..j014
    assert [j["id"] for j in body["jobs"]] == [f"j{i:03d}" for i in range(5, 15)]


def test_list_jobs_status_filter(client):
    app, c = client
    _seed_jobs(app, 6)  # 3 done, 3 error
    r = c.get("/api/v1/jobs?status=done")
    body = r.get_json()
    assert body["total"] == 3
    assert all(j["status"] == "done" for j in body["jobs"])
    # Comma-separated filter
    r = c.get("/api/v1/jobs?status=done,error")
    assert r.get_json()["total"] == 6


def test_list_jobs_clamps_limit(client):
    app, c = client
    _seed_jobs(app, 3)
    r = c.get("/api/v1/jobs?limit=99999")
    assert r.get_json()["limit"] == 500


def test_list_transcripts_pagination(client):
    app, c = client
    tm = app.extensions["trove.transcribe"]
    tm._jobs.clear()
    for i in range(7):
        tid = f"t{i}"
        tm._jobs[tid] = transcribe_jobs.TranscribeJob(
            id=tid, parent_job_id="p", model_used="m",
            status=transcribe_jobs.TranscribeStatus.DONE if i < 4
            else transcribe_jobs.TranscribeStatus.ERROR,
        )
    r = c.get("/api/v1/transcripts?status=done&limit=3")
    body = r.get_json()
    assert body["total"] == 4 and body["returned"] == 3


# ---- bulk submit ----------------------------------------------------

def test_submit_bulk_partial_failure(client):
    # After the bulk-no-probe fix, _submit_one is called with probe=False so
    # run_info is NOT invoked on the request thread — no monkeypatch needed.
    app, c = client
    def fake_enqueue(url, fmt, fmt_id, title, thumbnail="", *, auto_transcribe=False,
                     subtitles=False, chapters=False, embed=False, resolve_title=False):
        jm = app.extensions["trove.jobs"]
        jid = f"id{len(jm._jobs)}"
        jm._jobs[jid] = Job(id=jid, url=url, title=title, status=JobStatus.QUEUED)
        return jid
    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    r = c.post("/api/v1/jobs/bulk", json={
        "urls": [
            "https://example.com/ok-1",
            "--exec=evil",                # rejected by safety
            "https://example.com/ok-2",
        ],
        "format": "audio",
    })
    assert r.status_code == 207  # multi-status because one failed
    body = r.get_json()
    assert body["submitted"] == 2 and body["failed"] == 1
    assert body["results"][1]["error"] == "unsupported_url"


def test_submit_bulk_rejects_empty(client):
    _, c = client
    assert c.post("/api/v1/jobs/bulk", json={}).status_code == 400
    assert c.post("/api/v1/jobs/bulk", json={"urls": []}).status_code == 400


def test_submit_bulk_caps_at_50(client):
    # Limit was lowered from 100 to 50; cap fires at 51 URLs.
    _, c = client
    r = c.post("/api/v1/jobs/bulk", json={"urls": ["https://example.com/v"] * 51})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "too_many_urls" and body["limit"] == 50


def test_bulk_submit_does_not_probe_on_the_request_thread(client):
    # _submit_one is called with probe=False on the bulk path; run_info must
    # NOT be called synchronously.  We monkey-patch runner.run_info to raise
    # AssertionError — since _submit_one does `from runner import run_info`
    # at call time, patching the module attribute intercepts any request-thread
    # probe.  app.py imports run_info at module top (app.run_info) so the
    # worker's copy is unaffected.
    import runner
    import app as _app_module
    original_run_info = runner.run_info
    runner.run_info = lambda url: (_ for _ in ()).throw(
        AssertionError("run_info called on request thread"))

    app_obj, c = client

    # Stub enqueue_download so no real worker or yt-dlp fires.
    def fake_enqueue(url, fmt, fmt_id, title, thumbnail="", *, auto_transcribe=False,
                     subtitles=False, chapters=False, embed=False, resolve_title=False):
        jm = app_obj.extensions["trove.jobs"]
        jid = f"bulk{len(jm._jobs)}"
        jm._jobs[jid] = Job(id=jid, url=url, title=title, status=JobStatus.QUEUED)
        return jid
    app_obj.extensions["trove.actions"]["enqueue_download"] = fake_enqueue

    try:
        r = c.post("/api/v1/jobs/bulk", json={
            "urls": [f"https://93.184.216.34/v{i}" for i in range(3)]})
    finally:
        runner.run_info = original_run_info

    assert r.status_code in (201, 207)
    data = r.get_json()
    assert data["submitted"] == 3
    for row in data["results"]:
        # Placeholder title: URL itself (real title resolved by worker)
        assert row["title"] == row["url"]


# ---- idempotency ----------------------------------------------------

def test_idempotency_replays_same_job(client):
    app, c = client
    calls = {"n": 0}
    def fake_enqueue(url, *a, **kw):
        calls["n"] += 1
        jm = app.extensions["trove.jobs"]
        j = Job(id="only-id", url=url, title="t", status=JobStatus.QUEUED)
        jm._jobs["only-id"] = j
        return "only-id"
    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    body = {"url": "https://example.com/a", "title": "T"}
    headers = {"Idempotency-Key": "deadbeef-1"}
    r1 = c.post("/api/v1/jobs", json=body, headers=headers)
    assert r1.status_code == 201
    r2 = c.post("/api/v1/jobs", json=body, headers=headers)
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.get_json()["id"] == r1.get_json()["id"]
    assert calls["n"] == 1   # never re-enqueued


def test_idempotency_different_keys_create_distinct_jobs(client):
    app, c = client
    counter = {"n": 0}
    def fake_enqueue(url, *a, **kw):
        counter["n"] += 1
        jid = f"id{counter['n']}"
        jm = app.extensions["trove.jobs"]
        jm._jobs[jid] = Job(id=jid, url=url, title="t", status=JobStatus.QUEUED)
        return jid
    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    body = {"url": "https://example.com/x", "title": "X"}
    a = c.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "k1"})
    b = c.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "k2"})
    assert a.get_json()["id"] != b.get_json()["id"]


# ---- rate-limit headers --------------------------------------------

def test_rate_limit_headers_present_on_normal_response(client):
    app, c = client
    _swap_rate_limiter(app, rate=10)
    r = c.get("/api/v1/health")
    assert r.headers.get("X-RateLimit-Limit") == "10"
    assert int(r.headers.get("X-RateLimit-Remaining")) <= 10
    assert r.headers.get("X-RateLimit-Window") == "60"


def test_rate_limit_429_carries_retry_after(client):
    app, c = client
    _swap_rate_limiter(app, rate=2)
    # /jobs/<id>/file is NOT poll-exempt → eats the bucket
    seen = []
    for _ in range(5):
        r = c.get("/api/v1/jobs/no-such/file")
        seen.append((r.status_code, r.headers.get("Retry-After")))
    rate_limited = [s for s in seen if s[0] == 429]
    assert rate_limited, f"expected a 429 in {seen}"
    assert all(s[1] is not None for s in rate_limited)


# ---- storage / du ---------------------------------------------------

def test_storage_empty(client):
    _, c = client
    r = c.get("/api/v1/storage")
    body = r.get_json()
    assert body["total_bytes"] == 0 and body["file_count"] == 0
    assert body["by_job"] == [] and body["orphan_files"] == []


def test_storage_attributes_files_to_jobs(client, tmp_path):
    app, c = client
    # Seed one fake job and one matching file in the configured download dir
    jm = app.extensions["trove.jobs"]
    jm._jobs["abc"] = Job(id="abc", url="https://x", title="The Clip", status=JobStatus.DONE)
    dd = app.extensions["trove.download_dir"]
    os.makedirs(dd, exist_ok=True)
    (dd / "abc.mp4").write_bytes(b"x" * 100)
    (dd / "stray.bin").write_bytes(b"y" * 25)
    body = c.get("/api/v1/storage").get_json()
    assert body["total_bytes"] == 125
    assert body["file_count"] == 2
    assert body["by_job"][0]["id"] == "abc"
    assert body["by_job"][0]["bytes"] == 100
    assert body["by_job"][0]["title"] == "The Clip"
    assert body["orphan_bytes"] == 25
    assert body["orphan_files"][0]["name"] == "stray.bin"


# ---- transcript search ---------------------------------------------

def test_search_requires_query(client):
    _, c = client
    r = c.get("/api/v1/transcripts/search")
    assert r.status_code == 400


def test_search_returns_matches_with_snippet(client, tmp_path):
    app, c = client
    # Seed a parent job with a real on-disk .words.json
    dd = app.extensions["trove.download_dir"]
    os.makedirs(dd, exist_ok=True)
    media = dd / "abc.mp4"
    media.write_bytes(b"x")
    words_path = str(dd / "abc.words.json")
    import transcript_io as tio
    import json as _json
    with open(words_path, "w") as f:
        _json.dump({
            "schema_version": 2,
            "duration": 5.0,
            "words": [
                {"idx": 0, "w": "Hello",   "original_w": "Hello",
                 "start": 0.0, "end": 0.5, "edited": False, "deleted": False},
                {"idx": 1, "w": "machine", "original_w": "machine",
                 "start": 0.5, "end": 1.2, "edited": False, "deleted": False},
                {"idx": 2, "w": "learning","original_w": "learning",
                 "start": 1.2, "end": 2.1, "edited": False, "deleted": False},
            ],
            "segments": [],
        }, f)
    # Now wire up parent job + transcribe job
    jm = app.extensions["trove.jobs"]
    jm._jobs["abc"] = Job(id="abc", url="https://x", title="ML clip",
                          status=JobStatus.DONE, file_path=str(media))
    tm = app.extensions["trove.transcribe"]
    tm._jobs["t1"] = transcribe_jobs.TranscribeJob(
        id="t1", parent_job_id="abc", model_used="m",
        status=transcribe_jobs.TranscribeStatus.DONE,
    )
    r = c.get("/api/v1/transcripts/search?q=machine")
    body = r.get_json()
    assert body["returned"] == 1
    m = body["matches"][0]
    assert m["transcript_id"] == "t1"
    assert m["title"] == "ML clip"
    assert "machine" in m["snippet"].lower()
    assert m["start_seconds"] == pytest.approx(0.5)


def test_search_stays_correct_after_a_word_edit(client, tmp_path):
    """The FTS5 candidate filter must never go stale: editing a word to introduce a new term,
    then searching it, must still find the transcript (the word-edit re-indexes it). Without the
    re-index the stale filter would wrongly skip it — a false negative. Also confirms an edited-
    away term stops matching (substring semantics preserved end-to-end)."""
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())  # "Hello machine learning"
    # First search populates the index for t1 (lazy backfill).
    assert c.get("/api/v1/transcripts/search?q=machine").get_json()["returned"] == 1
    # Introduce a brand-new term via an edit — this re-indexes t1.
    assert c.post("/api/v1/transcripts/t1/words/2", json={"op": "set_text", "w": "elephants"}).status_code == 200
    # The new term is found (mid-token substring too) and the old term is gone.
    found = c.get("/api/v1/transcripts/search?q=elephant").get_json()
    assert found["returned"] == 1 and found["matches"][0]["transcript_id"] == "t1"
    assert c.get("/api/v1/transcripts/search?q=learning").get_json()["returned"] == 0


# ---- chunked transcript read ---------------------------------------

def _seed_done_transcript(app, tmp_path, *, body_text: str | None = None,
                          words_data: dict | None = None):
    """Drop a finished parent job + transcribe job + on-disk artifacts.

    Returns the artifact base path so callers can stat the rendered
    files. The parent's ``file_path`` lives under the configured
    download dir so the export / chunk endpoint's ``os.path.exists``
    check passes the same way it does in production.
    """
    import json as _json
    dd = app.extensions["trove.download_dir"]
    os.makedirs(dd, exist_ok=True)
    media = dd / "abc.mp4"
    media.write_bytes(b"x")
    base = str(dd / "abc")
    if body_text is not None:
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(body_text)
    if words_data is not None:
        with open(base + ".words.json", "w") as f:
            _json.dump(words_data, f)
    jm = app.extensions["trove.jobs"]
    jm._jobs["abc"] = Job(id="abc", url="https://x", title="Clip",
                          status=JobStatus.DONE, file_path=str(media))
    tm = app.extensions["trove.transcribe"]
    tm._jobs["t1"] = transcribe_jobs.TranscribeJob(
        id="t1", parent_job_id="abc", model_used="m",
        status=transcribe_jobs.TranscribeStatus.DONE,
    )
    return base


# ---- transcript word editing (drives caption re-burn + the ripple cut) ----

def _editable_words():
    return {"schema_version": 2, "duration": 2.1,
            "segments": [{"start": 0.0, "end": 2.1, "word_idxs": [0, 1, 2], "speaker": None}],
            "words": [
                {"idx": 0, "w": "Hello", "original_w": "Hello", "start": 0.0, "end": 0.5, "edited": False, "deleted": False},
                {"idx": 1, "w": "machine", "original_w": "machine", "start": 0.5, "end": 1.2, "edited": False, "deleted": False},
                {"idx": 2, "w": "learning", "original_w": "learning", "start": 1.2, "end": 2.1, "edited": False, "deleted": False}]}


def test_edit_transcript_word_set_text_and_delete(client, tmp_path):
    app, c = client
    base = _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    r = c.post("/api/v1/transcripts/t1/words/1", json={"op": "set_text", "w": "robot"})
    assert r.status_code == 200 and r.get_json()["word"]["w"] == "robot"
    r2 = c.post("/api/v1/transcripts/t1/words/2", json={"op": "delete"})
    assert r2.status_code == 200 and r2.get_json()["word"]["deleted"] is True
    import json as _json
    saved = _json.load(open(base + ".words.json"))
    assert saved["words"][1]["w"] == "robot" and saved["words"][1]["edited"] is True
    assert saved["words"][2]["deleted"] is True


def test_edit_transcript_word_404_unknown_idx(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    assert c.post("/api/v1/transcripts/t1/words/99", json={"op": "delete"}).status_code == 404


def test_edit_transcript_word_400_bad_op(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    assert c.post("/api/v1/transcripts/t1/words/0", json={"op": "explode"}).status_code == 400


def test_render_pipeline_rejects_bad_stop_after(client, tmp_path):
    """The 'Make clips' path passes stop_after='reframe' (cut + reframe, no burn); any other
    value is a 400 so a typo can't silently run the full export."""
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())  # source id "abc" + words.json
    assert c.post("/api/v1/sources/abc/render", json={"start": 0.0, "end": 1.0, "stop_after": "explode"}).status_code == 400


def test_chunk_text_format_default_returns_full_body(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="hello world\n")
    r = c.get("/api/v1/transcripts/t1/chunk")  # default format=txt, default limit
    assert r.status_code == 200
    body = r.get_json()
    assert body["format"] == "txt"
    assert body["offset"] == 0
    assert body["total"] == len(b"hello world\n")
    assert body["returned"] == body["total"]
    assert body["has_more"] is False
    assert body["content"] == "hello world\n"


def test_chunk_text_paginates_by_bytes(client, tmp_path):
    """Stitching pages back together must reproduce the original body
    byte-for-byte. Pinning this contract lets MCP clients trust the
    has_more / offset+returned pagination loop."""
    app, c = client
    full = "abcdefghij" * 50  # 500 chars / 500 bytes (ascii)
    _seed_done_transcript(app, tmp_path, body_text=full)

    pages: list[str] = []
    offset = 0
    while True:
        r = c.get(f"/api/v1/transcripts/t1/chunk?format=txt&offset={offset}&limit=120")
        body = r.get_json()
        assert body["total"] == 500
        assert body["limit"] == 120
        pages.append(body["content"])
        offset += body["returned"]
        if not body["has_more"]:
            break
    assert "".join(pages) == full
    assert offset == 500


def test_chunk_text_preserves_crlf_bytes(client, tmp_path):
    """Architect P1 regression: text-mode `open(..., "r")` would
    silently translate CRLF→LF, making the `total` byte count
    disagree with the bytes /export.<fmt> serves. Reading binary
    keeps the wire bytes faithful to disk."""
    app, c = client
    body_text = "line1\r\nline2\r\nline3\r\n"  # 21 bytes on disk
    _seed_done_transcript(app, tmp_path, body_text=body_text)
    body = c.get("/api/v1/transcripts/t1/chunk").get_json()
    assert body["total"] == 21
    assert body["returned"] == 21
    # ``\r`` survives the round-trip: would be stripped by text-mode read.
    assert body["content"].count("\r\n") == 3


def test_chunk_text_clamps_limit_to_max(client, tmp_path):
    """A pathological ``?limit=999999`` must be capped server-side
    so the response can't exceed the documented ceiling."""
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="x" * 100)
    body = c.get("/api/v1/transcripts/t1/chunk?limit=999999").get_json()
    assert body["limit"] == 64000   # _CHUNK_TEXT_MAX_LIMIT
    assert body["returned"] == 100  # capped at total available


def test_chunk_text_limit_zero_means_default_not_empty(client, tmp_path):
    """Architect P1 regression: ``?limit=0`` must NOT mean "return
    zero bytes" — that produced ``returned=0, has_more=true`` which
    deadlocked any naive paginator. Collapse 0 → server default."""
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="hello")
    body = c.get("/api/v1/transcripts/t1/chunk?limit=0").get_json()
    assert body["limit"] == 4000   # _CHUNK_TEXT_DEFAULT_LIMIT
    assert body["returned"] == 5
    assert body["has_more"] is False


def test_chunk_text_offset_past_end_returns_empty(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="hello")
    body = c.get("/api/v1/transcripts/t1/chunk?offset=999").get_json()
    assert body["returned"] == 0
    assert body["content"] == ""
    assert body["has_more"] is False


def test_chunk_json_slices_segments_and_filters_words(client, tmp_path):
    """Json mode is the whole reason this endpoint exists: returning
    just the requested segments + only the words those segments
    reference, so an MCP caller pulling segment[7] doesn't pay for
    every word in the transcript."""
    app, c = client
    words = [{"idx": i, "w": f"w{i}", "original_w": f"w{i}",
              "start": float(i), "end": float(i) + 0.5,
              "edited": False, "deleted": False} for i in range(10)]
    segments = [
        {"start": 0.0, "end": 2.5, "text": "first",  "word_idxs": [0, 1, 2], "speaker": None},
        {"start": 2.5, "end": 5.5, "text": "second", "word_idxs": [3, 4, 5], "speaker": None},
        {"start": 5.5, "end": 9.5, "text": "third",  "word_idxs": [6, 7, 8, 9], "speaker": None},
    ]
    _seed_done_transcript(app, tmp_path, words_data={
        "schema_version": 2, "language": "en", "duration": 9.5,
        "words": words, "segments": segments,
    })

    body = c.get("/api/v1/transcripts/t1/chunk?format=json&offset=1&limit=1").get_json()
    assert body["format"] == "json"
    assert body["offset"] == 1 and body["limit"] == 1
    assert body["total"] == 3 and body["returned"] == 1
    assert body["has_more"] is True
    assert body["total_words"] == 10
    assert len(body["segments"]) == 1
    assert body["segments"][0]["text"] == "second"
    # Only words 3,4,5 (referenced by the returned segment) come along.
    assert sorted(w["idx"] for w in body["words"]) == [3, 4, 5]
    # v2 schema metadata is surfaced so callers know what they're getting.
    assert body["schema_version"] == 2
    assert body["language"] == "en"
    assert body["duration"] == 9.5


def test_chunk_json_clamps_segment_limit(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data={
        "schema_version": 2, "duration": 0.0, "words": [], "segments": [],
    })
    body = c.get("/api/v1/transcripts/t1/chunk?format=json&limit=999999").get_json()
    assert body["limit"] == 500  # _CHUNK_JSON_MAX_LIMIT


def test_chunk_rejects_invalid_format(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="x")
    r = c.get("/api/v1/transcripts/t1/chunk?format=pdf")
    assert r.status_code == 404
    assert r.get_json()["error"] == "invalid_format"


def test_chunk_rejects_invalid_offset(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, body_text="x")
    r = c.get("/api/v1/transcripts/t1/chunk?offset=abc")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_offset"


def test_chunk_404s_when_transcript_missing(client):
    _, c = client
    r = c.get("/api/v1/transcripts/nope/chunk")
    assert r.status_code == 404
    assert r.get_json()["error"] == "transcript_not_found_or_not_done"


# ---- OpenAPI --------------------------------------------------------

def test_openapi_documents_every_v1_route(client):
    app, c = client
    r = c.get("/api/v1/openapi.json")
    assert r.status_code == 200
    doc = r.get_json()
    documented = set(doc["paths"].keys())

    # Translate Flask rules (`/api/v1/jobs/<id>`) to OAS paths (`/jobs/{id}`).
    actual = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v1/"):
            continue
        if rule.rule == "/api/v1/openapi.json":
            actual.add("/openapi.json")
            continue
        path = rule.rule[len("/api/v1"):]
        # Flask <converter:name> -> {name}
        import re
        path = re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", path)
        actual.add(path)

    missing = actual - documented
    assert not missing, f"undocumented v1 routes: {sorted(missing)}"


# ---- SSE events -----------------------------------------------------

def test_events_stream_emits_initial_snapshot(client):
    app, c = client
    _seed_jobs(app, 2)
    r = c.get("/api/v1/events?max_events=1&interval=0.05")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    # The Flask test client buffers + closes the response; pull the body.
    raw = b"".join(r.response).decode("utf-8")
    assert "event: snapshot" in raw
    assert "\"jobs\"" in raw and "\"transcripts\"" in raw


def test_events_stream_terminates_at_max_events(client):
    _, c = client
    r = c.get("/api/v1/events?max_events=1&interval=0.05")
    chunks = list(r.response)  # iterator drains and closes
    assert chunks, "expected at least one frame"


# ---- architect-flagged regressions ---------------------------------

def test_list_jobs_default_returns_all_back_compat(client):
    """Pre-pagination clients call ``GET /jobs`` with no ?limit and
    expect the full list. A default cap of 100 would silently truncate
    large queues; this test pins the legacy contract."""
    app, c = client
    _seed_jobs(app, 250)
    body = c.get("/api/v1/jobs").get_json()
    assert body["total"] == 250
    assert body["returned"] == 250
    assert len(body["jobs"]) == 250
    # And the surfaced ``limit`` should reflect the actual page size,
    # not the internal _UNLIMITED sentinel.
    assert body["limit"] == 250


def test_idempotent_concurrent_requests_single_flight(client):
    """Two concurrent POSTs with the same Idempotency-Key must produce
    at most ONE enqueue. The second one either replays the first or
    gets ``409 in_flight`` — never a duplicate job."""
    import threading
    app, c = client
    enqueue_calls = []
    enqueue_gate = threading.Event()
    def slow_enqueue(url, *a, **kw):
        # Block long enough that a second request observes the
        # in-flight placeholder.
        enqueue_calls.append(url)
        enqueue_gate.wait(timeout=2.0)
        jm = app.extensions["trove.jobs"]
        jid = f"job-{len(enqueue_calls)}"
        jm._jobs[jid] = Job(id=jid, url=url, title="t", status=JobStatus.QUEUED)
        return jid
    app.extensions["trove.actions"]["enqueue_download"] = slow_enqueue

    headers = {"Idempotency-Key": "race-key"}
    body = {"url": "https://example.com/x", "title": "X"}
    results: list = []

    def post():
        r = c.post("/api/v1/jobs", json=body, headers=headers)
        results.append((r.status_code, r.get_json()))

    t1 = threading.Thread(target=post)
    t2 = threading.Thread(target=post)
    t1.start()
    # Give t1 a moment to claim the slot before t2 races in.
    import time as _t; _t.sleep(0.05)
    t2.start()
    enqueue_gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Exactly one underlying enqueue call.
    assert len(enqueue_calls) == 1, enqueue_calls
    # And one of the two responses is either a 201 fresh + 409 in_flight,
    # or a 201 + 200 replay (depending on timing). Either way, never two
    # 201s, and never two distinct job ids.
    statuses = sorted(s for s, _ in results)
    assert statuses in ([200, 201], [201, 409]), statuses
    ids = {body.get("id") for _, body in results if body.get("id")}
    assert len(ids) == 1


def test_idempotent_failed_enqueue_releases_slot(client, monkeypatch):
    """If the enqueue raises (RuntimeError → 503 busy), the placeholder
    must be released so the same key isn't permanently poisoned."""
    app, c = client
    def boom(*a, **kw):
        raise RuntimeError("queue full")
    app.extensions["trove.actions"]["enqueue_download"] = boom
    h = {"Idempotency-Key": "fail-key"}
    r = c.post("/api/v1/jobs", json={"url": "https://x", "title": "T"}, headers=h)
    assert r.status_code == 503
    # Recovery: a real enqueue with the same key now succeeds (i.e. the
    # placeholder didn't stick around).
    def ok(url, *a, **kw):
        jm = app.extensions["trove.jobs"]
        jm._jobs["recovered"] = Job(id="recovered", url=url, title="T",
                                     status=JobStatus.QUEUED)
        return "recovered"
    app.extensions["trove.actions"]["enqueue_download"] = ok
    r2 = c.post("/api/v1/jobs", json={"url": "https://x", "title": "T"}, headers=h)
    assert r2.status_code == 201
    assert r2.get_json()["id"] == "recovered"


def test_idempotent_stale_key_after_eviction_submits_fresh(client):
    """If the original job is gone (TTL'd or dismissed), the same key
    must enqueue a NEW job — not 409 forever."""
    app, c = client
    counter = {"n": 0}
    def fake_enqueue(url, *a, **kw):
        counter["n"] += 1
        jm = app.extensions["trove.jobs"]
        jid = f"job{counter['n']}"
        jm._jobs[jid] = Job(id=jid, url=url, title="t", status=JobStatus.QUEUED)
        return jid
    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    h = {"Idempotency-Key": "stale-key"}
    body = {"url": "https://x", "title": "T"}

    r1 = c.post("/api/v1/jobs", json=body, headers=h)
    assert r1.status_code == 201
    first_id = r1.get_json()["id"]
    # Simulate the job being TTL-swept out of the manager.
    del app.extensions["trove.jobs"]._jobs[first_id]

    r2 = c.post("/api/v1/jobs", json=body, headers=h)
    assert r2.status_code == 201, r2.get_json()
    assert r2.get_json()["id"] != first_id


def test_list_transcripts_default_returns_all_back_compat(client):
    """Mirror of test_list_jobs_default_returns_all_back_compat — old
    callers calling /transcripts with no ?limit must keep getting
    every entry."""
    app, c = client
    tm = app.extensions["trove.transcribe"]
    tm._jobs.clear()
    for i in range(150):
        tid = f"t{i:03d}"
        tm._jobs[tid] = transcribe_jobs.TranscribeJob(
            id=tid, parent_job_id="p", model_used="m",
            status=transcribe_jobs.TranscribeStatus.DONE,
        )
    body = c.get("/api/v1/transcripts").get_json()
    assert body["total"] == 150
    assert body["returned"] == 150
    assert len(body["transcripts"]) == 150
    assert body["limit"] == 150


# ---- settings (writable engine config; demo 07) ---------------------

def test_settings_defaults_and_patch_roundtrip(client):
    _, c = client
    r = c.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.get_json()
    # every writable key is present with its default
    for k in ("fast_default", "default_preset", "clip_workers", "max_workers", "mcp_transport"):
        assert k in body
    assert body["fast_default"] is True
    assert body["clip_workers"] == 2
    assert body["default_preset"] == "tiktok"

    r = c.patch("/api/v1/settings",
                json={"fast_default": False, "clip_workers": 4, "default_preset": "reels"})
    assert r.status_code == 200
    updated = r.get_json()
    assert updated["fast_default"] is False
    assert updated["clip_workers"] == 4
    assert updated["default_preset"] == "reels"
    # untouched keys keep their defaults
    assert updated["max_workers"] == 4
    assert updated["mcp_transport"] == "stdio"
    # persisted: a fresh GET reflects it
    assert c.get("/api/v1/settings").get_json()["clip_workers"] == 4


def test_settings_clamps_numeric_and_rejects_bad_enums(client):
    _, c = client
    # numeric concurrency is clamped (like caption overrides), not errored
    assert c.patch("/api/v1/settings", json={"clip_workers": 999}).get_json()["clip_workers"] == 16
    assert c.patch("/api/v1/settings", json={"clip_workers": 0}).get_json()["clip_workers"] == 1
    # invalid enums are a 400 (don't silently coerce)
    assert c.patch("/api/v1/settings", json={"default_preset": "myspace"}).status_code == 400
    assert c.patch("/api/v1/settings", json={"mcp_transport": "carrier-pigeon"}).status_code == 400
    # a non-bool for a bool field is a 400 (avoid bool("false") == True footguns)
    assert c.patch("/api/v1/settings", json={"fast_default": "yes"}).status_code == 400
    # the rejected writes left the store untouched
    assert c.get("/api/v1/settings").get_json()["default_preset"] == "tiktok"


def test_create_app_applies_persisted_concurrency_at_startup(tmp_path, monkeypatch):
    """The "applies on restart" contract: a UI-written concurrency in the settings store wins
    over the env default at create_app; the env still governs an unconfigured store."""
    import json
    import app as _app_module
    dl = tmp_path / "downloads"
    dl.mkdir(parents=True)
    (dl / "settings.json").write_text(json.dumps({"clip_workers": 5, "max_workers": 7}))
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", dl)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    application = _app_module.create_app()
    assert application.extensions["trove.clips"].max_workers == 5   # render pool
    assert application.extensions["trove.jobs"].max_workers == 7    # download pool


# ---- glass-box re-rank (POST /sources/<id>/rank) ------------------------
# Stateless re-rank: the client posts the candidates it already holds + the desired weights;
# the engine re-scores ON the attached features and returns them sorted (MCP/CLI/agent parity —
# the studio mirrors the same weighted-sum client-side for instant slider feedback).

def _rank_cands():
    flat = {"start": 0.0, "end": 18.0, "title": "flat", "mode": "funny", "signals": [],
            "features": {"text": {"is_question": False, "exclamation": False, "intensity": 0.0,
                                  "filler_ratio": 0.0, "word_rate": 1.0}}}
    hooky = {"start": 20.0, "end": 38.0, "title": "hooky", "mode": "funny", "signals": ["hook", "punchline"],
             "features": {"text": {"is_question": True, "exclamation": True, "intensity": 0.0,
                                   "filler_ratio": 0.0, "word_rate": 1.0}}}
    return [flat, hooky]


def test_rank_endpoint_rescores_and_sorts(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    r = c.post("/api/v1/sources/abc/rank", json={"candidates": _rank_cands()})
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 2
    assert body["candidates"][0]["title"] == "hooky"                    # ranked best-first
    assert set(body["candidates"][0]["factors"]) == {"hook", "self_contained", "arc", "energy", "length_fit"}
    assert set(body["weights"]) == {"hook", "self_contained", "arc", "energy", "length_fit"}


def test_rank_endpoint_honors_weight_overrides(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    cands = [
        {"start": 0.0, "end": 18.0, "title": "hooky", "mode": "funny", "signals": ["hook"],
         "features": {"text": {"is_question": True, "exclamation": True, "intensity": 0.0,
                               "filler_ratio": 0.0, "word_rate": 1.0}}},
        {"start": 20.0, "end": 38.0, "title": "loud", "mode": "funny", "signals": [],
         "features": {"text": {"is_question": False, "exclamation": False, "intensity": 0.0,
                               "filler_ratio": 0.0, "word_rate": 1.0},
                      "audio": {"mean_db": -20, "max_db": -1, "dynamic_db": 34}, "scene_density": 2.0}},
    ]
    r = c.post("/api/v1/sources/abc/rank", json={"candidates": cands, "weights": {"energy": 1}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["candidates"][0]["title"] == "loud"                    # energy-weighted → loud wins
    assert body["weights"]["energy"] == 1.0 and body["weights"]["hook"] == 0.0


def test_rank_endpoint_requires_candidates(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    assert c.post("/api/v1/sources/abc/rank", json={}).status_code == 400


def test_rank_endpoint_unknown_source_404(client):
    _, c = client
    assert c.post("/api/v1/sources/ghost/rank", json={"candidates": []}).status_code == 404


def test_rank_endpoint_rejects_non_dict_candidate(client, tmp_path):
    # A non-dict element would make rank() raise AttributeError → an unhandled 500. The endpoint
    # must reject it up front with a clean 400 instead.
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    r = c.post("/api/v1/sources/abc/rank", json={"candidates": [{"start": 0.0, "end": 18.0}, "oops"]})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_candidates"


def test_source_energy_endpoint_returns_envelope(client, tmp_path, monkeypatch):
    # The audio-energy waveform: a normalized 0..1 envelope for a ready source (ffmpeg stubbed).
    import clip.signals as sig
    monkeypatch.setattr(sig, "energy_envelope", lambda path, **k: [0.1, 0.5, 1.0])
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc", media on disk
    r = c.get("/api/v1/sources/abc/energy?buckets=16")
    assert r.status_code == 200
    body = r.get_json()
    assert body["bars"] == [0.1, 0.5, 1.0] and body["buckets"] == 16
    assert c.get("/api/v1/sources/ghost/energy").status_code == 404   # unknown source


def test_source_scenes_endpoint_windows_to_the_clip(client, tmp_path, monkeypatch):
    # The editor timeline's Scenes lane: scene-cut times within a clip window (ffmpeg stubbed).
    import clip.signals as sig
    monkeypatch.setattr(sig, "scene_cuts", lambda path, s, e, **k: [61.5, 64.25])
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    r = c.get("/api/v1/sources/abc/scenes?start=60&end=120")
    assert r.status_code == 200 and r.get_json()["cuts"] == [61.5, 64.25]
    # no window → empty (whole-source detection is too slow to run on demand)
    assert c.get("/api/v1/sources/abc/scenes").get_json()["cuts"] == []


def test_source_filmstrip_endpoint_returns_data_uri(client, tmp_path, monkeypatch):
    # The editor timeline's Video lane: a thumbnail filmstrip across a clip window (ffmpeg stubbed).
    import clip.signals as sig
    monkeypatch.setattr(sig, "filmstrip", lambda path, s, e, **k: "data:image/jpeg;base64,AAAA")
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    r = c.get("/api/v1/sources/abc/filmstrip?start=60&end=105&frames=12")
    assert r.status_code == 200
    body = r.get_json()
    assert body["strip"].startswith("data:image/jpeg") and body["frames"] == 12
    # no window → no strip (avoids a full-source extraction on demand)
    assert c.get("/api/v1/sources/abc/filmstrip").get_json()["strip"] is None


# ---- agent: NL → bounded ReAct tool-loop over the full /api/v1 surface ----

def test_agent_route_shapes_loop_result(client, monkeypatch):
    # The route drives clip.agent.run_agent and returns its reply + real tool trace + jobs.
    import clip.agent as clip_agent
    captured = {}

    def fake_run(message, *, client, transcript_lines=None, **kw):
        captured["message"] = message
        return {"action": "reply", "reply": "1 job in the queue, done.",
                "tools": [{"name": "list_clip_jobs", "arg": "", "ms": 5, "ok": True}],
                "jobs": [{"id": "j1", "kind": "produce", "status": "done"}]}

    monkeypatch.setattr(clip_agent, "run_agent", fake_run)
    _, c = client
    r = c.post("/api/v1/agent", json={"message": "what's in the queue?"})
    assert r.status_code == 200
    b = r.get_json()
    assert b["reply"] == "1 job in the queue, done." and b["action"] == "reply"
    assert b["tools"][0]["name"] == "list_clip_jobs"          # REAL tool trace (not fabricated from jobs)
    assert b["jobs"][0]["id"] == "j1"


def test_agent_route_clarify_carries_kind(client, monkeypatch):
    import clip.agent as clip_agent
    monkeypatch.setattr(clip_agent, "run_agent", lambda m, **kw: {
        "action": "clarify", "reply": "Which one?", "question": "Which source?",
        "options": ["a", "b"], "kind": "enum", "tools": [], "jobs": []})
    _, c = client
    b = c.post("/api/v1/agent", json={"message": "clip it"}).get_json()
    assert b["action"] == "clarify" and b["question"] == "Which source?"
    assert b["options"] == ["a", "b"] and b["kind"] == "enum"


def test_agent_route_missing_message_400(client):
    _, c = client
    assert c.post("/api/v1/agent", json={}).status_code == 400


def test_agent_route_llm_unavailable_503(client, monkeypatch):
    import clip.agent as clip_agent
    from clip import llm
    def boom(*a, **k):
        raise llm.OfflineError("offline")
    monkeypatch.setattr(clip_agent, "run_agent", boom)
    _, c = client
    assert c.post("/api/v1/agent", json={"message": "hi"}).status_code == 503


# ---- recipes (Phase 3): saved end-to-end pipelines that drive render.pipeline ----

def test_recipe_crud(client):
    _, c = client
    assert c.get("/api/v1/recipes").get_json() == {"recipes": []}
    r = c.post("/api/v1/recipes", json={"name": "Punchy", "content_mode": "funny", "count": 8,
                                        "aspect": "9:16", "reframe_mode": "pan", "caption_preset": "karaoke",
                                        "platform": "tiktok", "fast": True, "weights": {"energy": 4, "hook": 5}})
    assert r.status_code == 201
    rid = r.get_json()["id"]
    assert c.get("/api/v1/recipes").get_json()["recipes"][0]["id"] == rid
    assert c.get(f"/api/v1/recipes/{rid}").get_json()["content_mode"] == "funny"
    u = c.patch(f"/api/v1/recipes/{rid}", json={"count": 5})
    assert u.status_code == 200 and u.get_json()["count"] == 5 and u.get_json()["content_mode"] == "funny"
    assert c.delete(f"/api/v1/recipes/{rid}").status_code == 204
    assert c.get(f"/api/v1/recipes/{rid}").status_code == 404


def test_recipe_validation(client):
    _, c = client
    assert c.post("/api/v1/recipes", json={}).status_code == 400                         # no name
    assert c.post("/api/v1/recipes", json={"name": "x", "aspect": "weird"}).status_code == 400
    assert c.post("/api/v1/recipes", json={"name": "x", "content_mode": "bogus"}).status_code == 400
    assert c.post("/api/v1/recipes", json={"name": "x", "platform": "myspace"}).status_code == 400
    assert c.post("/api/v1/recipes", json={"name": "x", "weights": {"hook": "lots"}}).status_code == 400
    assert c.patch("/api/v1/recipes/nope", json={"count": 2}).status_code == 404
    assert c.delete("/api/v1/recipes/nope").status_code == 404


def test_pipeline_uses_recipe_defaults(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    rid = c.post("/api/v1/recipes", json={"name": "R", "aspect": "1:1", "reframe_mode": "center",
                                          "caption_preset": "minimal", "platform": "reels"}).get_json()["id"]
    # a pipeline call giving only the range + recipe → the recipe supplies aspect/mode/style/preset
    r = c.post("/api/v1/sources/abc/render", json={"start": 0.0, "end": 1.5, "recipe_id": rid})
    assert r.status_code == 201
    p = r.get_json()["params"]
    assert (p["aspect"], p["mode"], p["style"], p["preset"]) == ("1:1", "center", "minimal", "reels")


def test_pipeline_body_overrides_recipe(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    rid = c.post("/api/v1/recipes", json={"name": "R", "aspect": "1:1", "platform": "reels"}).get_json()["id"]
    r = c.post("/api/v1/sources/abc/render", json={"start": 0.0, "end": 1.5, "recipe_id": rid, "aspect": "9:16"})
    assert r.get_json()["params"]["aspect"] == "9:16"   # explicit body wins over the recipe default


def test_pipeline_unknown_recipe_404(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    r = c.post("/api/v1/sources/abc/render", json={"start": 0.0, "end": 1.5, "recipe_id": "ghost"})
    assert r.status_code == 404


# ---- produce: apply a recipe end-to-end (find→rank→top-N→pipeline) → review queue (Phase 3) ----
# find_moments is stubbed so the async produce job never reaches the codex bridge in a unit test;
# the fan-out logic itself is covered synchronously in test_clip_runner.

def test_produce_endpoint_submits_a_produce_job(client, tmp_path, monkeypatch):
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    rid = c.post("/api/v1/recipes", json={"name": "R", "content_mode": "funny", "count": 3}).get_json()["id"]
    r = c.post("/api/v1/sources/abc/produce", json={"recipe_id": rid})
    assert r.status_code == 201 and r.get_json()["kind"] == "produce"


def test_produce_endpoint_unknown_recipe_404(client, tmp_path):
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())
    assert c.post("/api/v1/sources/abc/produce", json={"recipe_id": "ghost"}).status_code == 404


def test_produce_endpoint_no_transcript_409(client, tmp_path, monkeypatch):
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    app, c = client
    dd = app.extensions["trove.download_dir"]
    (dd / "novx.mp4").write_bytes(b"x")
    app.extensions["trove.jobs"]._jobs["novx"] = Job(id="novx", url="u", title="t",
                                                     status=JobStatus.DONE, file_path=str(dd / "novx.mp4"))
    rid = c.post("/api/v1/recipes", json={"name": "R"}).get_json()["id"]
    assert c.post("/api/v1/sources/novx/produce", json={"recipe_id": rid}).status_code == 409


def test_produce_inline_recipe_rejects_bad_enum(client, tmp_path):
    # An inline recipe (no recipe_id) must be validated up front like /render and /recipes — a bad
    # enum returns a clean 400, not a 201 that fails asynchronously in the render.
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    r = c.post("/api/v1/sources/abc/produce", json={"aspect": "weird"})
    assert r.status_code == 400 and r.get_json()["error"] == "bad_recipe"


def test_produce_inline_recipe_whitelists_keys_into_recipe(client, tmp_path, monkeypatch):
    # The inline-recipe branch builds the recipe from a whitelist of body keys — assert the
    # whitelisted (valid) keys reach produce_target and a junk key is dropped.
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    app, c = client
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())   # source "abc"
    captured = {}
    real = app.extensions["trove.clip_runner"].produce_target

    def _capture(*, source_id, recipe):
        captured["recipe"] = recipe
        return real(source_id=source_id, recipe=recipe)

    monkeypatch.setattr(app.extensions["trove.clip_runner"], "produce_target", _capture)
    r = c.post("/api/v1/sources/abc/produce", json={
        "content_mode": "funny", "count": 3, "aspect": "9:16", "reframe_mode": "pan",
        "caption_preset": "karaoke", "platform": "tiktok", "fast": True,
        "weights": {"energy": 2}, "brand_kit_id": "bk1", "junk": "nope"})
    assert r.status_code == 201
    rec = captured["recipe"]
    assert rec == {"content_mode": "funny", "count": 3, "aspect": "9:16", "reframe_mode": "pan",
                   "caption_preset": "karaoke", "platform": "tiktok", "fast": True,
                   "weights": {"energy": 2}, "brand_kit_id": "bk1"}
    assert "junk" not in rec


# ---- watches (Phase 3): folder / channel / playlist automations ----

def test_watch_crud(client):
    _, c = client
    assert c.get("/api/v1/watches").get_json() == {"watches": []}
    w = c.post("/api/v1/watches", json={"name": "Chan", "kind": "channel",
                                        "target": "https://youtube.com/@x", "recipe_id": "r1"})
    assert w.status_code == 201
    wid = w.get_json()["id"]
    assert w.get_json()["enabled"] is True and w.get_json()["seen"] == []
    assert c.get("/api/v1/watches").get_json()["watches"][0]["id"] == wid
    assert c.patch(f"/api/v1/watches/{wid}", json={"enabled": False}).get_json()["enabled"] is False
    assert c.delete(f"/api/v1/watches/{wid}").status_code == 204
    assert c.get(f"/api/v1/watches/{wid}").status_code == 404


def test_watch_validation(client):
    _, c = client
    assert c.post("/api/v1/watches", json={}).status_code == 400                            # no name
    assert c.post("/api/v1/watches", json={"name": "x", "kind": "bogus", "target": "y"}).status_code == 400
    assert c.post("/api/v1/watches", json={"name": "x", "kind": "folder"}).status_code == 400  # no target
    assert c.patch("/api/v1/watches/nope", json={"enabled": False}).status_code == 404
    assert c.delete("/api/v1/watches/nope").status_code == 404


def test_watch_scan_ingests_new_folder_videos(client, tmp_path, monkeypatch):
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    app, c = client
    indir = tmp_path / "incoming"
    indir.mkdir()
    p = indir / "talk.mp4"
    p.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(p, (old, old))
    wid = c.post("/api/v1/watches", json={"name": "F", "kind": "folder",
                                          "target": str(indir), "recipe_id": "r1"}).get_json()["id"]
    r = c.post(f"/api/v1/watches/{wid}/scan")
    assert r.status_code == 200 and len(r.get_json()["ingested"]) == 1     # the new file ingested
    assert "talk.mp4" in c.get(f"/api/v1/watches/{wid}").get_json()["seen"]
    assert c.post(f"/api/v1/watches/{wid}/scan").get_json()["ingested"] == []   # nothing new the 2nd scan


def test_watch_scan_unknown_404(client):
    _, c = client
    assert c.post("/api/v1/watches/ghost/scan").status_code == 404


def test_create_watch_rejects_unsafe_channel_target(client):
    _, c = client
    r = c.post("/api/v1/watches", json={
        "name": "evil", "kind": "channel", "target": "--config-location=/tmp/evil"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsafe_target"

    r = c.post("/api/v1/watches", json={
        "name": "internal", "kind": "playlist", "target": "http://169.254.169.254/latest"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsafe_target"


def test_update_watch_rejects_unsafe_retarget(client):
    # IP-literal public address: is_safe_url checks it without a DNS lookup (offline-safe test)
    _, c = client
    r = c.post("/api/v1/watches", json={
        "name": "w", "kind": "playlist", "target": "https://93.184.216.34/list"})
    assert r.status_code == 201
    wid = r.get_json()["id"]
    r = c.patch(f"/api/v1/watches/{wid}", json={"target": "-o/tmp/pwn"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsafe_target"


def test_two_watches_on_the_same_folder_ingest_a_file_once(client, tmp_path, monkeypatch):
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    _, c = client
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "video.mp4"
    f.write_bytes(b"\x00" * 4096)
    old = time.time() - 3600
    os.utime(f, (old, old))   # pre-settled so the upcoming folder-debounce task can't skip it

    w1 = c.post("/api/v1/watches", json={"name": "a", "kind": "folder", "target": str(inbox)}).get_json()
    w2 = c.post("/api/v1/watches", json={"name": "b", "kind": "folder", "target": str(inbox)}).get_json()
    c.post(f"/api/v1/watches/{w1['id']}/scan")
    c.post(f"/api/v1/watches/{w2['id']}/scan")

    jobs = c.get("/api/v1/jobs?limit=100").get_json()["jobs"]
    same_file = [j for j in jobs if j["url"].endswith("video.mp4")]
    assert len(same_file) == 1, f"file ingested {len(same_file)}x across two watches"
