"""Tests for the /api/v1 JSON blueprint (CLI + MCP backbone).

These cover the stable contract: shapes, status codes, idempotence
guards, and the auth boundary. Heavy operations (real downloads,
real whisper) are stubbed via the same monkeypatch points the
existing endpoint tests use.
"""
from __future__ import annotations
import os
import copy
import inspect
import json
import threading
import time
from pathlib import Path
import pytest
from app import create_app
from job_capacity import QueueFullError
from jobs import Job, JobStatus
import transcribe_jobs
import safety
import watcher


PHASE0_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/v1/phase0-contract.json")
    .read_text(encoding="utf-8")
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("TROVE_TRUST_PROXY_HOPS", raising=False)
    monkeypatch.delenv("TROVE_RATE_LIMIT_MAX_KEYS", raising=False)
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


def test_capabilities_exposes_live_pending_capacity_by_workload(client):
    app, c = client
    app.extensions["trove.jobs"].pending_capacity = 11
    app.extensions["trove.transcribe"].pending_capacity = 12
    app.extensions["trove.clips"].pending_capacity = 13

    limits = c.get("/api/v1/capabilities").get_json()["limits"]

    assert limits["pending_capacity"] == {
        "download": 11,
        "transcription": 12,
        "media": 13,
    }


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
    assert {"filename", "downloaded_bytes", "auto_transcribe", "dismissed", "dismissed_at"}.issubset(body.keys())
    assert body["dismissed"] is False
    assert body["dismissed_at"] is None


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
        "url": "https://93.184.216.34/video",
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
        "url": "https://93.184.216.34/v", "title": "t",
        "subtitles": True, "chapters": True, "embed": True,
    })
    assert r.status_code == 201
    assert captured == {"subtitles": True, "chapters": True, "embed": True}


def test_submit_job_busy_returns_503(client, monkeypatch):
    app, c = client

    def fake_enqueue(*a, **kw):
        raise RuntimeError("pool full")

    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    # IP-literal: is_safe_url checks it without DNS (offline-safe); what this test
    # proves is that a busy enqueue raises and the route returns 503, not URL validity.
    r = c.post("/api/v1/jobs", json={"url": "https://93.184.216.34/video", "title": "x"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "busy"


def _assert_queue_full_response(response):
    assert response.status_code == 429
    assert response.get_json() == PHASE0_CONTRACT["queue_full"]
    assert response.headers["Retry-After"] == "1"


def test_submit_job_queue_full_returns_exact_429_and_releases_idempotency_claim(client):
    app, c = client

    def saturated(*_args, **_kwargs):
        raise QueueFullError("download queue full")

    app.extensions["trove.actions"]["enqueue_download"] = saturated
    request = {
        "json": {"url": "https://93.184.216.34/video", "title": "x"},
        "headers": {"Idempotency-Key": "capacity-retry"},
    }

    _assert_queue_full_response(c.post("/api/v1/jobs", **request))
    # A rejected claim must be released so retry does not become 409 in_flight.
    _assert_queue_full_response(c.post("/api/v1/jobs", **request))


def test_submit_job_does_not_probe_on_the_request_thread(client, monkeypatch):
    """A single-URL submit with NO title must enqueue immediately without
    running yt-dlp's ``run_info`` probe on the request thread.

    A slow/auth-gated host (e.g. x.com) makes that probe take many seconds,
    so a synchronous probe leaves the studio's Download POST pending and the
    button looks dead. Like the bulk path, the worker resolves the real title
    afterwards (``resolve_title=True``); the placeholder title is the URL.
    """
    import runner
    calls = {"n": 0}

    class _FakeInfo:
        error_category = None
        title = "RESOLVED BY PROBE"
        thumbnail = "t.jpg"

    def spy(_url):
        calls["n"] += 1
        return _FakeInfo()

    # _submit_one does `from runner import run_info` at call time, so patching
    # the module attribute intercepts any request-thread probe.
    monkeypatch.setattr(runner, "run_info", spy)

    app, c = client
    captured = {}

    def fake_enqueue(url, fmt, fmt_id, title, thumbnail="", *, auto_transcribe=False,
                     subtitles=False, chapters=False, embed=False, resolve_title=False):
        captured.update(title=title, resolve_title=resolve_title)
        jm = app.extensions["trove.jobs"]
        jm._jobs["noprobe1"] = Job(id="noprobe1", url=url, title=title, status=JobStatus.QUEUED)
        return "noprobe1"

    app.extensions["trove.actions"]["enqueue_download"] = fake_enqueue
    r = c.post("/api/v1/jobs", json={"url": "https://93.184.216.34/v"})  # NO title

    assert r.status_code == 201
    assert calls["n"] == 0, "run_info must not be called on the request thread"
    assert captured["title"] == "https://93.184.216.34/v"  # placeholder; worker resolves
    assert captured["resolve_title"] is True


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
    history = c.get("/api/v1/jobs/jid")
    assert history.status_code == 200
    assert history.get_json()["dismissed"] is True
    assert history.get_json()["dismissed_at"] is not None
    assert any(j["id"] == "jid" for j in c.get("/api/v1/jobs").get_json()["jobs"])


def test_cancel_terminal_download_is_noop_and_preserves_file(client, tmp_path):
    app, c = client
    media = tmp_path / "published.mp4"
    media.write_bytes(b"published")
    app.extensions["trove.jobs"]._jobs["done"] = Job(
        id="done", url="u", title="t", status=JobStatus.DONE,
        file_path=str(media), filename=media.name,
    )
    assert c.post("/api/v1/jobs/done/cancel").status_code == 404
    assert c.get("/api/v1/jobs/done").get_json()["status"] == "done"
    assert media.read_bytes() == b"published"


def test_pause_404_for_unknown(client):
    _, c = client
    r = c.post("/api/v1/jobs/missing/pause")
    assert r.status_code == 404


def test_attempt_unwinding_resume_returns_structured_409(client):
    from jobs import AttemptUnwindingError

    app, c = client

    def unwinding(_jid):
        raise AttemptUnwindingError("old attempt is still unwinding")

    app.extensions["trove.actions"]["resume_job"] = unwinding
    app.extensions["trove.jobs"]._jobs["paused"] = Job(
        id="paused", url="u", title="t", status=JobStatus.PAUSED,
    )

    response = c.post("/api/v1/jobs/paused/resume")

    assert response.status_code == 409
    assert response.get_json() == {"error": "attempt_unwinding"}


def test_resume_queue_full_returns_exact_429_and_preserves_paused_job(client):
    app, c = client
    paused = Job(id="paused-full", url="u", title="t", status=JobStatus.PAUSED)
    paused._attempt = 7
    app.extensions["trove.jobs"]._jobs[paused.id] = paused
    before = copy.deepcopy(vars(paused))
    before.pop("last_accessed", None)

    def saturated(_job_id):
        raise QueueFullError("download queue full")

    app.extensions["trove.actions"]["resume_job"] = saturated
    response = c.post(f"/api/v1/jobs/{paused.id}/resume")

    _assert_queue_full_response(response)
    after = copy.deepcopy(vars(paused))
    after.pop("last_accessed", None)
    assert after == before


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/jobs", {"url": "https://93.184.216.34/video"}),
        ("/api/v1/jobs/bulk", {"urls": ["https://93.184.216.34/a", "https://93.184.216.34/b"]}),
    ],
)
def test_offline_download_submission_is_exact_409_before_dns_or_job_creation(
    client, monkeypatch, path, payload,
):
    app, c = client
    policy = app.extensions["trove.network_policy"]
    manager = app.extensions["trove.jobs"]
    before_jobs = manager.snapshot_jobs()
    dns_calls = []
    submit_calls = []
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    monkeypatch.setattr(manager._executor, "submit", lambda *a, **kw: submit_calls.append((a, kw)))
    policy.enable_offline()

    response = c.post(path, json=payload)

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    assert dns_calls == []
    assert manager.snapshot_jobs() == before_jobs
    assert submit_calls == []


def test_offline_direct_enqueue_rechecks_admission_before_dns_or_submit(client, monkeypatch):
    app, _ = client
    policy = app.extensions["trove.network_policy"]
    manager = app.extensions["trove.jobs"]
    dns_calls = []
    submit_calls = []
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    monkeypatch.setattr(manager, "submit", lambda *a, **kw: submit_calls.append((a, kw)))
    policy.enable_offline()

    from network_policy import NetworkPolicyError
    with pytest.raises(NetworkPolicyError, match="Offline mode"):
        app.extensions["trove.actions"]["enqueue_download"](
            "https://93.184.216.34/video", "video", None, "title",
        )

    assert dns_calls == []
    assert submit_calls == []


