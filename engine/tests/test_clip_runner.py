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
        self.submitted = []   # records produce_target's fan-out (no pipeline actually runs)

    def update_progress(self, jid, pct, *, stage=None):
        self.progress.append((pct, stage))

    def submit(self, *, kind, target, source_id=None, clip_id=None, params=None):
        self.submitted.append({"kind": kind, "source_id": source_id, "clip_id": clip_id, "params": params or {}})
        return f"job{len(self.submitted)}"


class _FakeSettings:
    """Stand-in for settings.SettingsStore — only .get() is read by the runner."""
    def __init__(self, **vals):
        self._v = vals

    def get(self):
        return dict(self._v)


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


def test_find_moments_target_ranks_candidates(runner, monkeypatch):
    # Candidates arrive pre-ranked with the glass-box score so Discovery shows a real score
    # immediately (closing the Phase-1 honesty boundary). rank scores on the signals annotate
    # attached; here the hooky moment must outrank the flat one.
    _words_file(runner)

    def fake_find(words_path, **kw):
        return [
            {"start": 0.0, "end": 18.0, "title": "flat", "rationale": "", "mode": kw["mode"], "signals": []},
            {"start": 20.0, "end": 38.0, "title": "hooky", "rationale": "", "mode": kw["mode"],
             "signals": ["hook", "punchline"]},
        ]

    _patch(monkeypatch, "moments", "find_moments", fake_find)
    job = _job("moments", source_id="src1")
    runner.find_moments_target(source_id="src1", params={"mode": "funny", "count": 5})(job)

    cands = job.result["candidates"]
    assert all({"score", "factors", "weights"} <= set(c) for c in cands)
    assert set(cands[0]["factors"]) == {"hook", "self_contained", "arc", "energy", "length_fit"}
    assert cands[0]["title"] == "hooky"                  # sorted best-first
    assert cands[0]["score"] >= cands[1]["score"]
    assert job.result["weights"]["hook"] > 0             # effective weights logged (no silent magic)


def test_find_moments_target_honors_weight_overrides(runner, monkeypatch):
    _words_file(runner)

    def fake_find(words_path, **kw):
        return [{"start": 0.0, "end": 18.0, "title": "x", "rationale": "", "mode": kw["mode"], "signals": []}]

    _patch(monkeypatch, "moments", "find_moments", fake_find)
    job = _job("moments", source_id="src1")
    runner.find_moments_target(
        source_id="src1", params={"mode": "funny", "count": 5, "weights": {"energy": 1}})(job)

    w = job.result["candidates"][0]["weights"]
    assert w["energy"] == 1.0 and w["hook"] == 0.0       # the override propagated into rank


def test_produce_target_fans_out_ranked_pipelines(runner, monkeypatch):
    # The watch-folder keystone: apply a recipe end-to-end — find_moments → rank(recipe.weights)
    # → take the top `count` → submit a full render pipeline per moment with the recipe's settings.
    _words_file(runner)

    def fake_find(words_path, **kw):
        return [{"start": float(i * 20), "end": float(i * 20 + 18), "title": f"m{i}", "rationale": "",
                 "mode": kw["mode"], "signals": (["hook", "punchline"] if i == 2 else [])} for i in range(4)]

    _patch(monkeypatch, "moments", "find_moments", fake_find)
    recipe = {"id": "r1", "content_mode": "funny", "count": 2, "aspect": "1:1", "reframe_mode": "center",
              "caption_preset": "minimal", "platform": "reels", "fast": True, "weights": {"hook": 5}}
    job = _job("produce", source_id="src1")
    runner.produce_target(source_id="src1", recipe=recipe)(job)

    subs = runner.clip_manager.submitted
    assert len(subs) == 2 and all(s["kind"] == "pipeline" for s in subs)   # top `count`=2 pipelines
    p0 = subs[0]["params"]
    assert (p0["aspect"], p0["mode"], p0["style"], p0["preset"]) == ("1:1", "center", "minimal", "reels")
    assert p0["fast"] is True and p0["recipe_id"] == "r1" and p0["auto"] is True   # recipe + provenance
    assert subs[0]["params"]["start"] == 40.0                              # hooky m2 ranked first (hook weight)
    assert job.result["count"] == 2 and len(job.result["clip_jobs"]) == 2 and job.result["recipe_id"] == "r1"


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
    _patch(monkeypatch, "reframe", "probe_dimensions", lambda p: (1920, 1080))
    _patch(monkeypatch, "reframe", "detect_faces", lambda *a, **k: detected.append(1))
    _patch(monkeypatch, "reframe", "speaker_track", lambda c, **kw: {"segments": [], "roiL": kw["roi_left"], "roiR": kw["roi_right"], "source": "roi"})
    _patch(monkeypatch, "reframe", "render", lambda c, t, **kw: Path(kw["out_path"]).write_bytes(b"R"))

    rois = {"left": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}, "right": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}}
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


