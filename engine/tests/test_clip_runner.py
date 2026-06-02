"""Tests for clip_runner — the orchestration that wires the clip/ engine modules to
the on-disk clip tree + a ClipJob's cancel/progress hooks.

The engine functions (cutter/reframe/captioner/exporter/moments) are already covered by
their own suites, so here they're mocked: we assert the *orchestration* — which paths
are produced, the meta.json "Clip record", diarization extraction from the transcript,
artifact chaining (cut→reframe→caption→export), pipeline staging, and cancel handling.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import clip_runner as cr
from clip_jobs import ClipJob


class _FakeJM:
    def __init__(self, sources):
        self._s = sources  # {source_id: file_path}

    def get(self, sid):
        fp = self._s.get(sid)
        return SimpleNamespace(id=sid, file_path=fp) if fp else None


class _FakeCM:
    def __init__(self):
        self.progress = []

    def update_progress(self, jid, pct, *, stage=None):
        self.progress.append((pct, stage))


@pytest.fixture()
def runner(tmp_path):
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "src1.mp4").write_bytes(b"MEDIA")
    jm = _FakeJM({"src1": str(dl / "src1.mp4")})
    return cr.ClipRunner(download_dir=dl, job_manager=jm, clip_manager=_FakeCM())


def _words_file(runner, source_id="src1", *, with_speakers=True):
    seg = {"start": 0.0, "end": 10.0, "text": "hi", "word_idxs": [], "speaker": "SPEAKER_00" if with_speakers else None}
    data = {"schema_version": 2, "language": "en", "duration": 10.0, "edited_at": None,
            "words": [], "segments": [seg], "bookmarks": []}
    p = runner.download_dir / f"{source_id}.words.json"
    p.write_text(json.dumps(data))
    return str(p)


def _job(kind, **kw):
    return ClipJob(id="j1", kind=kind, **kw)


def _patch(monkeypatch, mod, name, fn):
    monkeypatch.setattr(getattr(cr, mod), name, fn)


# ---- cut -------------------------------------------------------------

def test_cut_target_writes_clip_and_meta_record(runner, monkeypatch):
    calls = {}

    def fake_cut(src, start, end, out, **kw):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"CLIP")
        calls.update(src=src, start=start, end=end, out=out, kw=kw)
        return out

    _patch(monkeypatch, "cutter", "cut", fake_cut)
    job = _job("cut", source_id="src1")
    runner.cut_target(source_id="src1", clip_id="clipA", params={"start": 2.0, "end": 12.0})(job)

    cdir = runner.clip_dir("clipA")
    assert calls["src"].endswith("src1.mp4")
    assert calls["start"] == 2.0 and calls["end"] == 12.0
    assert calls["out"] == str(cdir / "clip.mp4")
    # cancel/progress wiring is handed to ffmpeg
    assert callable(calls["kw"]["cancel_check"]) and callable(calls["kw"]["register_proc"])
    # job result + Clip record on disk
    assert job.clip_id == "clipA"
    assert job.result["clip_path"] == str(cdir / "clip.mp4")
    meta = json.loads((cdir / "meta.json").read_text())
    assert meta["source_id"] == "src1" and meta["start"] == 2.0 and meta["end"] == 12.0


def test_cut_target_unknown_source_raises(runner):
    job = _job("cut", source_id="ghost")
    with pytest.raises(ValueError, match="source"):
        runner.cut_target(source_id="ghost", clip_id="c", params={"start": 0, "end": 1})(job)


# ---- moments ---------------------------------------------------------

def test_find_moments_target_records_candidates(runner, monkeypatch):
    _words_file(runner)
    seen = {}

    def fake_find(words_path, **kw):
        seen.update(words_path=words_path, kw=kw)
        return [{"start": 1.0, "end": 9.0, "title": "x", "rationale": "y", "mode": kw["mode"], "signals": []}]

    _patch(monkeypatch, "moments", "find_moments", fake_find)
    job = _job("moments", source_id="src1")
    runner.find_moments_target(source_id="src1", params={"mode": "insightful", "count": 3})(job)

    assert seen["words_path"].endswith("src1.words.json")
    assert seen["kw"]["mode"] == "insightful" and seen["kw"]["count"] == 3
    assert seen["kw"]["source_id"] == "src1"
    assert job.result["count"] == 1 and job.result["candidates"][0]["title"] == "x"


# ---- reframe ---------------------------------------------------------

def _seed_clip(runner, clip_id="clipA", source_id="src1", *, start=2.0, end=12.0, files=("clip.mp4",)):
    d = runner.clip_dir(clip_id)
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_bytes(b"V")
    (d / "meta.json").write_text(json.dumps(
        {"clip_id": clip_id, "source_id": source_id, "start": start, "end": end}))
    return d


def test_reframe_target_detects_rois_fuses_diar_and_renders(runner, monkeypatch):
    _words_file(runner, with_speakers=True)
    d = _seed_clip(runner)
    seen = {}

    def fake_detect(clip_path, **kw):
        seen["detect"] = clip_path
        return {"width": 1920, "height": 1080, "frame_path": kw.get("frame_path"),
                "rois": {"left": {"x": 0, "y": 0, "w": 960, "h": 1080},
                         "right": {"x": 960, "y": 0, "w": 960, "h": 1080}}}

    def fake_track(clip_path, **kw):
        seen["track_kw"] = kw
        return {"segments": [{"start": 0.0, "end": 10.0, "speaker": "left"}],
                "roiL": kw["roi_left"], "roiR": kw["roi_right"], "source": "fused"}

    def fake_render(clip_path, track, **kw):
        Path(kw["out_path"]).write_bytes(b"R")
        seen["render_kw"] = kw
        return kw["out_path"]

    _patch(monkeypatch, "reframe", "detect_faces", fake_detect)
    _patch(monkeypatch, "reframe", "speaker_track", fake_track)
    _patch(monkeypatch, "reframe", "render", fake_render)

    job = _job("reframe", clip_id="clipA")
    runner.reframe_target(clip_id="clipA", params={"aspect": "9:16", "mode": "pan"})(job)

    assert seen["detect"] == str(d / "clip.mp4")
    # diarization turns were pulled from the transcript's speaker'd segments
    assert seen["track_kw"]["diarization"] and seen["track_kw"]["diarization"][0]["speaker"] == "SPEAKER_00"
    assert seen["render_kw"]["aspect"] == "9:16" and seen["render_kw"]["mode"] == "pan"
    assert seen["render_kw"]["out_path"] == str(d / "reframed.mp4")
    assert json.loads((d / "track.json").read_text())["source"] == "fused"
    assert job.result["reframed_path"] == str(d / "reframed.mp4")
    assert job.source_id == "src1"  # filled from the Clip record for queue grouping


def test_reframe_target_uses_confirmed_rois_without_detection(runner, monkeypatch):
    _words_file(runner, with_speakers=False)
    _seed_clip(runner)
    detected = []
    _patch(monkeypatch, "reframe", "detect_faces", lambda *a, **k: detected.append(1))
    _patch(monkeypatch, "reframe", "speaker_track", lambda c, **kw: {"segments": [], "roiL": kw["roi_left"], "roiR": kw["roi_right"], "source": "roi"})
    _patch(monkeypatch, "reframe", "render", lambda c, t, **kw: Path(kw["out_path"]).write_bytes(b"R"))

    rois = {"left": {"x": 1, "y": 2, "w": 3, "h": 4}, "right": {"x": 5, "y": 6, "w": 7, "h": 8}}
    job = _job("reframe", clip_id="clipA")
    runner.reframe_target(clip_id="clipA", params={"rois": rois})(job)
    assert detected == []  # confirmed ROIs → no auto-detect


# ---- caption ---------------------------------------------------------

def test_caption_target_slices_window_and_burns_onto_reframed(runner, monkeypatch):
    _words_file(runner)
    d = _seed_clip(runner, files=("clip.mp4", "reframed.mp4"), start=2.0, end=12.0)
    seen = {}

    def fake_generate(words_path, **kw):
        seen["gen"] = kw
        Path(kw["out_ass_path"]).write_text("[ass]")
        return kw["out_ass_path"]

    def fake_burn(video_in, ass, out, **kw):
        seen["burn"] = (video_in, ass, out)
        Path(out).write_bytes(b"C")
        return out

    _patch(monkeypatch, "captioner", "generate", fake_generate)
    _patch(monkeypatch, "captioner", "burn", fake_burn)

    job = _job("caption", clip_id="clipA")
    runner.caption_target(clip_id="clipA", params={"style": "karaoke"})(job)

    assert seen["gen"]["clip_start"] == 2.0 and seen["gen"]["clip_end"] == 12.0
    assert seen["gen"]["style"] == "karaoke"
    assert seen["gen"]["out_ass_path"] == str(d / "captions.ass")
    # burns onto the reframed video (preferred over the raw cut), → captioned.mp4
    assert seen["burn"][0] == str(d / "reframed.mp4")
    assert seen["burn"][2] == str(d / "captioned.mp4")
    assert job.result["captioned_path"] == str(d / "captioned.mp4")


# ---- export ----------------------------------------------------------

def test_export_target_prefers_captioned_and_writes_render(runner, monkeypatch):
    _seed_clip(runner, files=("clip.mp4", "reframed.mp4", "captioned.mp4"))
    seen = {}

    def fake_export(clip_path, **kw):
        seen.update(clip_path=clip_path, kw=kw)
        Path(kw["out_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["out_path"]).write_bytes(b"OUT")
        return kw["out_path"]

    _patch(monkeypatch, "exporter", "export", fake_export)
    job = _job("export", clip_id="clipA")
    runner.export_target(clip_id="clipA", render_id="rend1", params={"preset": "reels", "fast": False})(job)

    d = runner.clip_dir("clipA")
    assert seen["clip_path"] == str(d / "captioned.mp4")  # latest artifact in the chain
    assert seen["kw"]["preset"] == "reels" and seen["kw"]["fast"] is False
    assert seen["kw"]["out_path"] == str(d / "renders" / "rend1.mp4")
    assert job.result["render_id"] == "rend1"
    assert job.result["output_path"] == str(d / "renders" / "rend1.mp4")


def test_export_target_falls_back_to_cut_when_no_caption(runner, monkeypatch):
    _seed_clip(runner, files=("clip.mp4",))
    seen = {}
    _patch(monkeypatch, "exporter", "export",
           lambda clip_path, **kw: (seen.update(clip_path=clip_path), Path(kw["out_path"]).parent.mkdir(parents=True, exist_ok=True), Path(kw["out_path"]).write_bytes(b"O"), kw["out_path"])[-1])
    job = _job("export", clip_id="clipA")
    runner.export_target(clip_id="clipA", render_id="r", params={})(job)
    assert seen["clip_path"] == str(runner.clip_dir("clipA") / "clip.mp4")


# ---- pipeline --------------------------------------------------------

def test_pipeline_chains_every_stage_with_progress(runner, monkeypatch):
    _words_file(runner)
    order = []
    monkeypatch.setattr(cr.cutter, "cut", lambda s, a, b, out, **k: (order.append("cut"), Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"C"), out)[-1])
    monkeypatch.setattr(cr.reframe, "detect_faces", lambda c, **k: {"rois": {"left": {"x": 0, "y": 0, "w": 1, "h": 1}, "right": {"x": 1, "y": 0, "w": 1, "h": 1}}, "width": 2, "height": 1, "frame_path": k.get("frame_path")})
    monkeypatch.setattr(cr.reframe, "speaker_track", lambda c, **k: (order.append("track"), {"segments": [], "roiL": k["roi_left"], "roiR": k["roi_right"], "source": "roi"})[-1])
    monkeypatch.setattr(cr.reframe, "render", lambda c, t, **k: (order.append("reframe"), Path(k["out_path"]).write_bytes(b"R"), k["out_path"])[-1])
    monkeypatch.setattr(cr.captioner, "generate", lambda w, **k: (order.append("caption.gen"), Path(k["out_ass_path"]).write_text("a"), k["out_ass_path"])[-1])
    monkeypatch.setattr(cr.captioner, "burn", lambda v, a, out, **k: (order.append("caption.burn"), Path(out).write_bytes(b"X"), out)[-1])
    monkeypatch.setattr(cr.exporter, "export", lambda c, **k: (order.append("export"), Path(k["out_path"]).parent.mkdir(parents=True, exist_ok=True), Path(k["out_path"]).write_bytes(b"O"), k["out_path"])[-1])

    job = _job("pipeline", source_id="src1")
    runner.pipeline_target(
        source_id="src1", clip_id="clipP", render_id="rendP",
        params={"start": 1.0, "end": 9.0, "aspect": "9:16", "mode": "pan",
                "style": "opus", "preset": "tiktok"},
    )(job)

    assert order == ["cut", "track", "reframe", "caption.gen", "caption.burn", "export"]
    stages = [s for _, s in runner.clip_manager.progress if s]
    assert stages == ["cut", "reframe", "caption", "export"]
    d = runner.clip_dir("clipP")
    assert job.clip_id == "clipP" and job.result["render_id"] == "rendP"
    assert job.result["output_path"] == str(d / "renders" / "rendP.mp4")
    assert (d / "meta.json").exists()


# ---- cancellation ----------------------------------------------------

def test_target_swallows_cancellation_cleanly(runner, monkeypatch):
    def cancelled_cut(*a, **k):
        raise RuntimeError("cancelled")

    _patch(monkeypatch, "cutter", "cut", cancelled_cut)
    job = _job("cut", source_id="src1")
    job._cancel_flag = True
    # A cancelled ffmpeg must not surface as a job error — return cleanly.
    runner.cut_target(source_id="src1", clip_id="c", params={"start": 0, "end": 1})(job)
    assert job.result == {} and job.error_message is None
