"""Tests for the /api/v1 clip endpoints (the render queue + clip operations).

These cover the wire contract: status codes, the clip-job view shape, validation, and
that each endpoint submits a ClipJob that runs the right engine work. The clip engine
functions are mocked (via clip_runner) so jobs complete without ffmpeg/codex — the same
discipline the existing endpoint tests use for downloads/whisper.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app import create_app
from jobs import Job, JobStatus


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TROVE_JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("TROVE_RATE_LIMIT", "0")
    monkeypatch.setenv("SPOOL_LLM_PROVIDER", "codex")
    monkeypatch.setenv("SPOOL_LLM_EGRESS_CONSENT", "1")
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    import app as _app_module
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_app_module, "DOWNLOAD_DIR", tmp_path / "downloads")
    app = create_app()
    return app, app.test_client()


@pytest.fixture(autouse=True)
def mock_engine(monkeypatch):
    """Replace the (separately-tested) clip engine functions with fast fakes that just
    write their output files, so submitted ClipJobs complete deterministically."""
    import clip_runner as cr

    def w(p, b=b"V"):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(b)
        return p

    monkeypatch.setattr(cr.cutter, "cut", lambda s, a, b, out, **k: w(out, b"C"))
    monkeypatch.setattr(cr.reframe, "detect_faces", lambda c, **k: {
        "width": 2, "height": 1, "frame_path": k.get("frame_path"),
        "rois": {"left": {"x": 0, "y": 0, "w": 1, "h": 1}, "right": {"x": 1, "y": 0, "w": 1, "h": 1}}})
    monkeypatch.setattr(cr.reframe, "probe_dimensions", lambda p: (1920, 1080))
    monkeypatch.setattr(cr.reframe, "speaker_track", lambda c, **k: {
        "segments": [], "roiL": k["roi_left"], "roiR": k["roi_right"], "source": "fused"})
    monkeypatch.setattr(cr.reframe, "render", lambda c, t, **k: w(k["out_path"], b"R"))
    monkeypatch.setattr(cr.captioner, "generate", lambda words, **k: (Path(k["out_ass_path"]).write_text("a"), k["out_ass_path"])[-1])
    monkeypatch.setattr(cr.captioner, "burn", lambda v, a, out, **k: w(out, b"X"))
    monkeypatch.setattr(cr.exporter, "export", lambda c, **k: w(k["out_path"], b"O"))
    monkeypatch.setattr(cr.moments, "find_moments", lambda words, **k: [
        {"start": 1.0, "end": 9.0, "title": "M", "rationale": "r", "mode": k.get("mode", "funny"), "signals": []}])


# ---- seeding helpers -------------------------------------------------

def _seed_source(app, sid="src1", *, with_transcript=True):
    dl = app.extensions["trove.download_dir"]
    (dl / f"{sid}.mp4").write_bytes(b"MEDIA")
    app.extensions["trove.jobs"]._jobs[sid] = Job(
        id=sid, url="u", title="T", status=JobStatus.DONE, file_path=str(dl / f"{sid}.mp4"))
    if with_transcript:
        data = {"schema_version": 2, "language": "en", "duration": 60.0, "edited_at": None,
                "words": [{"idx": 0, "w": "hi", "original_w": "hi", "start": 1.0, "end": 2.0,
                           "edited": False, "deleted": False}],
                "segments": [{"start": 0.0, "end": 10.0, "text": "hi", "word_idxs": [0], "speaker": "SPEAKER_00"}],
                "bookmarks": []}
        (dl / f"{sid}.words.json").write_text(json.dumps(data))


def _seed_clip(app, clip_id="clipA", source_id="src1", *, files=("clip.mp4",), start=2.0, end=12.0):
    cr = app.extensions["trove.clip_runner"]
    cr.write_clip_meta(clip_id, source_id=source_id, start=start, end=end)
    for f in files:
        (cr.clip_dir(clip_id) / f).write_bytes(b"V")


def _await(c, jid, status="done", tries=150):
    for _ in range(tries):
        r = c.get(f"/api/v1/clip-jobs/{jid}")
        if r.status_code == 200 and r.get_json()["status"] == status:
            return r.get_json()
        time.sleep(0.02)
    last = c.get(f"/api/v1/clip-jobs/{jid}").get_json()
    raise AssertionError(f"job {jid} never reached {status}: {last}")


# ---- moments ---------------------------------------------------------

def test_find_moments_creates_job_and_returns_candidates(client):
    app, c = client
    _seed_source(app)
    r = c.post("/api/v1/sources/src1/moments", json={"mode": "insightful", "count": 4})
    assert r.status_code == 201
    body = r.get_json()
    assert body["kind"] == "moments" and body["source_id"] == "src1"
    done = _await(c, body["id"])
    assert done["result"]["count"] == 1
    assert done["result"]["candidates"][0]["title"] == "M"
    assert done["result"]["candidates"][0]["mode"] == "insightful"


def test_find_moments_404_when_source_not_ready(client):
    app, c = client
    app.extensions["trove.jobs"]._jobs["p"] = Job(id="p", url="u", title="t", status=JobStatus.DOWNLOADING)
    assert c.post("/api/v1/sources/p/moments", json={}).status_code == 404


def test_find_moments_409_when_no_transcript(client):
    app, c = client
    _seed_source(app, with_transcript=False)
    r = c.post("/api/v1/sources/src1/moments", json={})
    assert r.status_code == 409 and r.get_json()["error"] == "no_transcript"


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("/api/v1/sources/src1/moments", {"mode": "funny"}),
        ("/api/v1/sources/src1/produce", {"content_mode": "funny", "count": 1}),
    ],
)
@pytest.mark.parametrize(
    "privacy_state,expected_error",
    [
        ("provider-none", "reasoning_provider_required"),
        ("consent-missing", "egress_consent_required"),
        ("offline", "offline_network_disabled"),
    ],
)
def test_reasoning_routes_reject_before_clip_job_admission(
    client, monkeypatch, endpoint, payload, privacy_state, expected_error,
):
    import clip_runner as cr

    app, c = client
    _seed_source(app)
    provider_calls = []
    monkeypatch.setattr(
        cr.moments,
        "find_moments",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)) or [],
    )

    if privacy_state == "provider-none":
        assert c.patch("/api/v1/settings", json={"reasoning_provider": "none"}).status_code == 200
    elif privacy_state == "consent-missing":
        assert c.patch("/api/v1/settings", json={"reasoning_provider": "none"}).status_code == 200
        assert c.patch("/api/v1/settings", json={"reasoning_provider": "codex"}).status_code == 200
    else:
        assert c.patch("/api/v1/settings", json={"offline": True}).status_code == 200

    manager = app.extensions["trove.clips"]
    before = len(manager.snapshot_jobs())
    response = c.post(endpoint, json=payload)

    assert response.status_code == 409
    assert response.get_json()["error"] == expected_error
    assert len(manager.snapshot_jobs()) == before
    assert provider_calls == []


def test_queued_reasoning_rechecks_consent_before_provider_execution(client, monkeypatch):
    import clip_runner as cr
    from clip import llm

    app, c = client
    _seed_source(app)
    entered = threading.Event()
    release = threading.Event()
    provider_calls = []

    def delayed_reasoning(*_args, **_kwargs):
        entered.set()
        assert release.wait(2), "reasoning worker was not released"
        llm.complete("transcript text", provider="codex")
        return []

    monkeypatch.setattr(cr.moments, "find_moments", delayed_reasoning)
    monkeypatch.setattr(
        llm.CodexProvider,
        "complete",
        lambda self, prompt, system=None: provider_calls.append(prompt) or "[]",
    )

    response = c.post("/api/v1/sources/src1/moments", json={"mode": "funny"})
    assert response.status_code == 201
    job_id = response.get_json()["id"]
    assert entered.wait(2), "reasoning worker never reached the barrier"

    revoked = c.patch("/api/v1/settings", json={"reasoning_egress_consent": False})
    assert revoked.status_code == 200
    release.set()

    failed = _await(c, job_id, status="error")
    assert failed["error_category"] == "egress_consent_required"
    assert provider_calls == []


# ---- cut -------------------------------------------------------------

def test_cut_creates_clip(client):
    app, c = client
    _seed_source(app)
    r = c.post("/api/v1/sources/src1/cut", json={"start": 2.0, "end": 12.0})
    assert r.status_code == 201 and r.get_json()["kind"] == "cut"
    done = _await(c, r.get_json()["id"])
    assert done["clip_id"] and done["result"]["clip_path"].endswith("clip.mp4")
    # the Clip record landed on disk
    cr = app.extensions["trove.clip_runner"]
    assert cr.load_clip_meta(done["clip_id"])["start"] == 2.0


def test_attempt_staging_cancelled_cut_never_replaces_published_clip(
    client, monkeypatch,
):
    import clip_runner as cr_module

    app, c = client
    _seed_source(app)
    entered = threading.Event()
    release = threading.Event()
    observed = {}

    def blocked_cut(_src, _start, _end, out, **_kwargs):
        observed["out"] = out
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"candidate")
        entered.set()
        release.wait(5)
        return out

    monkeypatch.setattr(cr_module.cutter, "cut", blocked_cut)
    response = c.post("/api/v1/sources/src1/cut", json={"start": 2.0, "end": 12.0})
    body = response.get_json()
    assert entered.wait(2)
    assert "/.attempts/clip/" in observed["out"]
    assert c.post(f"/api/v1/clip-jobs/{body['id']}/cancel").status_code == 200
    release.set()

    manager = app.extensions["trove.clips"]
    deadline = time.time() + 5
    while manager.get(body["id"])._worker_active and time.time() < deadline:
        time.sleep(0.01)
    job = manager.get(body["id"])
    assert job.status.value == "cancelled"
    assert job.result == {}
    published = app.extensions["trove.clip_runner"].clip_dir(body["clip_id"])
    assert not (published / "meta.json").exists()
    assert not (published / "clip.mp4").exists()


def test_attempt_staging_pipeline_promotes_final_paths_and_removes_private_tree(client):
    app, c = client
    _seed_source(app)
    response = c.post(
        "/api/v1/sources/src1/render",
        json={"start": 1.0, "end": 9.0, "aspect": "9:16", "style": "opus"},
    )
    done = _await(c, response.get_json()["id"])

    assert "/.attempts/" not in done["result"]["output_path"]
    assert Path(done["result"]["output_path"]).read_bytes() == b"O"
    private = app.extensions["trove.download_dir"] / ".attempts" / "clip" / done["id"]
    assert not private.exists()


@pytest.mark.parametrize("body", [{"start": 5, "end": 5}, {"start": 9, "end": 3}, {"end": 3}, {"start": -1, "end": 4}])
def test_cut_400_on_bad_range(client, body):
    app, c = client
    _seed_source(app)
    assert c.post("/api/v1/sources/src1/cut", json=body).status_code == 400


# ---- reframe ---------------------------------------------------------

def test_reframe_creates_job(client):
    app, c = client
    _seed_source(app)
    _seed_clip(app)
    r = c.post("/api/v1/clips/clipA/reframe", json={"aspect": "9:16", "mode": "pan"})
    assert r.status_code == 201 and r.get_json()["kind"] == "reframe"
    done = _await(c, r.get_json()["id"])
    assert done["result"]["reframed_path"].endswith("reframed.mp4")
    assert done["result"]["aspect"] == "9:16"


def test_reframe_404_unknown_clip(client):
    _, c = client
    assert c.post("/api/v1/clips/ghost/reframe", json={}).status_code == 404


def test_reframe_400_bad_aspect(client):
    app, c = client
    _seed_clip(app)
    assert c.post("/api/v1/clips/clipA/reframe", json={"aspect": "3:2"}).status_code == 400


def test_reframe_accepts_tuning_and_fractional_rois(client):
    """S7 sends the knobs (min-dwell/smoothing/crop-margin), fractional ROIs, and an
    optionally edited speaker track — all reach the job params."""
    app, c = client
    _seed_source(app)
    _seed_clip(app)
    body = {"aspect": "9:16", "mode": "pan", "min_dwell": 1.5, "smoothing": 21, "crop_margin": 0.15,
            "rois": {"left": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
                     "right": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}},
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "left"}]}
    r = c.post("/api/v1/clips/clipA/reframe", json=body)
    assert r.status_code == 201
    p = c.get(f"/api/v1/clip-jobs/{r.get_json()['id']}").get_json()["params"]
    assert p["min_dwell"] == 1.5 and p["smoothing"] == 21 and p["crop_margin"] == 0.15
    assert p["rois"]["left"]["w"] == 0.5
    assert p["segments"][0]["speaker"] == "left"
    _await(c, r.get_json()["id"])


@pytest.mark.parametrize("body", [
    {"segments": "nope"},
    {"segments": [{"start": 0.0, "end": 1.0, "speaker": "middle"}]},
    {"crop_margin": "x"},
    {"smoothing": "lots"},
])
def test_reframe_400_on_bad_tuning(client, body):
    app, c = client
    _seed_clip(app)
    assert c.post("/api/v1/clips/clipA/reframe", json=body).status_code == 400


def test_clip_artifact_serves_intermediate_files(client):
    """The editor previews (S6/S7/S8) stream the clip's intermediate mp4s by name."""
    app, c = client
    _seed_clip(app, files=("clip.mp4", "reframed.mp4"))
    assert c.get("/api/v1/clips/clipA/artifacts/clip").status_code == 200
    assert c.get("/api/v1/clips/clipA/artifacts/reframed").status_code == 200


