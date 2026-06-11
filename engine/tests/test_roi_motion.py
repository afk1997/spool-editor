"""The vendored roi_motion CLI: segment boundaries must come from the frames' real
pts_time (24/25/29.97/50/60fps + VFR), not a hardcoded 30fps index division, and
degenerate inputs (empty / length-mismatched motion files) must not crash."""
import json
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "clip", "backhalf", "roi_motion.py")


def _write_motion(path, fps, active_ranges, n_frames):
    """Synthetic ffmpeg metadata-print output: frame i at pts_time i/fps, YAVG high
    inside active_ranges (frame-index ranges) and ~0 outside."""
    lines = []
    for i in range(n_frames):
        active = any(lo <= i < hi for lo, hi in active_ranges)
        lines.append(f"frame:{i}    pts:{i}      pts_time:{i / fps:.6f}")
        lines.append(f"lavfi.signalstats.YAVG={50.0 if active else 0.001}")
    path.write_text("\n".join(lines) + "\n")


def _run(left, right, *extra):
    return subprocess.run([sys.executable, SCRIPT, str(left), str(right), *extra],
                          capture_output=True, text=True, timeout=60)


def test_segment_boundaries_use_real_pts_time_at_60fps(tmp_path):
    n = 600  # 10s of 60fps video
    left, right = tmp_path / "l.txt", tmp_path / "r.txt"
    _write_motion(left, 60.0, [(0, 300)], n)     # left active for the first 5s
    _write_motion(right, 60.0, [(300, 600)], n)  # right active for the last 5s
    out = _run(left, right, "1.0")
    assert out.returncode == 0, out.stderr
    segs = json.loads(out.stdout)
    assert segs[0]["speaker"] == "left" and segs[-1]["speaker"] == "right"
    # The cut sits near t=5.0s wall-clock. The old i/30 math put it at ~10s (2x off).
    cut = segs[0]["end"]
    assert 4.0 <= cut <= 6.0, f"60fps cut landed at {cut}s — timeline mis-scaled"
    assert segs[-1]["end"] <= 10.5  # total duration is honest too


def test_empty_motion_files_emit_empty_json_not_crash(tmp_path):
    left, right = tmp_path / "l.txt", tmp_path / "r.txt"
    left.write_text("")
    right.write_text("")
    out = _run(left, right)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_length_mismatch_truncates_instead_of_asserting(tmp_path):
    left, right = tmp_path / "l.txt", tmp_path / "r.txt"
    _write_motion(left, 30.0, [(0, 90)], 91)   # one extra frame at EOF
    _write_motion(right, 30.0, [(0, 0)], 90)
    out = _run(left, right)
    assert out.returncode == 0, out.stderr
    segs = json.loads(out.stdout)
    assert segs and segs[0]["speaker"] == "left"
