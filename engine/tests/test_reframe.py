"""Tests for clip.reframe — the analysis half (detect_faces + diar⊕ROI speaker_track).

The fusion logic is pure and gets the most coverage (it's the signature feature).
ffmpeg / roi_motion are mocked for the orchestration + detect_faces tests.
"""
from __future__ import annotations

import pytest

from clip import reframe


# ---- pure fusion logic ----------------------------------------------------

def test_collapse_merges_consecutive_same_side():
    segs = [
        {"start": 0.0, "end": 2.0, "speaker": "left"},
        {"start": 2.0, "end": 4.0, "speaker": "left"},
        {"start": 4.0, "end": 6.0, "speaker": "right"},
    ]
    assert reframe._collapse(segs) == [
        {"start": 0.0, "end": 4.0, "speaker": "left"},
        {"start": 4.0, "end": 6.0, "speaker": "right"},
    ]


def test_video_side_for_picks_dominant_overlap():
    video = [
        {"start": 0.0, "end": 5.0, "speaker": "left"},
        {"start": 5.0, "end": 10.0, "speaker": "right"},
    ]
    assert reframe._video_side_for(video, 0.0, 4.0) == "left"
    assert reframe._video_side_for(video, 6.0, 10.0) == "right"
    assert reframe._video_side_for(video, 100.0, 110.0) is None  # no overlap


def test_fuse_maps_two_speakers_to_their_sides():
    video = [
        {"start": 0.0, "end": 5.0, "speaker": "left"},
        {"start": 5.0, "end": 10.0, "speaker": "right"},
    ]
    diar = [
        {"start": 0.0, "end": 5.0, "speaker": "S1"},
        {"start": 5.0, "end": 10.0, "speaker": "S2"},
    ]
    assert reframe._fuse_diar_roi(video, diar) == [
        {"start": 0.0, "end": 5.0, "speaker": "left"},
        {"start": 5.0, "end": 10.0, "speaker": "right"},
    ]


def test_fuse_resolves_still_speaker_via_audio_and_distinct_sides():
    """The diar⊕ROI win: the right speaker is still (no video motion — the left box
    moves the whole time), but audio knows when S2 talks. S1 claims left (strong video
    signal); S2 is forced to the opposite side, so the still speaker still gets framed."""
    video = [{"start": 0.0, "end": 10.0, "speaker": "left"}]  # only the left face ever moved
    diar = [
        {"start": 0.0, "end": 3.0, "speaker": "S1"},
        {"start": 3.0, "end": 6.0, "speaker": "S2"},  # still speaker
        {"start": 6.0, "end": 10.0, "speaker": "S1"},
    ]
    fused = reframe._fuse_diar_roi(video, diar)
    assert fused == [
        {"start": 0.0, "end": 3.0, "speaker": "left"},
        {"start": 3.0, "end": 6.0, "speaker": "right"},
        {"start": 6.0, "end": 10.0, "speaker": "left"},
    ]


def test_fuse_collapses_consecutive_same_speaker_turns():
    video = [{"start": 0.0, "end": 6.0, "speaker": "left"}]
    diar = [
        {"start": 0.0, "end": 3.0, "speaker": "S1"},
        {"start": 3.0, "end": 6.0, "speaker": "S1"},
    ]
    assert reframe._fuse_diar_roi(video, diar) == [{"start": 0.0, "end": 6.0, "speaker": "left"}]


def test_fuse_with_no_turns_falls_back_to_video():
    video = [{"start": 0.0, "end": 5.0, "speaker": "right"}]
    assert reframe._fuse_diar_roi(video, []) == video


def test_diar_speaker_sides_maps_speakers_to_opposite_sides():
    """The reusable speaker->side map (also used by the auto-pan face-track fusion, item B):
    each diar speaker resolves to the screen side its turns most overlap in the video timeline,
    distinct speakers kept on opposite sides."""
    video = [
        {"start": 0.0, "end": 5.0, "speaker": "left"},
        {"start": 5.0, "end": 10.0, "speaker": "right"},
    ]
    diar = [
        {"start": 0.0, "end": 5.0, "speaker": "S1"},
        {"start": 5.0, "end": 10.0, "speaker": "S2"},
    ]
    assert reframe.diar_speaker_sides(video, diar) == {"S1": "left", "S2": "right"}
    assert reframe.diar_speaker_sides(video, []) == {}


# ---- orchestration (ffmpeg + roi_motion mocked) ---------------------------

def _rois():
    return ({"x": 0, "y": 0, "w": 960, "h": 1080}, {"x": 960, "y": 0, "w": 960, "h": 1080})