def test_offline_remote_watch_target_validation_denies_before_dns(client, monkeypatch):
    app, c = client
    dns_calls = []
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    app.extensions["trove.network_policy"].enable_offline()

    response = c.post("/api/v1/watches", json={
        "name": "remote",
        "kind": "playlist",
        "target": "https://93.184.216.34/playlist",
    })

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    assert app.extensions["trove.watches"].list() == []
    assert dns_calls == []


def test_offline_remote_watch_update_is_denied_before_store_mutation(client, monkeypatch):
    app, c = client
    created = c.post("/api/v1/watches", json={
        "name": "remote",
        "kind": "playlist",
        "target": "https://93.184.216.34/playlist",
    }).get_json()
    watch_id = created["id"]
    store_path = Path(app.extensions["trove.download_dir"]) / "watches.json"
    before_record = app.extensions["trove.watches"].get(watch_id)
    before_bytes = store_path.read_bytes()
    dns_calls = []
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    app.extensions["trove.network_policy"].enable_offline()

    response = c.patch(f"/api/v1/watches/{watch_id}", json={"name": "changed"})

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    assert app.extensions["trove.watches"].get(watch_id) == before_record
    assert store_path.read_bytes() == before_bytes
    assert dns_calls == []


def test_offline_folder_watch_cannot_be_repointed_to_remote(client, tmp_path, monkeypatch):
    app, c = client
    folder = tmp_path / "local-watch"
    folder.mkdir()
    created = c.post("/api/v1/watches", json={
        "name": "local", "kind": "folder", "target": str(folder),
    }).get_json()
    watch_id = created["id"]
    store_path = Path(app.extensions["trove.download_dir"]) / "watches.json"
    before_record = app.extensions["trove.watches"].get(watch_id)
    before_bytes = store_path.read_bytes()
    dns_calls = []
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    app.extensions["trove.network_policy"].enable_offline()

    response = c.patch(f"/api/v1/watches/{watch_id}", json={
        "kind": "playlist", "target": "https://example.com/list",
    })

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    assert app.extensions["trove.watches"].get(watch_id) == before_record
    assert store_path.read_bytes() == before_bytes
    assert dns_calls == []


def test_offline_remote_watch_scan_rejects_before_cache_invalidation_or_state(client, monkeypatch):
    app, c = client
    target = "https://93.184.216.34/playlist"
    created = c.post("/api/v1/watches", json={
        "name": "remote", "kind": "playlist", "target": target,
    }).get_json()
    watch_id = created["id"]
    store_path = Path(app.extensions["trove.download_dir"]) / "watches.json"
    before_record = app.extensions["trove.watches"].get(watch_id)
    before_bytes = store_path.read_bytes()
    watcher.clear_listing_cache()
    cache_key = (target, 30)
    watcher._listing_cache[cache_key] = {
        "items": ["https://youtu.be/cached"], "expires": float("inf"), "fails": 0,
    }
    before_cache = {key: dict(value) for key, value in watcher._listing_cache.items()}
    listing_calls = []
    monkeypatch.setattr(
        watcher, "list_playlist_items", lambda *a, **kw: listing_calls.append((a, kw)) or [],
    )
    app.extensions["trove.network_policy"].enable_offline()

    try:
        response = c.post(f"/api/v1/watches/{watch_id}/scan")

        assert response.status_code == 409
        assert response.get_json() == {"error": "offline_network_disabled"}
        assert listing_calls == []
        assert watcher._listing_cache == before_cache
        assert app.extensions["trove.watches"].get(watch_id) == before_record
        assert store_path.read_bytes() == before_bytes
    finally:
        watcher.clear_listing_cache()


def test_resume_offline_leaves_paused_job_and_persisted_snapshot_unchanged(
    client, monkeypatch,
):
    app, c = client
    manager = app.extensions["trove.jobs"]
    job = Job(
        id="paused-offline", url="https://93.184.216.34/video", title="paused",
        status=JobStatus.PAUSED, format_choice="video", out_template="existing.%(ext)s",
        _was_paused=True, _attempt=3,
    )
    manager._jobs[job.id] = job
    manager._persist()
    before_job = copy.deepcopy(job)
    store_path = Path(app.extensions["trove.download_dir"]) / "jobs.json"
    before_bytes = store_path.read_bytes()
    executor_calls = []
    dns_calls = []
    monkeypatch.setattr(manager._executor, "submit", lambda *a, **kw: executor_calls.append((a, kw)))
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    app.extensions["trove.network_policy"].enable_offline()

    response = c.post(f"/api/v1/jobs/{job.id}/resume")

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    after_job = copy.deepcopy(manager.get(job.id))
    # Reading a job intentionally refreshes its non-persisted TTL access clock;
    # resume state, attempts, errors, and artifacts must otherwise be identical.
    after_job.last_accessed = before_job.last_accessed
    assert after_job == before_job
    assert store_path.read_bytes() == before_bytes
    assert executor_calls == []
    assert dns_calls == []


