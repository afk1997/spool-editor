"""Caption↔audio sync regressions: a cut clip must BEGIN at the requested start.

The cut feeds the caption stage, which re-bases word times to ``(word - clip_start)``.
If the cut begins early — at the nearest prior keyframe, as a lossless ``-c copy``
stream-copy does — then every caption is early by that offset: the reported desync,
and the clip also starts before the chosen moment. These tests run REAL ffmpeg on a
synthetic source with sparse keyframes to prove the cut is frame-accurate, measuring
the clip's true start by FFT cross-correlation (the vendored ``audio_xcorr``) rather
than trusting container metadata.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from clip import cutter

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs real ffmpeg/ffprobe on PATH",
)

_XCORR = os.path.join(os.path.dirname(__file__), "..", "clip", "backhalf", "audio_xcorr.py")


def _make_source(path, *, dur=8.0, gop=2.0, fps=24, seed=7):
    """A synthetic mp4 with sparse keyframes (every ``gop`` s) and seeded pink noise
    audio (broadband → a sharp cross-correlation peak)."""
    g = int(round(gop * fps))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration={dur}",
         "-f", "lavfi", "-i", f"anoisesrc=d={dur}:c=pink:r=44100:seed={seed}",
         "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


def _duration(path):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True).stdout.strip() or 0)


def _keyframes(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip().splitlines()
    # csv=p=0 can emit a trailing field separator (``0.000000,``) — take the first token.
    return sorted(float(line.split(",")[0]) for line in out if line.split(",")[0])


def _to_pcm(src, out, *, ss=None, t=None):
    argv = ["ffmpeg", "-y", "-v", "error"]
    if ss is not None:
        argv += ["-ss", f"{ss:.3f}"]
    argv += ["-i", str(src)]
    if t is not None:
        argv += ["-t", f"{t:.3f}"]
    argv += ["-ac", "1", "-ar", "8000", "-f", "s16le", str(out)]
    subprocess.run(argv, check=True)
    return out


def _true_start(clip, src):
    """Source-time where ``clip``'s audio actually begins (FFT cross-correlation)."""
    d = tempfile.mkdtemp(prefix="xc.")
    try:
        cp = _to_pcm(clip, os.path.join(d, "c.pcm"))
        sp = _to_pcm(src, os.path.join(d, "s.pcm"))
        res = subprocess.run([sys.executable, _XCORR, cp, sp, "0.0"],
                             capture_output=True, text=True)
        return float(res.stdout.strip())
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cut_begins_at_requested_start_not_prior_keyframe(tmp_path):
    src = tmp_path / "src.mp4"
    _make_source(src)  # keyframes at 0, 2, 4, 6
    # Precondition: start=3.0 must be mid-GOP (nearest prior keyframe strictly before it),
    # else the scenario can't exercise the bug.
    kfs = _keyframes(src)
    assert max(k for k in kfs if k <= 3.0) < 2.9, f"start 3.0 not mid-GOP; keyframes={kfs}"

    out = tmp_path / "clip.mp4"
    cutter.cut(str(src), 3.0, 7.0, str(out))

    # The clip must be the requested 4.0 s, not 5.0 s (4.0 + the 1.0 s keyframe preroll).
    dur = _duration(out)
    assert abs(dur - 4.0) < 0.2, f"clip is {dur:.2f}s — keyframe preroll not trimmed"

    # And its audio must actually begin at source-time 3.0, not 2.0 (the prior keyframe).
    ts = _true_start(out, src)
    assert abs(ts - 3.0) < 0.15, f"clip begins at {ts:.2f}s in source, expected 3.0s"