def test_clip_artifact_404_missing_and_400_invalid(client):
    app, c = client
    _seed_clip(app, files=("clip.mp4",))
    assert c.get("/api/v1/clips/clipA/artifacts/captioned").status_code == 404  # not produced yet
    assert c.get("/api/v1/clips/clipA/artifacts/bogus").status_code == 400       # not a known artifact


# ---- captions --------------------------------------------------------

def test_caption_creates_job(client):
    app, c = client
    _seed_source(app)
    _seed_clip(app, files=("clip.mp4", "reframed.mp4"))
    r = c.post("/api/v1/clips/clipA/captions", json={"style": "karaoke"})
    assert r.status_code == 201
    done = _await(c, r.get_json()["id"])
    assert done["result"]["captioned_path"].endswith("captioned.mp4")
    assert done["result"]["style"] == "karaoke"


def test_caption_400_bad_style(client):
    app, c = client
    _seed_source(app)
    _seed_clip(app)
    assert c.post("/api/v1/clips/clipA/captions", json={"style": "neon"}).status_code == 400


def test_caption_accepts_style_overrides(client):
    """S8 sends fine-styling overrides; they reach the caption job params (clamped/validated)."""
    app, c = client
    _seed_source(app)
    _seed_clip(app, files=("clip.mp4",))
    body = {"style": "opus", "overrides": {"size": 90, "outline": 6, "fill": "#ffffff",
                                           "highlight": "#FFE94D", "position": 30, "words": 4, "allcaps": True}}
    r = c.post("/api/v1/clips/clipA/captions", json=body)
    assert r.status_code == 201
    p = c.get(f"/api/v1/clip-jobs/{r.get_json()['id']}").get_json()["params"]
    assert p["overrides"]["size"] == 90 and p["overrides"]["fill"] == "#ffffff"
    assert p["overrides"]["words"] == 4 and p["overrides"]["allcaps"] is True
    _await(c, r.get_json()["id"])