def test_queued_download_rechecks_offline_before_deferred_info_or_download(
    tmp_path, monkeypatch,
):
    import app as app_module
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", download_dir)
    monkeypatch.setattr(app_module, "MAX_WORKERS", 1)
    application = app_module.create_app()
    client = application.test_client()
    manager = application.extensions["trove.jobs"]
    blocker_entered = threading.Event()
    release_blocker = threading.Event()
    dns_calls = []
    subprocess_calls = []

    def blocker(_job):
        blocker_entered.set()
        assert release_blocker.wait(2)

    manager.submit(target=blocker, title="blocker", url="file://blocker")
    assert blocker_entered.wait(1)
    monkeypatch.setattr(
        safety.socket, "getaddrinfo",
        lambda *_args: [(safety.socket.AF_INET, safety.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    response = client.post(
        "/api/v1/jobs", json={"url": "https://example.com/deferred-title"},
    )
    assert response.status_code == 201
    target_id = response.get_json()["id"]
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *args: dns_calls.append(args))
    monkeypatch.setattr(app_module.run_info.__globals__["subprocess"], "run", lambda *a, **kw: subprocess_calls.append((a, kw)))
    monkeypatch.setattr(app_module.run_info.__globals__["subprocess"], "Popen", lambda *a, **kw: subprocess_calls.append((a, kw)))
    application.extensions["trove.network_policy"].enable_offline()
    release_blocker.set()

    deadline = time.time() + 3
    while time.time() < deadline and manager.get(target_id).status not in {JobStatus.ERROR, JobStatus.DONE}:
        time.sleep(0.01)
    target = manager.get(target_id)
    assert target.status is JobStatus.ERROR
    assert target.error_category == "offline_network_disabled"
    assert dns_calls == []
    assert subprocess_calls == []


def test_offline_keeps_settings_reads_job_reads_and_local_import_available(
    client, tmp_path,
):
    app, c = client
    policy = app.extensions["trove.network_policy"]
    policy.enable_offline()
    source = tmp_path / "local.mp4"
    source.write_bytes(b"local media")

    job_id = app.extensions["trove.actions"]["import_local_file"](
        str(source), auto_transcribe=False,
    )
    assert _wait_download_worker_inactive(app.extensions["trove.jobs"], job_id)

    assert c.get("/api/v1/settings").status_code == 200
    assert c.get("/api/v1/jobs").status_code == 200
    assert app.extensions["trove.jobs"].get(job_id).status is JobStatus.DONE
    assert policy.active_leases == 0


def _wait_download_worker_inactive(manager, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and not job._worker_active:
            return True
        time.sleep(0.01)
    return False


def test_attempt_staging_cancelled_download_never_publishes_or_auto_transcribes(
    client, monkeypatch,
):
    from runner import DownloadResult
    import app as app_module

    app, c = client
    entered = threading.Event()
    release = threading.Event()
    observed = {}
    transcribe_calls = []

    def fake_download(**kwargs):
        observed["template"] = kwargs["out_template"]
        staged = Path(kwargs["out_template"].replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"candidate")
        entered.set()
        release.wait(5)
        return DownloadResult(file_path=str(staged))

    monkeypatch.setattr(app_module, "run_download", fake_download)
    monkeypatch.setattr("models_store.get_active_path", lambda: Path("model.bin"))
    monkeypatch.setattr(
        app.extensions["trove.transcribe"], "submit",
        lambda **kwargs: transcribe_calls.append(kwargs) or "t1",
    )

    response = c.post("/api/v1/jobs", json={
        "url": "https://93.184.216.34/video", "title": "video", "auto_transcribe": True,
    })
    jid = response.get_json()["id"]
    assert entered.wait(2)
    assert "/.attempts/download/" in observed["template"]
    assert c.post(f"/api/v1/jobs/{jid}/cancel").status_code == 200
    release.set()
    assert _wait_download_worker_inactive(app.extensions["trove.jobs"], jid)

    job = app.extensions["trove.jobs"].get(jid)
    assert job.status is JobStatus.CANCELLED
    assert job.file_path is None and job.filename is None
    assert not (app.extensions["trove.download_dir"] / f"{jid}.mp4").exists()
    assert transcribe_calls == []


def test_attempt_staging_successful_download_promotes_before_auto_transcribe(
    client, monkeypatch,
):
    from runner import DownloadResult
    import app as app_module

    app, c = client
    observed = {}
    auto_state = []

    def fake_download(**kwargs):
        observed["template"] = kwargs["out_template"]
        staged = Path(kwargs["out_template"].replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"download")
        return DownloadResult(file_path=str(staged))

    monkeypatch.setattr(app_module, "run_download", fake_download)
    monkeypatch.setattr("models_store.get_active_path", lambda: Path("model.bin"))

    def fake_transcribe_submit(**kwargs):
        parent = app.extensions["trove.jobs"].get(kwargs["parent_job_id"])
        auto_state.append((parent.status, parent.file_path, Path(parent.file_path).exists()))
        return "t1"

    monkeypatch.setattr(app.extensions["trove.transcribe"], "submit", fake_transcribe_submit)

    response = c.post("/api/v1/jobs", json={
        "url": "https://93.184.216.34/video", "title": "video", "auto_transcribe": True,
    })
    jid = response.get_json()["id"]
    deadline = time.time() + 5
    while app.extensions["trove.jobs"].get(jid).status is not JobStatus.DONE and time.time() < deadline:
        time.sleep(0.01)

    final = app.extensions["trove.download_dir"] / f"{jid}.mp4"
    assert "/.attempts/download/" in observed["template"]
    assert final.read_bytes() == b"download"
    assert app.extensions["trove.jobs"].get(jid).file_path == str(final)
    assert auto_state == [(JobStatus.DONE, str(final), True)]


def test_auto_transcribe_queue_full_does_not_poison_completed_download(
    client, monkeypatch,
):
    from runner import DownloadResult
    import app as app_module

    app, c = client

    def fake_download(**kwargs):
        staged = Path(kwargs["out_template"].replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"download")
        return DownloadResult(file_path=str(staged))

    def saturated(**_kwargs):
        raise QueueFullError("transcription queue full")

    monkeypatch.setattr(app_module, "run_download", fake_download)
    monkeypatch.setattr("models_store.get_active_path", lambda: Path("model.bin"))
    monkeypatch.setattr(app.extensions["trove.transcribe"], "submit", saturated)

    response = c.post("/api/v1/jobs", json={
        "url": "https://93.184.216.34/video",
        "title": "video",
        "auto_transcribe": True,
    })
    job_id = response.get_json()["id"]
    assert _wait_download_worker_inactive(app.extensions["trove.jobs"], job_id)

    job = app.extensions["trove.jobs"].get(job_id)
    assert job.status is JobStatus.DONE
    assert Path(job.file_path).read_bytes() == b"download"
    assert app.extensions["trove.transcribe"].snapshot_jobs() == []


def test_auto_transcribe_entitlement_survives_dismiss_after_done_persist(
    client, monkeypatch,
):
    from runner import DownloadResult
    import app as app_module

    app, c = client
    manager = app.extensions["trove.jobs"]
    done_persisted = threading.Event()
    release_persist = threading.Event()
    worker_ident = []
    transcribe_calls = []
    blocked = False
    real_persist = manager._persist

    def persist_with_done_barrier(*args, **kwargs):
        nonlocal blocked
        result = real_persist(*args, **kwargs)
        if worker_ident and threading.get_ident() == worker_ident[0]:
            with manager._lock:
                is_done = any(job.status is JobStatus.DONE for job in manager._jobs.values())
            if is_done and not blocked:
                blocked = True
                done_persisted.set()
                assert release_persist.wait(5)
        return result

    manager._persist = persist_with_done_barrier

    def fake_download(**kwargs):
        worker_ident.append(threading.get_ident())
        staged = Path(kwargs["out_template"].replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"download")
        return DownloadResult(file_path=str(staged))

    monkeypatch.setattr(app_module, "run_download", fake_download)
    monkeypatch.setattr("models_store.get_active_path", lambda: Path("model.bin"))
    monkeypatch.setattr(
        app.extensions["trove.transcribe"], "submit",
        lambda **kwargs: transcribe_calls.append(kwargs) or "t1",
    )

    response = c.post("/api/v1/jobs", json={
        "url": "https://93.184.216.34/video", "title": "video", "auto_transcribe": True,
    })
    jid = response.get_json()["id"]
    assert done_persisted.wait(2)
    assert c.post(f"/api/v1/jobs/{jid}/dismiss").status_code == 204
    release_persist.set()
    assert _wait_download_worker_inactive(manager, jid)
    assert [call["parent_job_id"] for call in transcribe_calls] == [jid]


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


def test_start_transcribe_queue_full_returns_exact_429_without_hidden_job(
    client, monkeypatch, tmp_path,
):
    app, c = client
    media = tmp_path / "parent.mp4"
    media.write_bytes(b"media")
    app.extensions["trove.jobs"]._jobs["parent-full"] = Job(
        id="parent-full",
        url="u",
        title="t",
        status=JobStatus.DONE,
        file_path=str(media),
    )
    monkeypatch.setattr("models_store.get_active_path", lambda: Path("model.bin"))

    def saturated(_parent_job_id):
        raise QueueFullError("transcription queue full")

    app.extensions["trove.actions"]["start_transcribe"] = saturated
    response = c.post("/api/v1/jobs/parent-full/transcribe")

    _assert_queue_full_response(response)
    assert app.extensions["trove.transcribe"].snapshot_jobs() == []


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


def _wait_transcribe_worker_inactive(manager, tid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(tid)
        if job is not None and not job._worker_active:
            return True
        time.sleep(0.01)
    return False


def _seed_transcribe_parent(app, tmp_path, parent_id="p"):
    media = app.extensions["trove.download_dir"] / f"{parent_id}.mp4"
    media.write_bytes(b"media")
    app.extensions["trove.jobs"]._jobs[parent_id] = Job(
        id=parent_id, url="u", title="t", status=JobStatus.DONE, file_path=str(media),
    )
    return media


def test_cancel_diarization_preserves_published_transcript_and_skips_fts(
    client, monkeypatch, tmp_path,
):
    import app as app_module
    import diarizer
    from transcriber import TranscriptResult

    app, c = client
    media = _seed_transcribe_parent(app, tmp_path)
    base = media.with_suffix("")
    published = [Path(str(base) + suffix) for suffix in (".words.json", ".txt", ".srt", ".vtt")]
    for index, path in enumerate(published):
        path.write_bytes(f"old-{index}".encode())
    before = {path: path.read_bytes() for path in published}
    entered = threading.Event()
    release = threading.Event()
    indexed = []

    def fake_extract(_media, wav_path, **_kwargs):
        Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
        Path(wav_path).write_bytes(b"wav")

    result = TranscriptResult(
        language="en", duration=2.0,
        words=[{"w": "hello", "start": 0.0, "end": 1.0}],
        segments=[{"start": 0.0, "end": 1.0, "text": "hello",
                   "words": [{"w": "hello", "start": 0.0, "end": 1.0}]}],
    )
    monkeypatch.setattr(app_module.transcriber, "extract_audio", fake_extract)
    monkeypatch.setattr(app_module.transcriber, "run_transcribe", lambda **_: result)
    monkeypatch.setattr(diarizer, "vad_available", lambda: False)
    monkeypatch.setattr(diarizer, "available", lambda: True)

    def blocked_diarize(**_):
        entered.set()
        release.wait(5)
        return [type("Chunk", (), {
            "start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
        })()]

    monkeypatch.setattr(diarizer, "diarize", blocked_diarize)
    monkeypatch.setattr(app_module.transcript_index_mod, "index_words_file",
                        lambda *args: indexed.append(args))
    monkeypatch.setattr("models_store.get_active_path", lambda: tmp_path / "model.bin")

    response = c.post("/api/v1/jobs/p/transcribe")
    tid = response.get_json()["id"]
    assert entered.wait(2)
    assert c.post(f"/api/v1/transcripts/{tid}/cancel").status_code == 200
    release.set()
    assert _wait_transcribe_worker_inactive(app.extensions["trove.transcribe"], tid)

    job = app.extensions["trove.transcribe"].get(tid)
    assert job.status is transcribe_jobs.TranscribeStatus.CANCELLED
    assert job.diarization_status is None and job.speaker_count is None
    assert {path: path.read_bytes() for path in published} == before
    assert indexed == []


def test_attempt_staging_transcribe_promotes_before_fts_index(client, monkeypatch, tmp_path):
    import app as app_module
    import diarizer
    from transcriber import TranscriptResult

    app, c = client
    media = _seed_transcribe_parent(app, tmp_path)
    observed = {"bases": [], "indexed": []}

    def fake_extract(_media, wav_path, **_kwargs):
        observed["wav"] = wav_path
        Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
        Path(wav_path).write_bytes(b"wav")

    result = TranscriptResult(
        language="en", duration=2.0,
        words=[{"w": "hello", "start": 0.0, "end": 1.0}],
        segments=[{"start": 0.0, "end": 1.0, "text": "hello",
                   "words": [{"w": "hello", "start": 0.0, "end": 1.0}]}],
    )
    real_write = app_module.transcriber.write_artifacts

    def spy_write(result_value, base_path):
        observed["bases"].append(base_path)
        real_write(result_value, base_path)

    def spy_index(_index, tid, words_path):
        observed["indexed"].append((tid, words_path, Path(words_path).exists()))

    monkeypatch.setattr(app_module.transcriber, "extract_audio", fake_extract)
    monkeypatch.setattr(app_module.transcriber, "run_transcribe", lambda **_: result)
    monkeypatch.setattr(app_module.transcriber, "write_artifacts", spy_write)
    monkeypatch.setattr(app_module.transcript_index_mod, "index_words_file", spy_index)
    monkeypatch.setattr(diarizer, "vad_available", lambda: False)
    monkeypatch.setattr(diarizer, "available", lambda: False)
    monkeypatch.setattr("models_store.get_active_path", lambda: tmp_path / "model.bin")

    response = c.post("/api/v1/jobs/p/transcribe")
    tid = response.get_json()["id"]
    deadline = time.time() + 5
    tm = app.extensions["trove.transcribe"]
    while tm.get(tid).status not in {
        transcribe_jobs.TranscribeStatus.DONE, transcribe_jobs.TranscribeStatus.ERROR,
    } and time.time() < deadline:
        time.sleep(0.01)

    final_base = str(media.with_suffix(""))
    assert "/.attempts/transcribe/" in observed["wav"]
    assert observed["bases"] and "/.attempts/transcribe/" in observed["bases"][0]
    assert all(Path(final_base + suffix).exists()
               for suffix in (".words.json", ".txt", ".srt", ".vtt"))
    assert observed["indexed"] == [(tid, final_base + ".words.json", True)]


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


def _reset_model_install_state(api_v1):
    with api_v1._install_lock:
        api_v1._install_state.update({
            "downloading": False,
            "name": None,
            "received": 0,
            "total": 0,
            "error": None,
            "done": False,
        })


def test_model_install_offline_rejects_before_state_or_thread_admission(
    client, monkeypatch, tmp_path
):
    import models_store
    from routes import api_v1

    app, c = client
    monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
    _reset_model_install_state(api_v1)
    with api_v1._install_lock:
        before = dict(api_v1._install_state)
    app.extensions["trove.network_policy"].enable_offline()
    urlopen_calls = []

    def forbidden_urlopen(*args, **kwargs):
        urlopen_calls.append((args, kwargs))
        raise AssertionError("offline install reached urlopen")

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("offline install admitted a worker")

    monkeypatch.setattr(models_store, "urlopen", forbidden_urlopen)
    monkeypatch.setattr(api_v1, "Thread", ForbiddenThread)

    response = c.post("/api/v1/models/ggml-tiny.bin/install")

    assert response.status_code == 409
    assert response.get_json() == {"error": "offline_network_disabled"}
    assert urlopen_calls == []
    with api_v1._install_lock:
        assert api_v1._install_state == before
    assert not (tmp_path / "models").exists()


def test_queued_model_install_rechecks_policy_before_urlopen(
    client, monkeypatch, tmp_path
):
    import models_store
    from network_policy import NetworkPolicyError
    from routes import api_v1

    app, c = client
    monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
    _reset_model_install_state(api_v1)
    captured = {}
    urlopen_calls = []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            captured["target"] = target

        def start(self):
            captured["started"] = True

    def forbidden_urlopen(*args, **kwargs):
        urlopen_calls.append((args, kwargs))
        raise AssertionError("queued offline install reached urlopen")

    monkeypatch.setattr(api_v1, "Thread", DeferredThread)
    monkeypatch.setattr(models_store, "urlopen", forbidden_urlopen)

    accepted = c.post("/api/v1/models/ggml-tiny.bin/install")
    assert accepted.status_code == 202
    assert captured["started"] is True

    app.extensions["trove.network_policy"].enable_offline()
    with pytest.raises(NetworkPolicyError) as denied:
        captured["target"]()

    assert denied.value.code == "offline_network_disabled"
    assert urlopen_calls == []
    with api_v1._install_lock:
        assert api_v1._install_state["downloading"] is False
        assert api_v1._install_state["done"] is False
        assert "offline_network_disabled" in api_v1._install_state["error"]
    assert not (tmp_path / "models").exists()


def test_active_model_download_blocks_offline_setting_until_release(
    client, monkeypatch, tmp_path
):
    import models_store

    app, c = client
    models_dir = tmp_path / "models"
    monkeypatch.setattr(models_store, "MODELS_DIR", models_dir)
    policy = app.extensions["trove.network_policy"]
    read_started = threading.Event()
    release_read = threading.Event()
    payload = b"model-data"

    class BlockingResponse:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._read = False

        def read(self, _size=-1):
            if self._read:
                return b""
            self._read = True
            read_started.set()
            assert release_read.wait(2)
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(models_store, "urlopen", lambda *a, **k: BlockingResponse())
    failures = []

    def download_model():
        try:
            models_store.download(
                "ggml-tiny.bin", network_policy=policy, verify=False
            )
        except BaseException as exc:  # expose worker failures to the test thread
            failures.append(exc)

    worker = threading.Thread(target=download_model)
    worker.start()
    try:
        assert read_started.wait(2)
        assert policy.active_leases == 1
        rejected = c.patch("/api/v1/settings", json={"offline": True})
        assert rejected.status_code == 409
        assert rejected.get_json() == {"error": "network_work_active"}
        assert c.get("/api/v1/settings").get_json()["offline"] is False
        assert policy.offline is False
    finally:
        release_read.set()
        worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert policy.active_leases == 0
    enabled = c.patch("/api/v1/settings", json={"offline": True})
    assert enabled.status_code == 200
    assert enabled.get_json()["offline"] is True


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


def test_transcript_history_retains_dismissed_record(client):
    app, c = client
    tm = app.extensions["trove.transcribe"]
    tj = transcribe_jobs.TranscribeJob(
        id="dismissed-t", parent_job_id="p1", model_used="ggml-tiny.bin",
        status=transcribe_jobs.TranscribeStatus.DONE,
    )
    with tm._lock:
        tm._jobs[tj.id] = tj
    assert c.post(f"/api/v1/transcripts/{tj.id}/dismiss").status_code == 204
    direct = c.get(f"/api/v1/transcripts/{tj.id}")
    assert direct.status_code == 200
    assert direct.get_json()["dismissed"] is True
    assert direct.get_json()["dismissed_at"] is not None
    listed = c.get("/api/v1/transcripts").get_json()["transcripts"]
    assert any(row["id"] == tj.id and row["dismissed"] is True for row in listed)


def test_create_app_configures_and_starts_all_job_sweepers(tmp_path, monkeypatch):
    import app as app_module
    from clip_jobs import ClipJobManager
    from jobs import JobManager
    from transcribe_jobs import TranscribeJobManager

    calls = []
    monkeypatch.setenv("TROVE_JOB_TTL_SECONDS", "17")
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path / "sweeper-downloads")
    monkeypatch.setattr(JobManager, "start_sweeper", lambda self, *a, **k: calls.append("download"))
    monkeypatch.setattr(TranscribeJobManager, "start_sweeper", lambda self, *a, **k: calls.append("transcribe"))
    monkeypatch.setattr(ClipJobManager, "start_sweeper", lambda self, *a, **k: calls.append("clip"))

    created = app_module.create_app()
    assert created.extensions["trove.jobs"].ttl_seconds == 17
    assert created.extensions["trove.transcribe"].ttl_seconds == 17
    assert created.extensions["trove.clips"].ttl_seconds == 17
    assert calls == ["download", "transcribe", "clip"]
    created.extensions["trove.jobs"].shutdown(wait=True)
    created.extensions["trove.transcribe"].shutdown(wait=True)
    created.extensions["trove.clips"].shutdown(wait=True)


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


def test_rate_limit_ignores_spoofed_forwarded_for_by_default(client):
    app, c = client
    _swap_rate_limiter(app, rate=1)

    statuses = [
        c.get(
            "/api/v1/jobs/missing/file",
            headers={"X-Forwarded-For": forwarded},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ).status_code
        for forwarded in ("198.51.100.1", "198.51.100.2")
    ]

    assert statuses == [404, 429]
    assert set(app.extensions["trove.rate_limiter"]._hits) == {"127.0.0.1"}


def test_rate_limit_uses_valid_rightmost_trusted_proxy_hop(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_TRUST_PROXY_HOPS", "1")
    monkeypatch.delenv("TROVE_TOKEN", raising=False)
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path / "proxy-downloads")
    created = app_module.create_app()
    limiter = created.extensions["trove.rate_limiter"]
    limiter.rate = 1
    c = created.test_client()

    try:
        rightmost_statuses = [
            c.get(
                "/api/v1/jobs/missing/file",
                headers={"X-Forwarded-For": forwarded},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            ).status_code
            for forwarded in (
                "198.51.100.1, 203.0.113.9",
                "198.51.100.2, 203.0.113.9",
                "198.51.100.2, 203.0.113.10",
            )
        ]
        malformed_statuses = [
            c.get(
                "/api/v1/jobs/missing/file",
                headers={"X-Forwarded-For": forwarded},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            ).status_code
            for forwarded in ("not-an-ip", "also-not-an-ip")
        ]

        assert rightmost_statuses == [404, 429, 404]
        assert malformed_statuses == [404, 429]
        assert set(limiter._hits) == {
            "127.0.0.1",
            "203.0.113.9",
            "203.0.113.10",
        }
    finally:
        created.extensions["trove.jobs"].shutdown(wait=True)
        created.extensions["trove.transcribe"].shutdown(wait=True)
        created.extensions["trove.clips"].shutdown(wait=True)


def test_create_app_reads_rate_limit_max_keys_at_runtime(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_RATE_LIMIT_MAX_KEYS", "4")
    monkeypatch.delenv("TROVE_TOKEN", raising=False)
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path / "limited-downloads")
    created = app_module.create_app()

    try:
        assert created.extensions["trove.rate_limiter"].max_keys == 4
    finally:
        created.extensions["trove.jobs"].shutdown(wait=True)
        created.extensions["trove.transcribe"].shutdown(wait=True)
        created.extensions["trove.clips"].shutdown(wait=True)


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

def test_contract_fixture_bulk_submit_uses_real_route_validation(client):
    app, c = client
    contract = PHASE0_CONTRACT["bulk_submit"]

    def deterministic_enqueue(url, _fmt, _fmt_id, title, _thumbnail="", **_kwargs):
        job = Job(id="job_1", url=url, title=title, status=JobStatus.QUEUED)
        app.extensions["trove.jobs"]._jobs[job.id] = job
        return job.id

    app.extensions["trove.actions"]["enqueue_download"] = deterministic_enqueue
    response = c.post("/api/v1/jobs/bulk", json=contract["request"])

    assert response.status_code == 207
    assert response.get_json() == contract["response"]

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
            "https://93.184.216.34/ok-1",
            "--exec=evil",                # rejected by safety
            "https://93.184.216.34/ok-2",
        ],
        "format": "audio",
    })
    assert r.status_code == 207  # multi-status because one failed
    body = r.get_json()
    assert body["submitted"] == 2 and body["failed"] == 1
    assert body["results"][1]["error"] == "unsupported_url"