def test_kept_spans_excludes_deleted_word_ranges(runner):
    """The transcript-driven cut keeps everything except deleted words' time ranges."""
    data = {"words": [
        {"idx": 0, "w": "a", "start": 3.0, "end": 3.5, "deleted": False},
        {"idx": 1, "w": "b", "start": 5.0, "end": 6.0, "deleted": True},
        {"idx": 2, "w": "c", "start": 7.0, "end": 7.5, "deleted": False}], "segments": []}
    p = runner.download_dir / "src1.words.json"
    p.write_text(json.dumps(data))
    assert cr._kept_spans(str(p), 2.0, 12.0) == [(2.0, 5.0), (6.0, 12.0)]


def test_kept_spans_single_when_no_deletions_or_no_file(runner):
    p = runner.download_dir / "ok.words.json"
    p.write_text(json.dumps({"words": [{"idx": 0, "w": "a", "start": 3.0, "end": 4.0, "deleted": False}], "segments": []}))
    assert cr._kept_spans(str(p), 2.0, 12.0) == [(2.0, 12.0)]
    assert cr._kept_spans(None, 2.0, 12.0) == [(2.0, 12.0)]              # no transcript → single span


def test_cut_ripple_cuts_when_words_deleted(runner, monkeypatch):
    """Editing the transcript (deleting words) → the clip is cut to drop those spans."""
    data = {"words": [
        {"idx": 0, "w": "keep", "start": 3.0, "end": 3.5, "deleted": False},
        {"idx": 1, "w": "cut", "start": 5.0, "end": 6.0, "deleted": True}], "segments": []}
    (runner.download_dir / "src1.words.json").write_text(json.dumps(data))
    seen = {}
    _patch(monkeypatch, "cutter", "cut", lambda *a, **k: pytest.fail("deletions present → must ripple-cut"))
    _patch(monkeypatch, "cutter", "cut_spans",
           lambda s, spans, out, **k: (seen.update(spans=spans), Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"C"))[-1] or out)
    runner.cut_target(source_id="src1", clip_id="clipR", params={"start": 2.0, "end": 12.0})(_job("cut", source_id="src1"))
    assert seen["spans"] == [(2.0, 5.0), (6.0, 12.0)]


def test_cut_single_range_when_no_deletions(runner, monkeypatch):
    _words_file(runner)  # words list is empty → no deletions
    seen = {}
    _patch(monkeypatch, "cutter", "cut",
           lambda s, a, b, out, **k: (seen.update(rng=(a, b)), Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"C"))[-1] or out)
    _patch(monkeypatch, "cutter", "cut_spans", lambda *a, **k: pytest.fail("no deletions → single-range cut, not a ripple concat"))
    runner.cut_target(source_id="src1", clip_id="clipS", params={"start": 2.0, "end": 12.0})(_job("cut", source_id="src1"))
    assert seen["rng"] == (2.0, 12.0)


def test_caption_forwards_style_overrides(runner, monkeypatch):
    """S8 fine-styling overrides reach captioner.generate (mapped to the ASS there)."""
    _words_file(runner)
    _seed_clip(runner, files=("clip.mp4",))
    seen = {}

    def fake_generate(words_path, **kw):
        seen["gen"] = kw
        Path(kw["out_ass_path"]).write_text("[ass]")
        return kw["out_ass_path"]

    _patch(monkeypatch, "captioner", "generate", fake_generate)
    _patch(monkeypatch, "captioner", "burn", lambda v, a, out, **kw: (Path(out).write_bytes(b"C"), out)[-1])
    ov = {"size": 90, "fill": "#ffffff", "highlight": "#FFE94D", "position": 30, "words": 4, "allcaps": True}
    runner.caption_target(clip_id="clipA", params={"style": "opus", "overrides": ov})(_job("caption", clip_id="clipA"))
    assert seen["gen"]["overrides"] == ov


