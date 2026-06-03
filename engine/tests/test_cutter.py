"""Tests for clip.cutter — the lossless ffmpeg stream-copy trim.

cutter delegates the subprocess plumbing to clip._ffmpeg, so we patch the Popen
there. A _FakePopen drives the wait loop without shelling out; we assert the argv
contract + cancel/error handling.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clip import _ffmpeg, cutter


class _FakePopen:
    """Stand-in for subprocess.Popen — finishes immediately by default."""
    def __init__(self, argv, **kw):
        self.argv = argv
        self.returncode = 0
        self._killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self._killed = True
        self.returncode = -9


def test_cut_invokes_ffmpeg_accurate_reencode(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"CLIP")  # fake a successful cut
        return _FakePopen(argv, **kw)

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", fake_popen)

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    out = tmp_path / "clip.mp4"

    result = cutter.cut(str(src), 10.0, 25.5, str(out))

    assert result == str(out)
    assert out.exists()
    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    # Frame-accurate trim, NOT a keyframe-aligned stream-copy: the video is re-encoded so
    # the clip begins exactly at `start` (a `-c copy` would start up to a GOP early and
    # desync every caption). See tests/test_caption_sync.py for the behavioral proof.
    assert "-c:v" in argv and "libx264" in argv      # re-encode the video (frame-accurate)
    assert "copy" not in argv                          # never a stream-copy
    assert "-ss" in argv and "10.000" in argv          # fast input seek to start
    assert "-t" in argv and "15.500" in argv           # duration = end - start
    assert str(src) in argv and str(out) in argv


def test_cut_spans_builds_trim_concat_filtergraph(monkeypatch, tmp_path):
    """Ripple cut (transcript edit drops interior words): trim each kept span and concat."""
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"CLIP")
        return _FakePopen(argv, **kw)

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", fake_popen)
    src = tmp_path / "src.mp4"; src.write_bytes(b"x")
    out = tmp_path / "ripple.mp4"

    result = cutter.cut_spans(str(src), [(1.0, 3.0), (5.0, 8.0)], str(out))

    assert result == str(out) and out.exists()
    argv = captured["argv"]
    fc = argv[argv.index("-filter_complex") + 1]
    assert "trim=start=1.000:end=3.000" in fc          # first kept span (video)
    assert "atrim=start=5.000:end=8.000" in fc          # second kept span (audio)
    assert "concat=n=2:v=1:a=1[v][a]" in fc             # joined back into one stream
    assert "[v]" in argv and "[a]" in argv              # mapped out


def test_cut_spans_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        cutter.cut_spans(str(tmp_path / "s.mp4"), [], str(tmp_path / "o.mp4"))


@pytest.mark.parametrize("start,end", [(5.0, 5.0), (10.0, 3.0)])
def test_cut_rejects_nonpositive_range(tmp_path, start, end):
    with pytest.raises(ValueError):
        cutter.cut(str(tmp_path / "s.mp4"), start, end, str(tmp_path / "o.mp4"))


def test_cut_rejects_negative_start(tmp_path):
    with pytest.raises(ValueError):
        cutter.cut(str(tmp_path / "s.mp4"), -1.0, 5.0, str(tmp_path / "o.mp4"))


def test_cut_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    def fake_popen(argv, **kw):
        p = _FakePopen(argv, **kw)
        p.returncode = 1
        return p

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="ffmpeg cut failed"):
        cutter.cut(str(tmp_path / "s.mp4"), 0.0, 5.0, str(tmp_path / "o.mp4"))


def test_cut_cancellable_mid_cut(monkeypatch, tmp_path):
    class _SlowPopen(_FakePopen):
        def wait(self, timeout=None):
            if self._killed:
                return self.returncode
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", _SlowPopen)
    polls = [False, True]  # cancel on the second poll

    with pytest.raises(RuntimeError, match="cancelled"):
        cutter.cut(
            str(tmp_path / "s.mp4"), 0.0, 5.0, str(tmp_path / "o.mp4"),
            cancel_check=lambda: polls.pop(0) if polls else True,
        )


def test_cut_register_proc_called_then_cleared(monkeypatch, tmp_path):
    def fake_popen(argv, **kw):
        Path(argv[-1]).write_bytes(b"x")
        return _FakePopen(argv, **kw)

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", fake_popen)
    seen = []
    cutter.cut(
        str(tmp_path / "s.mp4"), 0.0, 2.0, str(tmp_path / "o.mp4"),
        register_proc=seen.append,
    )
    # live Popen first, then None in the finally block (matches transcriber)
    assert len(seen) == 2 and seen[-1] is None