def test_submit_bulk_partial_capacity_returns_exact_rows_header_and_no_hidden_jobs(client):
    app, c = client
    calls = 0

    def capacity_one(url, _fmt, _fmt_id, title, _thumbnail="", **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise QueueFullError("download queue full")
        job = Job(id="accepted-1", url=url, title=title, status=JobStatus.QUEUED)
        app.extensions["trove.jobs"]._jobs[job.id] = job
        return job.id

    app.extensions["trove.actions"]["enqueue_download"] = capacity_one
    response = c.post("/api/v1/jobs/bulk", json={
        "urls": [
            "https://93.184.216.34/one",
            "https://93.184.216.34/two",
            "https://93.184.216.34/three",
        ],
    })

    assert response.status_code == 207
    assert response.headers["Retry-After"] == "1"
    assert response.get_json() == {
        "submitted": 1,
        "failed": 2,
        "results": [
            {
                "url": "https://93.184.216.34/one",
                "id": "accepted-1",
                "title": "https://93.184.216.34/one",
            },
            {
                "url": "https://93.184.216.34/two",
                "error": "queue_full",
                "retry_after": 1,
            },
            {
                "url": "https://93.184.216.34/three",
                "error": "queue_full",
                "retry_after": 1,
            },
        ],
    }
    assert [job.id for job in app.extensions["trove.jobs"].snapshot_jobs()] == ["accepted-1"]


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
    body = {"url": "https://93.184.216.34/a", "title": "T"}
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
    body = {"url": "https://93.184.216.34/x", "title": "X"}
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

def test_contract_fixture_word_edit_supports_canonical_and_legacy_wire_shapes(
    client, tmp_path,
):
    app, c = client
    contract = PHASE0_CONTRACT["word_edit"]
    expected = contract["response_subset"]
    _seed_done_transcript(app, tmp_path, words_data={
        "schema_version": 2,
        "duration": 1.0,
        "words": [{
            "idx": expected["word"]["idx"],
            "w": "uncorrected",
            "original_w": "uncorrected",
            "start": 0.0,
            "end": 1.0,
            "edited": False,
            "deleted": False,
        }],
        "segments": [],
    })
    tm = app.extensions["trove.transcribe"]
    transcript = tm._jobs.pop("t1")
    transcript.id = expected["tid"]
    tm._jobs[transcript.id] = transcript
    endpoint = (
        f"/api/v1/transcripts/{expected['tid']}/words/"
        f"{expected['word']['idx']}"
    )

    canonical = c.post(endpoint, json=contract["request"])
    assert canonical.status_code == 200
    assert canonical.get_json()["tid"] == expected["tid"]
    assert {
        key: canonical.get_json()["word"][key]
        for key in expected["word"]
    } == expected["word"]
    assert "Warning" not in canonical.headers

    legacy = c.post(endpoint, json=contract["legacy_request"])
    assert legacy.status_code == 200
    assert legacy.headers["Warning"] == '299 Spool "text is deprecated; use w"'
    assert {
        key: legacy.get_json()["word"][key]
        for key in expected["word"]
    } == expected["word"]


@pytest.mark.parametrize(
    ("canonical_w", "legacy_text"),
    [
        ("corrected", "different"),
        ("corrected", 123),
        (123, "corrected"),
        ("corrected", None),
        (None, "corrected"),
    ],
)
def test_contract_fixture_word_edit_rejects_conflicting_w_and_text(
    client, tmp_path, canonical_w, legacy_text,
):
    app, c = client
    request_body = {
        "op": PHASE0_CONTRACT["word_edit"]["request"]["op"],
        "w": canonical_w,
        "text": legacy_text,
    }
    _seed_done_transcript(app, tmp_path, words_data=_editable_words())

    response = c.post("/api/v1/transcripts/t1/words/1", json=request_body)

    assert response.status_code == 400
    assert response.get_json() == {"error": "conflicting_word_text"}

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


def test_chunk_json_corrupt_transcript_is_structured_500(client, tmp_path):
    """A corrupt .words.json must yield a structured JSON 500, not an HTML stack trace."""
    app, c = client
    # Arrange: seed a done transcript whose .words.json contains invalid JSON.
    dd = app.extensions["trove.download_dir"]
    os.makedirs(dd, exist_ok=True)
    media = dd / "abc.mp4"
    media.write_bytes(b"x")
    base = str(dd / "abc")
    with open(base + ".words.json", "wb") as f:
        f.write(b"{not json")
    jm = app.extensions["trove.jobs"]
    jm._jobs["abc"] = Job(id="abc", url="https://x", title="Clip",
                          status=JobStatus.DONE, file_path=str(media))
    tm = app.extensions["trove.transcribe"]
    tm._jobs["t1"] = transcribe_jobs.TranscribeJob(
        id="t1", parent_job_id="abc", model_used="m",
        status=transcribe_jobs.TranscribeStatus.DONE,
    )
    r = c.get("/api/v1/transcripts/t1/chunk?format=json")
    assert r.status_code == 500
    assert r.get_json()["error"] == "transcript_unreadable"


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


def test_contract_fixture_openapi_documents_word_edit_and_bulk_submit(client):
    _, c = client
    paths = c.get("/api/v1/openapi.json").get_json()["paths"]

    word = paths["/transcripts/{tid}/words/{idx}"]["post"]
    word_request = word["requestBody"]["content"]["application/json"]["schema"]
    assert set(PHASE0_CONTRACT["word_edit"]["request"]) <= set(
        word_request["properties"]
    )
    replacement_variant, structural_variant = word_request["oneOf"]
    assert replacement_variant["properties"]["op"]["enum"] == [
        "set_text", "insert_after",
    ]
    assert replacement_variant["required"] == ["op"]
    assert replacement_variant["anyOf"] == [
        {"required": ["w"]},
        {"required": ["text"]},
    ]
    assert structural_variant["properties"]["op"]["enum"] == [
        "delete", "merge_next",
    ]
    assert structural_variant["not"] == {
        "anyOf": [{"required": ["w"]}, {"required": ["text"]}],
    }
    word_response = word["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(PHASE0_CONTRACT["word_edit"]["response_subset"]) <= set(
        word_response["properties"]
    )
    assert set(PHASE0_CONTRACT["word_edit"]["response_subset"]["word"]) <= set(
        word_response["properties"]["word"]["properties"]
    )
    assert word["responses"]["200"]["headers"]["Warning"]["schema"] == {
        "type": "string",
        "enum": ['299 Spool "text is deprecated; use w"'],
    }
    conflict_schema = word["responses"]["400"]["content"]["application/json"]["schema"]
    assert "conflicting_word_text" in conflict_schema["properties"]["error"]["enum"]

    bulk = paths["/jobs/bulk"]["post"]
    bulk_request = bulk["requestBody"]["content"]["application/json"]["schema"]
    assert set(PHASE0_CONTRACT["bulk_submit"]["request"]) <= set(
        bulk_request["properties"]
    )
    bulk_response = bulk["responses"]["207"]["content"]["application/json"]["schema"]
    assert set(PHASE0_CONTRACT["bulk_submit"]["response"]) <= set(
        bulk_response["properties"]
    )
    row_variants = bulk_response["properties"]["results"]["items"]["oneOf"]
    for row in PHASE0_CONTRACT["bulk_submit"]["response"]["results"]:
        matching_variants = [
            variant
            for variant in row_variants
            if set(variant["required"]) <= set(row) <= set(variant["properties"])
        ]
        assert len(matching_variants) == 1, row


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
    # IP-literal: is_safe_url checks it without DNS (offline-safe); what this test
    # proves is the idempotency-key placeholder is released on enqueue failure.
    r = c.post("/api/v1/jobs", json={"url": "https://93.184.216.34/v", "title": "T"}, headers=h)
    assert r.status_code == 503
    # Recovery: a real enqueue with the same key now succeeds (i.e. the
    # placeholder didn't stick around).
    def ok(url, *a, **kw):
        jm = app.extensions["trove.jobs"]
        jm._jobs["recovered"] = Job(id="recovered", url=url, title="T",
                                     status=JobStatus.QUEUED)
        return "recovered"
    app.extensions["trove.actions"]["enqueue_download"] = ok
    r2 = c.post("/api/v1/jobs", json={"url": "https://93.184.216.34/v", "title": "T"}, headers=h)
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
    # IP-literal: is_safe_url checks it without DNS (offline-safe); what this test
    # proves is a TTL-evicted idempotency key re-submits a fresh job.
    body = {"url": "https://93.184.216.34/v", "title": "T"}

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
    for k in (
        "fast_default", "default_preset", "clip_workers", "max_workers", "mcp_transport",
        "reasoning_provider", "reasoning_egress_consent",
    ):
        assert k in body
    assert body["fast_default"] is True
    assert body["clip_workers"] == 2
    assert body["default_preset"] == "tiktok"
    assert body["reasoning_provider"] == "none"
    assert body["reasoning_egress_consent"] is False

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
    assert c.patch("/api/v1/settings", json={"reasoning_provider": "surprise-cloud"}).status_code == 400
    # a non-bool for a bool field is a 400 (avoid bool("false") == True footguns)
    assert c.patch("/api/v1/settings", json={"fast_default": "yes"}).status_code == 400
    assert c.patch("/api/v1/settings", json={"reasoning_egress_consent": "yes"}).status_code == 400
    assert c.patch("/api/v1/settings", json={"reasoning_egress_consent": True}).status_code == 400
    # the rejected writes left the store untouched
    assert c.get("/api/v1/settings").get_json()["default_preset"] == "tiktok"


def test_reasoning_provider_and_consent_roundtrip_reset(client):
    _, c = client

    enabled = c.patch("/api/v1/settings", json={
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    })
    assert enabled.status_code == 200
    assert enabled.get_json()["reasoning_egress_consent"] is True
    assert os.environ["SPOOL_LLM_PROVIDER"] == "codex"
    assert os.environ["SPOOL_LLM_EGRESS_CONSENT"] == "1"

    disabled = c.patch("/api/v1/settings", json={"reasoning_provider": "none"})
    assert disabled.get_json()["reasoning_egress_consent"] is False
    assert os.environ["SPOOL_LLM_PROVIDER"] == "none"
    assert "SPOOL_LLM_EGRESS_CONSENT" not in os.environ

    selected_again = c.patch("/api/v1/settings", json={"reasoning_provider": "codex"})
    assert selected_again.get_json()["reasoning_egress_consent"] is False
    assert os.environ["SPOOL_LLM_PROVIDER"] == "codex"
    assert "SPOOL_LLM_EGRESS_CONSENT" not in os.environ


def test_failed_settings_persistence_does_not_change_runtime_state(client, monkeypatch):
    app, c = client
    enabled = c.patch("/api/v1/settings", json={
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    }).get_json()
    store = app.extensions["trove.settings"]
    policy = app.extensions["trove.network_policy"]
    settings_path = Path(store.path)
    before_file = settings_path.read_bytes()

    def fail_save(_overrides):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", fail_save)
    failed = c.patch("/api/v1/settings", json={
        "reasoning_provider": "none",
        "offline": True,
    })

    assert failed.status_code == 500
    assert store.get() == enabled
    assert policy.offline is False
    assert Path(store.path).read_bytes() == before_file
    from settings import SettingsStore
    assert SettingsStore(store.path).get() == enabled
    assert os.environ["SPOOL_LLM_PROVIDER"] == "codex"
    assert os.environ["SPOOL_LLM_EGRESS_CONSENT"] == "1"
    assert "SPOOL_OFFLINE" not in os.environ


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


def test_offline_setting_drives_spool_offline_env(client, monkeypatch):
    """The studio's Offline toggle must ENFORCE — patching ``offline`` flips the one switch
    clip.llm.is_offline() reads (``SPOOL_OFFLINE``), so egress providers refuse in-process."""
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    app, c = client
    policy = app.extensions["trove.network_policy"]
    r = c.get("/api/v1/settings")
    assert r.get_json()["offline"] is False
    assert policy.offline is False

    r = c.patch("/api/v1/settings", json={"offline": True})
    assert r.status_code == 200 and r.get_json()["offline"] is True
    assert os.environ.get("SPOOL_OFFLINE") == "1"     # llm.is_offline() now blocks egress
    assert policy.offline is True

    r = c.patch("/api/v1/settings", json={"offline": False})
    assert r.get_json()["offline"] is False
    assert os.environ.get("SPOOL_OFFLINE") is None
    assert policy.offline is False

    # bool only — don't silently coerce ("yes-please" is truthy, a privacy footgun)
    r = c.patch("/api/v1/settings", json={"offline": "yes-please"})
    assert r.status_code == 400


def test_create_app_seeds_offline_from_env_at_boot(tmp_path, monkeypatch):
    """A launch with ``SPOOL_OFFLINE=1`` already set must seed the persisted setting so the
    studio badge reflects reality (offline-true) at boot — not the honest default of false."""
    import app as _app_module
    dl = tmp_path / "downloads"
    dl.mkdir(parents=True)
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", dl)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("SPOOL_OFFLINE", "1")
    application = _app_module.create_app()
    body = application.test_client().get("/api/v1/settings").get_json()
    assert body["offline"] is True
    assert application.extensions["trove.network_policy"].offline is True


def test_offline_patch_rejects_active_network_work_without_mutating_settings(client, monkeypatch):
    app, c = client
    store = app.extensions["trove.settings"]
    policy = app.extensions["trove.network_policy"]
    settings_path = Path(store.path)
    before_store = store.get()
    before_file = settings_path.read_bytes() if settings_path.exists() else None
    save_calls = 0
    real_save = store._save

    def counting_save(*args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        return real_save(*args, **kwargs)

    monkeypatch.setattr(store, "_save", counting_save)
    with policy.egress("url_download"):
        response = c.patch("/api/v1/settings", json={"offline": True})

    assert response.status_code == 409
    assert response.get_json() == {"error": "network_work_active"}
    assert save_calls == 0
    assert policy.offline is False
    assert store.get() == before_store
    assert (settings_path.read_bytes() if settings_path.exists() else None) == before_file
    from settings import SettingsStore
    assert SettingsStore(store.path).get() == before_store
    assert os.environ.get("SPOOL_OFFLINE") is None


def test_settings_patches_serialize_the_policy_through_route_return(client, monkeypatch):
    app, _ = client
    store = app.extensions["trove.settings"]
    policy = app.extensions["trove.network_policy"]
    first_updated = threading.Event()
    release_first = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    responses = []
    real_update = store.update
    calls = 0

    def blocking_update(*args, **kwargs):
        nonlocal calls
        values = real_update(*args, **kwargs)
        calls += 1
        if calls == 1:
            first_updated.set()
            assert release_first.wait(1)
        return values

    monkeypatch.setattr(store, "update", blocking_update)

    def patch(body, done):
        with app.test_client() as thread_client:
            responses.append(thread_client.patch("/api/v1/settings", json=body))
        done.set()

    first = threading.Thread(target=patch, args=({"offline": True}, first_done))
    first.start()
    assert first_updated.wait(1)

    second = threading.Thread(target=patch, args=({"fast_default": False}, second_done))
    second.start()
    assert second_done.wait(0.05) is False

    release_first.set()
    assert first_done.wait(1)
    assert second_done.wait(1)
    first.join(timeout=1)
    second.join(timeout=1)

    assert [response.status_code for response in responses] == [200, 200]
    assert store.get()["offline"] is True
    assert store.get()["fast_default"] is False
    persisted = Path(store.path).read_text()
    assert '"offline": true' in persisted
    assert '"fast_default": false' in persisted
    assert os.environ.get("SPOOL_OFFLINE") == "1"
    assert policy.offline is True


@pytest.mark.parametrize(
    "provider,consent,expected_provider,expected_consent",
    [
        ("codex", "1", "codex", True),
        ("codex", None, "codex", False),
        ("none", "1", "none", False),
        ("invalid-provider", "1", "none", False),
    ],
)
def test_create_app_seeds_only_valid_explicit_reasoning_consent(
    tmp_path, monkeypatch, provider, consent, expected_provider, expected_consent,
):
    import app as _app_module
    dl = tmp_path / f"downloads-{provider}-{consent}"
    dl.mkdir(parents=True)
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", dl)
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("SPOOL_LLM_PROVIDER", provider)
    if consent is None:
        monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    else:
        monkeypatch.setenv("SPOOL_LLM_EGRESS_CONSENT", consent)

    application = _app_module.create_app()
    body = application.test_client().get("/api/v1/settings").get_json()

    assert body["reasoning_provider"] == expected_provider
    assert body["reasoning_egress_consent"] is expected_consent
    assert os.environ["SPOOL_LLM_PROVIDER"] == expected_provider
    assert (os.environ.get("SPOOL_LLM_EGRESS_CONSENT") == "1") is expected_consent


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
    assert set(body["candidates"][0]["factors"]) == {"hook", "self_contained", "arc", "energy", "length_fit", "boundary_quality"}
    assert set(body["weights"]) == {"hook", "self_contained", "arc", "energy", "length_fit", "boundary_quality"}


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

def _enable_reasoning(c):
    response = c.patch("/api/v1/settings", json={
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    })
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("privacy_state", "expected_error"),
    [
        ("provider-none", "reasoning_provider_required"),
        ("consent-missing", "egress_consent_required"),
        ("offline", "offline_network_disabled"),
    ],
)
def test_agent_route_rejects_privacy_state_before_client_or_provider_work(
    client, monkeypatch, privacy_state, expected_error,
):
    import clip.agent as clip_agent
    import trove_client

    app, c = client
    if privacy_state == "consent-missing":
        assert c.patch(
            "/api/v1/settings", json={"reasoning_provider": "codex"}
        ).status_code == 200
    elif privacy_state == "offline":
        _enable_reasoning(c)
        assert c.patch("/api/v1/settings", json={"offline": True}).status_code == 200

    calls = []
    monkeypatch.setattr(
        trove_client,
        "TroveClient",
        lambda: calls.append("client") or object(),
    )
    monkeypatch.setattr(
        clip_agent,
        "run_agent",
        lambda *_args, **_kwargs: calls.append("provider") or {
            "action": "reply", "reply": "unexpected", "tools": [], "jobs": [],
        },
    )

    response = c.post("/api/v1/agent", json={"message": "what is queued?"})

    assert response.status_code == 409
    assert response.get_json() == {"error": expected_error}
    assert calls == []
    assert app.extensions["trove.network_policy"].active_leases == 0


def test_agent_route_threads_the_app_policy_and_live_settings_getter(client, monkeypatch):
    import clip.agent as clip_agent

    app, c = client
    _enable_reasoning(c)
    captured = {}

    def fake_run(message, **kwargs):
        captured.update(kwargs)
        return {"action": "reply", "reply": "ok", "tools": [], "jobs": []}

    monkeypatch.setattr(clip_agent, "run_agent", fake_run)
    response = c.post("/api/v1/agent", json={"message": "hi"})

    assert response.status_code == 200
    assert captured["network_policy"] is app.extensions["trove.network_policy"]
    assert captured["privacy_state"] == app.extensions["trove.settings"].get
    assert app.extensions["trove.clip_runner"].network_policy is captured["network_policy"]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (lambda llm: llm.OfflineError("offline"), "offline_network_disabled"),
        (lambda llm: llm.ReasoningDisabledError("none"), "reasoning_provider_required"),
        (lambda llm: llm.EgressConsentError("revoked"), "egress_consent_required"),
    ],
)
def test_agent_route_surfaces_last_moment_privacy_denials_as_exact_409(
    client, monkeypatch, error, expected_code,
):
    import clip.agent as clip_agent
    from clip import llm

    _, c = client
    _enable_reasoning(c)
    monkeypatch.setattr(
        clip_agent,
        "run_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error(llm)),
    )

    response = c.post("/api/v1/agent", json={"message": "hi"})

    assert response.status_code == 409
    assert response.get_json() == {"error": expected_code}


