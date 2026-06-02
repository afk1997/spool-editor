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


# ---- orchestration (ffmpeg + roi_motion mocked) ---------------------------

def _rois():
    return ({"x": 0, "y": 0, "w": 960, "h": 1080}, {"x": 960, "y": 0, "w": 960, "h": 1080})


def test_speaker_track_fused_when_diarization_present(monkeypatch, tmp_path):
    measured = []
    monkeypatch.setattr(reframe, "_measure_roi_motion",
                        lambda clip, roi, out, **kw: measured.append(roi))
    monkeypatch.setattr(reframe, "_roi_motion_segments",
                        lambda l, r, m: [{"start": 0.0, "end": 5.0, "speaker": "left"},
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
                        lambda l, r, m: [{"start": 0.0, "end": 3.0, "speaker": "left"},
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