def test_speaker_track_fused_when_diarization_present(monkeypatch, tmp_path):
    measured = []
    monkeypatch.setattr(reframe, "_measure_roi_motion",
                        lambda clip, roi, out, **kw: measured.append(roi))
    monkeypatch.setattr(reframe, "_roi_motion_segments",
                        lambda l, r, m, smoothing=None: [{"start": 0.0, "end": 5.0, "speaker": "left"},
                                         {"start": 5.0, "end": 10.0, "speaker": "right"}])
    left, right = _rois()
    track = reframe.speaker_track(
        "clip.mp4", roi_left=left, roi_right=right, work_dir=str(tmp_path),
        diarization=[{"start": 0.0, "end": 5.0, "speaker": "A"},
                     {"start": 5.0, "end": 10.0, "speaker": "B"}],
    )
    assert len(measured) == 2                      # both ROIs measured
    assert track["source"] == "fused"
    assert track["roiL"] == left and track["roiR"] == right
    assert track["segments"][0]["speaker"] == "left"
    assert track["segments"][1]["speaker"] == "right"


def test_speaker_track_roi_only_without_diarization(monkeypatch, tmp_path):
    monkeypatch.setattr(reframe, "_measure_roi_motion", lambda *a, **k: None)
    monkeypatch.setattr(reframe, "_roi_motion_segments",
                        lambda l, r, m, smoothing=None: [{"start": 0.0, "end": 3.0, "speaker": "left"},
                                         {"start": 3.0, "end": 4.0, "speaker": "left"}])
    left, right = _rois()
    track = reframe.speaker_track("clip.mp4", roi_left=left, roi_right=right, work_dir=str(tmp_path))
    assert track["source"] == "roi"
    assert track["segments"] == [{"start": 0.0, "end": 4.0, "speaker": "left"}]  # collapsed


def test_detect_faces_returns_default_half_rois(monkeypatch, tmp_path):
    monkeypatch.setattr(reframe, "probe_dimensions", lambda p: (1920, 1080))
    monkeypatch.setattr(reframe._ffmpeg, "run", lambda *a, **k: None)  # skip real frame extract
    out = reframe.detect_faces("clip.mp4", frame_path=str(tmp_path / "f.jpg"))
    assert out["width"] == 1920 and out["height"] == 1080
    assert out["rois"]["left"] == {"x": 0, "y": 0, "w": 960, "h": 1080}
    assert out["rois"]["right"] == {"x": 960, "y": 0, "w": 960, "h": 1080}


# ---- render (ffmpeg mocked; pan_expr runs for real) -----------------------

def _track():
    return {
        "segments": [{"start": 0.0, "end": 5.0, "speaker": "left"},
                     {"start": 5.0, "end": 10.0, "speaker": "right"}],
        "roiL": {"x": 0, "y": 0, "w": 960, "h": 1080},
        "roiR": {"x": 960, "y": 0, "w": 960, "h": 1080},
        "source": "fused",
    }


def _capture_render(monkeypatch):
    captured = {}
    monkeypatch.setattr(reframe, "probe_dimensions", lambda p: (1920, 1080))
    monkeypatch.setattr(reframe._ffmpeg, "run", lambda argv, **kw: captured.update(argv=argv))
    return captured


def test_render_pan_builds_crop_strip_and_pan_expr(monkeypatch, tmp_path):
    captured = _capture_render(monkeypatch)
    result = reframe.render("clip.mp4", _track(), aspect="9:16", mode="pan", out_path=str(tmp_path / "o.mp4"))
    assert result == str(tmp_path / "o.mp4")
    vf = captured["argv"][captured["argv"].index("-vf") + 1]
    assert vf.startswith("crop=608:1080:x='")     # vertical 9:16 strip from 1080p
    assert "scale=1080:1920" in vf
    assert "176" in vf and "1136" in vf            # left/right strip x derived from the ROIs


def test_render_center_is_a_centered_crop(monkeypatch, tmp_path):
    captured = _capture_render(monkeypatch)
    reframe.render("clip.mp4", _track(), aspect="9:16", mode="center", out_path=str(tmp_path / "o.mp4"))
    vf = captured["argv"][captured["argv"].index("-vf") + 1]
    assert vf.startswith("crop=608:1080:")
    assert "x='" not in vf                          # static crop, no pan expression
    assert "scale=1080:1920" in vf


def test_render_split_stacks_both_rois(monkeypatch, tmp_path):
    captured = _capture_render(monkeypatch)
    reframe.render("clip.mp4", _track(), aspect="9:16", mode="split", out_path=str(tmp_path / "o.mp4"))
    argv = captured["argv"]
    assert "-filter_complex" in argv
    graph = argv[argv.index("-filter_complex") + 1]
    assert "vstack=inputs=2[vout]" in graph
    assert graph.count("crop=") == 2                # one per ROI
    assert "-map" in argv and "[vout]" in argv