def test_active_reasoning_lease_blocks_offline_persistence_until_completion(
    client, monkeypatch,
):
    from clip import llm

    app, c = client
    _enable_reasoning(c)
    policy = app.extensions["trove.network_policy"]
    settings = app.extensions["trove.settings"]
    entered = threading.Event()
    release = threading.Event()
    outcomes = []

    class BlockingCodexProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            self.argv = argv

        def communicate(self, input=None, timeout=None):
            assert policy.active_leases == 1
            entered.set()
            assert release.wait(2), "reasoning completion was not released"
            Path(self.argv[self.argv.index("-o") + 1]).write_text("ok")
            return "", ""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", BlockingCodexProcess)

    def complete():
        try:
            outcomes.append(llm.complete(
                "transcript",
                provider="codex",
                network_policy=policy,
                privacy_state=settings.get,
            ))
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    worker = threading.Thread(target=complete)
    worker.start()
    assert entered.wait(2), "reasoning lease was not acquired"

    blocked = c.patch("/api/v1/settings", json={"offline": True})
    assert blocked.status_code == 409
    assert blocked.get_json() == {"error": "network_work_active"}
    assert settings.get()["offline"] is False
    assert "SPOOL_OFFLINE" not in os.environ

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert outcomes == ["ok"]
    assert policy.active_leases == 0
    assert c.patch("/api/v1/settings", json={"offline": True}).status_code == 200

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
    _enable_reasoning(c)
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
    _enable_reasoning(c)
    b = c.post("/api/v1/agent", json={"message": "clip it"}).get_json()
    assert b["action"] == "clarify" and b["question"] == "Which source?"
    assert b["options"] == ["a", "b"] and b["kind"] == "enum"


