"""Tests for the pure logic of per-shot face tracking (clip/face_track.py): picking the speaker's
face, framing it (adaptive zoom + rule-of-thirds), temporal smoothing, and building the ffmpeg
crop expressions. The OpenCV (YuNet) + ffmpeg I/O is verified on real media + the quality harness."""
from __future__ import annotations

from clip.face_track import pick_face, frame_rect, smooth_track, crop_exprs, cluster_by_x, mouth_motion


def test_cluster_by_x_groups_two_people_and_merges_one():
    W = 1920
    # faces tuple: (t, cx, cy, w, h, patch)
    left = [(0.0, 200, 300, 100, 100, []), (0.4, 210, 300, 100, 100, [])]
    right = [(0.0, 1500, 300, 100, 100, []), (0.4, 1490, 300, 100, 100, [])]
    clusters = cluster_by_x(left + right, W)
    assert len(clusters) == 2
    one = cluster_by_x(left + [(0.4, 230, 300, 100, 100, [])], W)
    assert len(one) == 1   # all near each other → a single person


def test_mouth_motion_higher_for_moving_lips():
    still = [[10, 10, 10, 10]] * 5                       # unchanging mouth patch → no motion
    talking = [[10, 50, 10, 50], [50, 10, 50, 10]] * 3    # alternating → lots of motion
    assert mouth_motion(talking) > mouth_motion(still)
    assert mouth_motion(still) == 0.0
    assert mouth_motion([[1, 2, 3]]) == 0.0              # <2 frames → 0


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


def _long_pan_timeline(n=400):
    """A long clip's worth of face-track keyframes with a continuous (non-collinear) pan —
    the shape that, one nested-if per keyframe, overflows ffmpeg's expression parser."""
    import math
    return [(round(i * 0.2, 3), 600 + 300 * math.sin(i / 12), 0.0, 473.3, 841.5, False)
            for i in range(n)]


def test_crop_exprs_bounded_for_long_clip():
    """A long clip has many keyframes; each crop expression must stay under a safe nesting
    budget regardless of length. Regression: ~100+ nested if() lerps overflowed ffmpeg's
    expression parser ('Missing )' / 'too many args') → the crop filter failed → 0-byte reframe.
    """
    for e in crop_exprs(_long_pan_timeline(400)):
        assert e.count("if(") <= 50, f"crop expression has {e.count('if(')} nested ifs (too many)"


def test_crop_exprs_render_through_real_ffmpeg_on_long_clip():
    """The true regression: the long-clip crop expression must be accepted by real ffmpeg's
    expression evaluator (the unit bound above is the proxy; this proves it end to end)."""
    import shutil
    import subprocess
    import pytest
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    w, h, x, y = crop_exprs(_long_pan_timeline(400))
    vf = f"crop=w='{w}':h='{h}':x='{x}':y='{y}',scale=1080:1920,setsar=1"
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "color=c=black:s=1920x1080:r=10:d=0.5", "-vf", vf, "-t", "0.5", "-f", "null", "-"],
        capture_output=True, text=True)
    assert r.returncode == 0, f"ffmpeg rejected the crop expression:\n{r.stderr[:400]}"


def test_reduce_points_keeps_endpoints_snaps_and_collapses_collinear():
    """Keyframe reduction is shape-preserving: a linear pan collapses to a handful of points,
    while the first/last keyframe and every hard-cut (snap) boundary survive."""
    from clip.face_track import _reduce_points
    pts = [(round(i * 0.2, 3), 100.0 + 5 * i, 0.0, 400.0, 700.0, i == 20) for i in range(40)]
    red = _reduce_points(pts, 1)   # param index 1 = x (a perfectly linear ramp here)
    ts = [p[0] for p in red]
    assert red[0] == pts[0] and red[-1] == pts[-1]   # endpoints preserved
    assert pts[20][0] in ts                           # the snap (hard cut) boundary kept
    assert len(red) < 12                              # collinear ramp collapses, not 40 points
