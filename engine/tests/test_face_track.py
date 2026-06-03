"""Tests for the pure logic of per-shot face tracking (clip/face_track.py): picking the speaker's
face, framing it (adaptive zoom + rule-of-thirds), temporal smoothing, and building the ffmpeg
crop expressions. The OpenCV (YuNet) + ffmpeg I/O is verified on real media + the quality harness."""
from __future__ import annotations

from clip.face_track import pick_face, frame_rect, smooth_track, crop_exprs


def test_pick_face_prefers_upper_speaker_over_foreground_audience():
    W, H = 1920, 1080
    audience = (970, 910, 300, 300, 0.9)   # cy ≈ 0.84 — bottom third, biggest
    speaker = (1090, 340, 180, 180, 0.9)   # cy ≈ 0.31 — upper
    assert pick_face([audience, speaker], W, H)[0] == 1090   # the upper speaker's cx


def test_pick_face_stability_prefers_face_near_previous():
    W, H = 1920, 1080
    left = (400, 300, 200, 200, 0.9)
    right = (1500, 300, 210, 210, 0.9)   # slightly bigger
    # with no history, the bigger (right) wins; once we've been on the left, stay on the left
    assert pick_face([left, right], W, H)[0] == 1500
    assert pick_face([left, right], W, H, prev_cx=400 / W)[0] == 400


def test_frame_rect_zooms_to_target_on_a_medium_face_and_keeps_aspect():
    # a medium face (h=300 ≈ 28% of 1080) → crop zooms so the face ≈ the target fraction; 9:16 aspect
    x, y, w, h = frame_rect(960, 400, 300, 1920, 1080, 1080, 1920)
    assert abs(w / h - 1080 / 1920) < 0.01            # crop holds the 9:16 aspect
    assert h < 1080                                    # zoomed in (tighter than full height)
    assert abs(300 / h - 0.34) < 0.04                  # the face fills ~the target fraction
    assert 0 <= x <= 1920 - w and 0 <= y <= 1080 - h   # clamped inside the frame


def test_frame_rect_does_not_over_zoom_tiny_faces():
    # a tiny face (wide shot) would need a huge zoom to hit the target → clamp to the floor (no blur)
    _, _, w, h = frame_rect(960, 400, 120, 1920, 1080, 1080, 1920)
    assert abs(h - 1080 * 0.6) < 2                     # clamped to the ~0.6 zoom floor


def test_frame_rect_centers_x_and_thirds_y():
    x, y, w, h = frame_rect(960, 400, 150, 1920, 1080, 1080, 1920)
    assert abs((x + w / 2) - 960) < 1.0                # face centered horizontally
    assert abs((y + h * 0.40) - 400) < 1.0             # face on the upper-third line


def test_smooth_track_deadzone_holds_on_tiny_moves_and_resets_at_cut():
    pts = [
        (0.0, 100.0, 0.0, 600.0, 1067.0, False),
        (0.4, 103.0, 0.0, 600.0, 1067.0, False),   # tiny x move → dead-zoned (held)
        (0.8, 400.0, 0.0, 600.0, 1067.0, True),    # a cut → snap to the new value
    ]
    out = smooth_track(pts, deadzone=0.05, alpha=0.4)
    assert out[1][1] == 100.0     # tiny move held (no jitter)
    assert out[2][1] == 400.0     # snapped exactly at the cut


def test_crop_exprs_builds_four_expressions():
    pts = [(0.0, 100.0, 10.0, 600.0, 1067.0, False), (2.0, 300.0, 20.0, 500.0, 889.0, False)]
    w, h, x, y = crop_exprs(pts)
    assert "600.0" in w and "500.0" in w        # width interpolates
    assert "100.0" in x and "300.0" in x        # x interpolates
    assert "10.0" in y and "1067.0" in h
    assert "if(lt(t,2.000)" in x                # a time interval boundary