def test_agent_route_returns_exact_mutation_disabled_envelope(client, monkeypatch):
    import clip.agent as clip_agent
    captured = {}

    def disabled(message, **kwargs):
        captured.update(kwargs)
        return dict(PHASE0_CONTRACT["agent_mutation_disabled"])

    monkeypatch.setattr(clip_agent, "run_agent", disabled)
    _, c = client
    _enable_reasoning(c)
    r = c.post("/api/v1/agent", json={
        "message": "delete my recipe",
        "confirm_tool": "delete_recipe",
    })

    assert r.status_code == 409
    assert r.get_json() == PHASE0_CONTRACT["agent_mutation_disabled"]
    assert captured["confirmed_tool"] == "delete_recipe"


def test_agent_route_missing_message_400(client):
    _, c = client
    assert c.post("/api/v1/agent", json={}).status_code == 400


def test_agent_route_llm_unavailable_503(client, monkeypatch):
    import clip.agent as clip_agent
    from clip import llm
    def boom(*a, **k):
        raise llm.ProviderUnavailableError("missing")
    monkeypatch.setattr(clip_agent, "run_agent", boom)
    _, c = client
    _enable_reasoning(c)
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
    assert c.patch("/api/v1/settings", json={
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    }).status_code == 200
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
    assert c.patch("/api/v1/settings", json={
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    }).status_code == 200
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
                                        "target": "https://93.184.216.34/@x", "recipe_id": "r1"})
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