def test_caption_forwards_brandkit_watermark(runner, monkeypatch):
    """Applying a brand kit caption-burns its watermark + lower-third (S9)."""
    _words_file(runner)
    _seed_clip(runner, files=("clip.mp4",))
    seen = {}
    _patch(monkeypatch, "captioner", "generate",
           lambda w, **kw: (seen.update(kw), Path(kw["out_ass_path"]).write_text("[ass]"))[-1] or kw["out_ass_path"])
    _patch(monkeypatch, "captioner", "burn", lambda v, a, out, **kw: (Path(out).write_bytes(b"C"), out)[-1])
    runner.caption_target(clip_id="clipA", params={"style": "opus", "watermark": "@acme", "lower_third": "Ep. 42"})(_job("caption", clip_id="clipA"))
    assert seen["watermark"] == "@acme" and seen["lower_third"] == "Ep. 42"


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


def test_export_reads_settings_defaults_when_params_omit_them(tmp_path, monkeypatch):
    """S14: the studio's General "output defaults" (fast/quality + preset) apply *hot* — the
    runner reads them from the settings store whenever a render leaves them unset. An explicit
    param always wins; with no store, the hardcoded fast=True/tiktok defaults still hold."""
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "src1.mp4").write_bytes(b"MEDIA")
    jm = _FakeJM({"src1": str(dl / "src1.mp4")})
    r = cr.ClipRunner(download_dir=dl, job_manager=jm, clip_manager=_FakeCM(),
                      settings_store=_FakeSettings(fast_default=False, default_preset="shorts"))
    _seed_clip(r, files=("clip.mp4", "captioned.mp4"))
    seen = {}
    _patch(monkeypatch, "exporter", "export",
           lambda cp, **kw: (seen.update(kw=kw), Path(kw["out_path"]).parent.mkdir(parents=True, exist_ok=True),
                             Path(kw["out_path"]).write_bytes(b"O"), kw["out_path"])[-1])

    # params omit fast + preset → the stored defaults are used
    r.export_target(clip_id="clipA", render_id="r1", params={})(_job("export", clip_id="clipA"))
    assert seen["kw"]["fast"] is False
    assert seen["kw"]["preset"] == "shorts"

    # explicit params override the stored defaults
    seen.clear()
    r.export_target(clip_id="clipA", render_id="r2", params={"fast": True, "preset": "reels"})(_job("export", clip_id="clipA"))
    assert seen["kw"]["fast"] is True
    assert seen["kw"]["preset"] == "reels"


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
    # the clip window rides in the result so the editor can trim its transcript timeline
    assert job.result["start"] == 1.0 and job.result["end"] == 9.0
    assert (d / "meta.json").exists()


def test_pipeline_stop_after_reframe_skips_caption_and_export(runner, monkeypatch):
    """'Make clips' cuts + reframes (auto-reframe) and STOPS — no caption/export burn. The clip
    lands ready to review (reframed.mp4 + its window) and the user renders later from the editor."""
    _words_file(runner)
    order = []
    monkeypatch.setattr(cr.cutter, "cut", lambda s, a, b, out, **k: (order.append("cut"), Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"C"), out)[-1])
    monkeypatch.setattr(cr.reframe, "detect_faces", lambda c, **k: {"rois": {"left": {"x": 0, "y": 0, "w": 1, "h": 1}, "right": {"x": 1, "y": 0, "w": 1, "h": 1}}, "width": 2, "height": 1, "frame_path": k.get("frame_path")})
    monkeypatch.setattr(cr.reframe, "speaker_track", lambda c, **k: (order.append("track"), {"segments": [], "roiL": k["roi_left"], "roiR": k["roi_right"], "source": "roi"})[-1])
    monkeypatch.setattr(cr.reframe, "render", lambda c, t, **k: (order.append("reframe"), Path(k["out_path"]).write_bytes(b"R"), k["out_path"])[-1])
    monkeypatch.setattr(cr.captioner, "generate", lambda w, **k: order.append("caption.gen"))
    monkeypatch.setattr(cr.exporter, "export", lambda c, **k: order.append("export"))

    job = _job("pipeline", source_id="src1")
    runner.pipeline_target(
        source_id="src1", clip_id="clipS", render_id="rendS",
        params={"start": 2.0, "end": 8.0, "aspect": "9:16", "mode": "pan", "stop_after": "reframe"},
    )(job)

    assert order == ["cut", "track", "reframe"]            # NO caption / export
    assert job.result["clip_id"] == "clipS"
    assert job.result["start"] == 2.0 and job.result["end"] == 8.0
    assert job.result["reframed_path"].endswith("reframed.mp4")
    assert "render_id" not in job.result and "output_path" not in job.result


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