@pytest.mark.parametrize("body", [
    {"overrides": "nope"},
    {"overrides": {"size": "huge"}},
    {"overrides": {"fill": "nothex"}},
])
def test_caption_400_on_bad_overrides(client, body):
    app, c = client
    _seed_source(app)
    _seed_clip(app, files=("clip.mp4",))
    assert c.post("/api/v1/clips/clipA/captions", json=body).status_code == 400


def test_caption_accepts_watermark(client):
    """Applying a brand kit caption-burns its watermark + lower-third (S9)."""
    app, c = client
    _seed_source(app)
    _seed_clip(app, files=("clip.mp4",))
    r = c.post("/api/v1/clips/clipA/captions", json={"style": "opus", "watermark": "@acme", "lower_third": "Ep. 42"})
    assert r.status_code == 201
    p = c.get(f"/api/v1/clip-jobs/{r.get_json()['id']}").get_json()["params"]
    assert p["watermark"] == "@acme" and p["lower_third"] == "Ep. 42"
    _await(c, r.get_json()["id"])


# ---- brand kits (S9) -------------------------------------------------

def test_brand_kits_crud(client):
    app, c = client
    assert c.get("/api/v1/brand-kits").get_json()["brand_kits"] == []
    r = c.post("/api/v1/brand-kits", json={"name": "Acme", "caption_preset": "opus",
                                           "caption_overrides": {"highlight": "#FFE94D", "size": 110},
                                           "watermark": "@acme", "palette": ["#45556E", "#C98A3D"]})
    assert r.status_code == 201
    kid = r.get_json()["id"]
    listed = c.get("/api/v1/brand-kits").get_json()["brand_kits"]
    assert len(listed) == 1 and listed[0]["name"] == "Acme" and listed[0]["caption_overrides"]["size"] == 110
    u = c.patch(f"/api/v1/brand-kits/{kid}", json={"name": "Acme Media"})
    assert u.status_code == 200 and u.get_json()["name"] == "Acme Media" and u.get_json()["watermark"] == "@acme"
    assert c.delete(f"/api/v1/brand-kits/{kid}").status_code == 204
    assert c.get("/api/v1/brand-kits").get_json()["brand_kits"] == []


