"""Tests for the pure logic of per-shot face tracking (clip/face_track.py): picking the speaker's
face from detected boxes, and building the ffmpeg crop-x expression that follows it (lerp within a
shot, snap at a cut, clamped to the frame). The OpenCV + ffmpeg I/O is verified on real media."""
from __future__ import annotations

from clip.face_track import dominant_face_cx, crop_x_expr


def test_dominant_face_cx_prefers_upper_speaker_over_foreground_audience():
    W, H = 1920, 1080
    # a big face low in the frame (foreground audience back-of-head) + a smaller upper face (speaker)
    audience = (820, 760, 300, 300)   # cy ≈ 0.84 — bottom third
    speaker = (1000, 250, 180, 180)   # cy ≈ 0.31 — upper
    cx = dominant_face_cx([audience, speaker], W, H)
    assert abs(cx - (1000 + 90) / W) < 1e-6   # the SPEAKER's center, not the bigger audience face


def test_dominant_face_cx_falls_back_to_largest_when_all_low():
    W, H = 1920, 1080
    a = (100, 800, 120, 120)
    b = (1500, 820, 240, 240)  # largest, also low
    assert abs(dominant_face_cx([a, b], W, H) - (1500 + 120) / W) < 1e-6


def test_dominant_face_cx_none_when_no_faces():
    assert dominant_face_cx([], 1920, 1080) is None


def test_crop_x_expr_clamps_to_frame():
    # cw=608 in a 1920 frame → x ∈ [0, 1312]. cx=0.0 → 0 ; cx=1.0 → 1312.
    assert crop_x_expr([(0.0, 0.0, False)], 1920, 608) == "0.0"
    assert crop_x_expr([(0.0, 1.0, False)], 1920, 608) == "1312.0"
    # centered
    assert crop_x_expr([(0.0, 0.5, False)], 1920, 608) == "656.0"


def test_crop_x_expr_lerps_within_a_shot():
    # two same-shot points → a time-interpolated x between them
    expr = crop_x_expr([(0.0, 0.25, False), (2.0, 0.75, False)], 1920, 608)
    assert "if(lt(t,2" in expr            # an interval boundary at t=2
    assert "(t-0.000)" in expr            # lerps from t0
    # x at cx=0.25 → 0.25*1920-304 = 176 ; cx=0.75 → 1136
    assert "176.0" in expr and "1136.0" in expr


def test_crop_x_expr_snaps_at_a_cut():
    # the second point is the start of a new shot (snap=True) → HOLD the first x until the cut, then
    # jump (no lerp across the cut).
    expr = crop_x_expr([(0.0, 0.2, False), (3.0, 0.8, True)], 1920, 608)
    # before t=3 it holds the first x (no "(t-" lerp term in that branch)
    x0 = 0.2 * 1920 - 304  # 80.0
    assert f"if(lt(t,3.000),{x0:.1f}," in expr