def test_watch_create_and_update_reject_non_object_json(client, tmp_path):
    app, c = client
    created = c.post("/api/v1/watches", json={
        "name": "local", "kind": "folder", "target": str(tmp_path),
    }).get_json()
    store = app.extensions["trove.watches"]
    before = store.list()

    create_response = c.post("/api/v1/watches", json=[{"kind": "folder"}])
    update_response = c.patch(
        f"/api/v1/watches/{created['id']}", json=[{"name": "changed"}],
    )

    assert create_response.status_code == 400
    assert create_response.get_json() == {"error": "bad_watch"}
    assert update_response.status_code == 400
    assert update_response.get_json() == {"error": "bad_watch"}
    assert store.list() == before


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("folder", "/tmp/local-watch"),
        ("playlist", "https://93.184.216.34/list"),
    ],
)
def test_watch_update_returns_404_if_record_is_deleted_after_preread(
    client, monkeypatch, kind, target,
):
    app, c = client
    store = app.extensions["trove.watches"]
    created = c.post("/api/v1/watches", json={
        "name": "race", "kind": kind, "target": target,
    }).get_json()
    monkeypatch.setattr(store, "update", lambda *_a, **_kw: None)

    response = c.patch(f"/api/v1/watches/{created['id']}", json={"name": "too late"})

    assert response.status_code == 404
    assert response.get_json() == {"error": "not_found"}


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