@pytest.mark.parametrize("body", [{"caption_preset": "opus"}, {"name": "X", "caption_preset": "neon"},
                                  {"name": "X", "caption_overrides": {"size": "huge"}}])
def test_brand_kit_create_400(client, body):
    _, c = client
    assert c.post("/api/v1/brand-kits", json=body).status_code == 400


def test_brand_kit_update_404(client):
    _, c = client
    assert c.patch("/api/v1/brand-kits/ghost", json={"name": "x"}).status_code == 404


def test_caption_override_font_is_sanitized(client):
    _, c = client
    r = c.post("/api/v1/brand-kits", json={
        "name": "k", "caption_overrides": {"font": "Bad,Font{\\evil}\x01Name"}})
    assert r.status_code == 201
    font = r.get_json()["caption_overrides"]["font"]
    for ch in (",", "{", "}", "\\", "\x01"):
        assert ch not in font
    assert "BadFont" in font


# ---- renders (export) ------------------------------------------------

def test_render_export_creates_job_and_file(client):
    app, c = client
    _seed_source(app)
    _seed_clip(app, files=("clip.mp4", "reframed.mp4", "captioned.mp4"))
    r = c.post("/api/v1/clips/clipA/renders", json={"preset": "reels", "fast": False})
    assert r.status_code == 201
    done = _await(c, r.get_json()["id"])
    rid = done["result"]["render_id"]
    assert done["result"]["output_path"].endswith(f"{rid}.mp4")
    # the produced file is downloadable
    fr = c.get(f"/api/v1/clips/clipA/renders/{rid}/file")
    assert fr.status_code == 200 and fr.data == b"O"


