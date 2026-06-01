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


def test_cut_invokes_ffmpeg_stream_copy(monkeypatch, tmp_path):
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
    assert "-c" in argv and "copy" in argv          # stream copy
    assert "-ss" in argv and "10.000" in argv        # input seek to start
    assert "-t" in argv and "15.500" in argv         # duration = end - start
    assert str(src) in argv and str(out) in argv


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