def test_watch_ingest_queue_full_returns_429_without_retry_or_seen_state(
    client, tmp_path, monkeypatch,
):
    app, c = client
    inbox = tmp_path / "capacity-inbox"
    inbox.mkdir()
    (inbox / "queued.mp4").write_bytes(b"media")
    created = c.post("/api/v1/watches", json={
        "name": "Capacity",
        "kind": "folder",
        "target": str(inbox),
    }).get_json()
    store = app.extensions["trove.watches"]
    before = copy.deepcopy(store.get(created["id"]))
    monkeypatch.setattr(watcher, "list_folder_items", lambda *_a, **_kw: ["queued.mp4"])

    def saturated(*_args, **_kwargs):
        raise QueueFullError("download queue full")

    monkeypatch.setattr(app.extensions["trove.jobs"], "submit", saturated)
    response = c.post(f"/api/v1/watches/{created['id']}/scan")

    _assert_queue_full_response(response)
    assert store.get(created["id"]) == before


def test_offline_folder_watch_create_update_and_scan_remain_local(client, tmp_path, monkeypatch):
    monkeypatch.setattr("clip.moments.find_moments", lambda *a, **k: [])
    app, c = client
    inbox = tmp_path / "offline-inbox"
    inbox.mkdir()
    video = inbox / "local.mp4"
    video.write_bytes(b"local")
    old = time.time() - 3600
    os.utime(video, (old, old))
    app.extensions["trove.network_policy"].enable_offline()

    created = c.post("/api/v1/watches", json={
        "name": "Local", "kind": "folder", "target": str(inbox),
    })
    assert created.status_code == 201
    watch_id = created.get_json()["id"]
    updated = c.patch(f"/api/v1/watches/{watch_id}", json={"name": "Still local"})
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "Still local"

    scanned = c.post(f"/api/v1/watches/{watch_id}/scan")
    assert scanned.status_code == 200
    assert len(scanned.get_json()["ingested"]) == 1
    assert "local.mp4" in c.get(f"/api/v1/watches/{watch_id}").get_json()["seen"]


def test_offline_background_reconcile_skips_remote_and_continues_local(client, tmp_path, monkeypatch):
    app, c = client
    remote = c.post("/api/v1/watches", json={
        "name": "Remote", "kind": "channel", "target": "https://93.184.216.34/@remote",
    }).get_json()
    folder = tmp_path / "empty-local-folder"
    folder.mkdir()
    local = c.post("/api/v1/watches", json={
        "name": "Local", "kind": "folder", "target": str(folder),
    }).get_json()
    remote_before = app.extensions["trove.watches"].get(remote["id"])
    playlist_calls = []
    folder_calls = []
    set_state_ids = []
    warning_calls = []
    store = app.extensions["trove.watches"]
    real_set_state = store.set_state

    monkeypatch.setattr(
        watcher, "list_playlist_items", lambda *a, **kw: playlist_calls.append((a, kw)) or [],
    )
    monkeypatch.setattr(
        watcher, "list_folder_items", lambda path, **kw: folder_calls.append(path) or [],
    )

    def record_set_state(watch_id, **state):
        set_state_ids.append(watch_id)
        return real_set_state(watch_id, **state)

    monkeypatch.setattr(store, "set_state", record_set_state)
    monkeypatch.setattr(app.logger, "warning", lambda *a, **kw: warning_calls.append((a, kw)))
    app.extensions["trove.network_policy"].enable_offline()

    app.extensions["trove.watch_reconcile_all"]()

    assert playlist_calls == []
    assert folder_calls == [str(folder)]
    assert remote["id"] not in set_state_ids
    assert local["id"] in set_state_ids
    assert store.get(remote["id"]) == remote_before
    assert warning_calls == []


def test_manual_remote_scan_invalidates_cache_after_waiting_for_same_watch_poll(
    client, monkeypatch,
):
    app, c = client
    target = "https://93.184.216.34/list"
    watch = c.post("/api/v1/watches", json={
        "name": "Remote", "kind": "playlist", "target": target,
    }).get_json()
    cache_key = (target, 30)
    watcher.clear_listing_cache()
    poll_in_listing = threading.Event()
    allow_poll_finish = threading.Event()
    waiter_at_lock = threading.Event()
    cache_present_at_listing = []
    errors = []

    reconcile_all = app.extensions["trove.watch_reconcile_all"]
    reconcile_one = inspect.getclosurevars(reconcile_all).nonlocals["_reconcile_one"]
    watch_lock = inspect.getclosurevars(reconcile_one).nonlocals["_watch_lock"]
    watch_locks = inspect.getclosurevars(watch_lock).nonlocals["_watch_locks"]
    real_lock = threading.Lock()

    class _ObservedLock:
        def __enter__(self):
            if real_lock.locked():
                waiter_at_lock.set()
            real_lock.acquire()
            return self

        def __exit__(self, *_exc):
            real_lock.release()

    watch_locks[watch["id"]] = _ObservedLock()

    def fake_listing(_target, **_kwargs):
        cache_present_at_listing.append(cache_key in watcher._listing_cache)
        if len(cache_present_at_listing) == 1:
            poll_in_listing.set()
            assert allow_poll_finish.wait(timeout=3)
            watcher._listing_cache[cache_key] = {
                "items": ["https://youtu.be/poll"], "expires": float("inf"), "fails": 0,
            }
        return []

    monkeypatch.setattr(watcher, "list_playlist_items", fake_listing)

    def run(callable_):
        try:
            callable_()
        except BaseException as error:
            errors.append(error)

    poll = threading.Thread(target=lambda: run(reconcile_all))
    manual = threading.Thread(
        target=lambda: run(lambda: app.extensions["trove.watch_reconcile"](watch["id"])),
    )

    try:
        poll.start()
        assert poll_in_listing.wait(timeout=3)
        manual.start()
        assert waiter_at_lock.wait(timeout=3)
        allow_poll_finish.set()
        poll.join(timeout=3)
        manual.join(timeout=3)

        assert not poll.is_alive() and not manual.is_alive()
        assert errors == []
        assert cache_present_at_listing == [False, False]
    finally:
        allow_poll_finish.set()
        poll.join(timeout=3)
        manual.join(timeout=3)
        watcher.clear_listing_cache()


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