def test_render_preview_is_low_res_and_ultrafast(monkeypatch, tmp_path):
    """The editor preview renders the REAL reframe, but downscaled + ultrafast (throwaway)."""
    captured = _capture_render(monkeypatch)
    reframe.render("clip.mp4", _track(), aspect="9:16", mode="pan", preview=True,
                   out_path=str(tmp_path / "preview.mp4"))
    argv = captured["argv"]
    vf = argv[argv.index("-vf") + 1]
    assert vf.endswith(",scale=-2:640")              # downscaled after the real crop/scale
    assert "scale=1080:1920" in vf                   # the real reframe is still computed first
    assert "ultrafast" in argv and argv[argv.index("-crf") + 1] == "30"  # fast throwaway encode


def test_render_default_is_not_downscaled(monkeypatch, tmp_path):
    captured = _capture_render(monkeypatch)
    reframe.render("clip.mp4", _track(), aspect="9:16", mode="pan", out_path=str(tmp_path / "o.mp4"))
    vf = captured["argv"][captured["argv"].index("-vf") + 1]
    assert "scale=-2:640" not in vf and "ultrafast" not in captured["argv"]  # full-res real render


def test_render_pan_without_segments_falls_back_to_center(monkeypatch, tmp_path):
    captured = _capture_render(monkeypatch)
    track = _track()
    track["segments"] = []
    reframe.render("clip.mp4", track, aspect="9:16", mode="pan", out_path=str(tmp_path / "o.mp4"))
    vf = captured["argv"][captured["argv"].index("-vf") + 1]
    assert "x='" not in vf                          # no timeline → centered crop fallback


@pytest.mark.parametrize("kw", [{"aspect": "3:2"}, {"mode": "zoom"}])
def test_render_rejects_unknown_aspect_or_mode(tmp_path, kw):
    with pytest.raises(ValueError):
        reframe.render("clip.mp4", _track(), out_path=str(tmp_path / "o.mp4"), **kw)


# ---- Phase 2: tunable speaker-track + crop margin (S7) --------------------

def test_speaker_track_forwards_min_dwell_and_smoothing(monkeypatch, tmp_path):
    """min-dwell + smoothing are real S7 knobs, forwarded to the timeline builder."""
    seen = {}
    monkeypatch.setattr(reframe, "_measure_roi_motion", lambda *a, **k: None)
    monkeypatch.setattr(
        reframe, "_roi_motion_segments",
        lambda l, r, min_dwell, smoothing=None: (seen.update(min_dwell=min_dwell, smoothing=smoothing), [])[-1],
    )
    left, right = _rois()
    reframe.speaker_track("clip.mp4", roi_left=left, roi_right=right,
                          work_dir=str(tmp_path), min_dwell=2.5, smoothing=31)
    assert seen["min_dwell"] == 2.5
    assert seen["smoothing"] == 31


def _write_motion(path, vals):
    """Synthesize a roi_motion input file: one frame = a pts_time line + a YAVG line."""
    from pathlib import Path
    lines = []
    for i, v in enumerate(vals):
        lines.append(f"frame:{i} pts:{i} pts_time:{i / 30:.6f}")
        lines.append(f"lavfi.signalstats.YAVG={v:.6f}")
    Path(path).write_text("\n".join(lines) + "\n")


def test_roi_motion_smoothing_changes_segmentation(tmp_path):
    """Smoothing is a real knob in the vendored timeline builder: a tiny window follows
    every alternation a heavy window averages away. Runs the real roi_motion CLI with
    min_dwell=0 so the merge step doesn't mask the effect."""
    left_vals, right_vals = [], []
    for block in range(6):                       # 6 × 15 frames, alternating dominance
        hi = block % 2 == 0
        left_vals += [2.0 if hi else 1.0] * 15
        right_vals += [1.0 if hi else 2.0] * 15
    lz, rz = str(tmp_path / "l.txt"), str(tmp_path / "r.txt")
    _write_motion(lz, left_vals)
    _write_motion(rz, right_vals)
    sharp = reframe._roi_motion_segments(lz, rz, 0.0, smoothing=1)
    smooth = reframe._roi_motion_segments(lz, rz, 0.0, smoothing=91)
    assert len(sharp) >= 6                        # tiny window keeps every alternation
    assert len(smooth) <= 2                        # heavy window collapses them away
    assert len(sharp) > len(smooth)                # smoothing demonstrably reduces switching


def test_render_pan_crop_margin_tightens_crop(monkeypatch, tmp_path):
    """crop-margin (S7) zooms the pan crop in — a tighter box than the full-height strip,
    still scaled to the target aspect. crop_margin=0 is byte-identical to today (above)."""
    import re
    captured = _capture_render(monkeypatch)
    reframe.render("clip.mp4", _track(), aspect="9:16", mode="pan",
                   crop_margin=0.25, out_path=str(tmp_path / "o.mp4"))
    vf = captured["argv"][captured["argv"].index("-vf") + 1]
    m = re.match(r"crop=(\d+):(\d+):", vf)
    cw, ch = int(m.group(1)), int(m.group(2))
    assert ch < 1080 and cw < 608                # tighter than the crop_margin=0 strip
    assert "scale=1080:1920" in vf               # still fills 9:16