def test_render_export_400_bad_preset(client):
    app, c = client
    _seed_clip(app)
    assert c.post("/api/v1/clips/clipA/renders", json={"preset": "myspace"}).status_code == 400


def test_render_file_404_when_missing(client):
    _, c = client
    assert c.get("/api/v1/clips/clipA/renders/nope/file").status_code == 404


# ---- pipeline (one-shot) --------------------------------------------

def test_render_pipeline_runs_full_chain(client):
    app, c = client
    _seed_source(app)
    r = c.post("/api/v1/sources/src1/render",
               json={"start": 1.0, "end": 9.0, "aspect": "9:16", "style": "opus", "preset": "tiktok"})
    assert r.status_code == 201 and r.get_json()["kind"] == "pipeline"
    done = _await(c, r.get_json()["id"])
    assert done["clip_id"] and done["result"]["render_id"]
    assert done["result"]["output_path"].endswith(".mp4")
    rid = done["result"]["render_id"]
    assert c.get(f"/api/v1/clips/{done['clip_id']}/renders/{rid}/file").status_code == 200


def test_render_pipeline_409_without_transcript(client):
    app, c = client
    _seed_source(app, with_transcript=False)
    r = c.post("/api/v1/sources/src1/render", json={"start": 1.0, "end": 9.0})
    assert r.status_code == 409


# ---- queue: list / get / cancel / dismiss ---------------------------

def test_list_clip_jobs_filter_by_kind(client):
    app, c = client
    _seed_source(app)
    c.post("/api/v1/sources/src1/moments", json={})
    cut = c.post("/api/v1/sources/src1/cut", json={"start": 1, "end": 5}).get_json()
    _await(c, cut["id"])
    r = c.get("/api/v1/clip-jobs?kind=cut")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 1 and body["clip_jobs"][0]["kind"] == "cut"


def test_get_clip_job_404(client):
    _, c = client
    assert c.get("/api/v1/clip-jobs/nope").status_code == 404


def test_dismiss_clip_job(client):
    app, c = client
    _seed_source(app)
    j = c.post("/api/v1/sources/src1/cut", json={"start": 1, "end": 5}).get_json()
    _await(c, j["id"])
    assert c.post(f"/api/v1/clip-jobs/{j['id']}/dismiss").status_code == 204
    direct = c.get(f"/api/v1/clip-jobs/{j['id']}")
    assert direct.status_code == 200
    assert direct.get_json()["dismissed"] is True
    assert direct.get_json()["dismissed_at"] is not None
    listed = c.get("/api/v1/clip-jobs").get_json()["clip_jobs"]
    assert any(row["id"] == j["id"] and row["dismissed"] is True for row in listed)


# ---- discovery surfaces ---------------------------------------------

def test_capabilities_advertises_clips(client):
    _, c = client
    body = c.get("/api/v1/capabilities").get_json()
    assert body["features"]["clips"] is True
    assert set(body["formats"]["clip_aspects"]) == {"9:16", "16:9", "1:1", "4:5"}


def test_events_snapshot_includes_clips(client):
    app, c = client
    _seed_source(app)
    j = c.post("/api/v1/sources/src1/cut", json={"start": 1, "end": 5}).get_json()
    _await(c, j["id"])
    r = c.get("/api/v1/events?max_events=1&interval=0.05")
    payload = r.get_data(as_text=True)
    assert '"clips"' in payload and j["id"] in payload