# ---- reframe: Phase-2 tuning (fractional ROIs · knobs · edited track) ------

def test_reframe_scales_fractional_rois_to_pixels(runner, monkeypatch):
    """The studio sends resolution-independent fractional ROIs (0–1); the runner scales
    them to source pixels before the engine (which crops in pixels)."""
    _words_file(runner, with_speakers=False)
    _seed_clip(runner)
    seen = {}
    _patch(monkeypatch, "reframe", "probe_dimensions", lambda p: (1920, 1080))
    _patch(monkeypatch, "reframe", "detect_faces", lambda *a, **k: pytest.fail("rois given → no detect"))
    _patch(monkeypatch, "reframe", "speaker_track",
           lambda c, **kw: (seen.update(roi_left=kw["roi_left"], roi_right=kw["roi_right"]),
                            {"segments": [], "roiL": kw["roi_left"], "roiR": kw["roi_right"], "source": "roi"})[-1])
    _patch(monkeypatch, "reframe", "render", lambda c, t, **kw: Path(kw["out_path"]).write_bytes(b"R"))
    rois = {"left": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
            "right": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}}
    runner.reframe_target(clip_id="clipA", params={"rois": rois})(_job("reframe", clip_id="clipA"))
    assert seen["roi_left"] == {"x": 0, "y": 0, "w": 960, "h": 1080}
    assert seen["roi_right"] == {"x": 960, "y": 0, "w": 960, "h": 1080}


def test_reframe_forwards_tuning_params(runner, monkeypatch):
    """min-dwell + smoothing reach speaker_track; crop-margin reaches render (S7 knobs)."""
    _words_file(runner)
    _seed_clip(runner)
    seen = {}
    _patch(monkeypatch, "reframe", "probe_dimensions", lambda p: (1920, 1080))
    _patch(monkeypatch, "reframe", "detect_faces", lambda c, **k: {
        "width": 1920, "height": 1080, "frame_path": k.get("frame_path"),
        "rois": {"left": {"x": 0, "y": 0, "w": 960, "h": 1080},
                 "right": {"x": 960, "y": 0, "w": 960, "h": 1080}}})
    _patch(monkeypatch, "reframe", "speaker_track",
           lambda c, **kw: (seen.update(track=kw),
                            {"segments": [], "roiL": kw["roi_left"], "roiR": kw["roi_right"], "source": "fused"})[-1])
    _patch(monkeypatch, "reframe", "render",
           lambda c, t, **kw: (seen.update(render=kw), Path(kw["out_path"]).write_bytes(b"R"))[-1])
    runner.reframe_target(clip_id="clipA", params={
        "min_dwell": 2.0, "smoothing": 25, "crop_margin": 0.2, "aspect": "9:16", "mode": "pan"},
    )(_job("reframe", clip_id="clipA"))
    assert seen["track"]["min_dwell"] == 2.0 and seen["track"]["smoothing"] == 25
    assert seen["render"]["crop_margin"] == 0.2


def test_reframe_uses_edited_segments_override(runner, monkeypatch):
    """An edited speaker track (drag/flip in S7) renders verbatim — skip the diar⊕ROI
    builder and mark the track source 'manual'."""
    _words_file(runner)
    d = _seed_clip(runner)
    seen = {}
    _patch(monkeypatch, "reframe", "probe_dimensions", lambda p: (1920, 1080))
    _patch(monkeypatch, "reframe", "speaker_track", lambda *a, **k: pytest.fail("edited track → skip speaker_track"))
    _patch(monkeypatch, "reframe", "render", lambda c, t, **kw: (seen.update(track=t), Path(kw["out_path"]).write_bytes(b"R"))[-1])
    rois = {"left": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}, "right": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}}
    segs = [{"start": 0.0, "end": 3.0, "speaker": "right"}, {"start": 3.0, "end": 8.0, "speaker": "left"}]
    runner.reframe_target(clip_id="clipA", params={"rois": rois, "segments": segs})(_job("reframe", clip_id="clipA"))
    assert seen["track"]["segments"] == segs and seen["track"]["source"] == "manual"
    saved = json.loads((d / "track.json").read_text())
    assert saved["source"] == "manual" and saved["segments"] == segs
