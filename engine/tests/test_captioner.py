"""Tests for clip.captioner.

generate() runs the real (pure-Python, no ffmpeg) vendored ASS generator, so we
assert actual sliced + re-based output. burn() delegates to clip._ffmpeg, so we
patch the Popen there and assert the subtitles-filter argv.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clip import _ffmpeg, captioner


def _write_words(tmp_path) -> str:
    """A minimal trove-style words.json: two words inside [10, 12], one far later."""
    p = tmp_path / "src.words.json"
    p.write_text(json.dumps({
        "schema_version": 2,
        "language": "en",
        "words": [
            {"idx": 0, "w": "hello", "start": 10.0, "end": 10.5, "deleted": False},
            {"idx": 1, "w": "world", "start": 10.6, "end": 11.2, "deleted": False},
            {"idx": 2, "w": "gone", "start": 10.7, "end": 10.9, "deleted": True},
            {"idx": 3, "w": "later", "start": 30.0, "end": 30.4, "deleted": False},
        ],
    }))
    return str(p)


def test_generate_slices_rebases_and_styles(tmp_path):
    words = _write_words(tmp_path)
    out = tmp_path / "caps.ass"

    result = captioner.generate(
        words, clip_start=10.0, clip_end=12.0, style="opus", out_ass_path=str(out),
    )

    assert result == str(out)
    content = out.read_text()
    assert "[Events]" in content and "Dialogue:" in content
    assert "hello" in content and "world" in content   # words inside the window
    assert "later" not in content                      # outside the window, dropped
    assert "gone" not in content                        # deleted word, dropped
    assert "Arial Black" in content                     # opus preset font
    assert "0:00:00.00" in content                      # first word re-based to t=0


def test_generate_rejects_unknown_style(tmp_path):
    with pytest.raises(ValueError, match="caption style"):
        captioner.generate(
            "ignored.json", clip_start=0.0, clip_end=5.0, style="fancy",
            out_ass_path=str(tmp_path / "o.ass"),
        )


def test_generate_rejects_inverted_window(tmp_path):
    with pytest.raises(ValueError):
        captioner.generate(
            "ignored.json", clip_start=5.0, clip_end=5.0, style="opus",
            out_ass_path=str(tmp_path / "o.ass"),
        )


def test_generate_empty_window_raises(tmp_path):
    words = _write_words(tmp_path)
    with pytest.raises(ValueError, match="no words"):
        captioner.generate(
            words, clip_start=100.0, clip_end=110.0, style="opus",
            out_ass_path=str(tmp_path / "o.ass"),
        )


def test_generate_supports_all_presets(tmp_path):
    words = _write_words(tmp_path)
    for style in ("opus", "karaoke", "minimal"):
        out = tmp_path / f"{style}.ass"
        captioner.generate(words, clip_start=10.0, clip_end=12.0, style=style, out_ass_path=str(out))
        assert "[Events]" in out.read_text()


# ---- burn ----------------------------------------------------------------

class _FakePopen:
    def __init__(self, argv, **kw):
        self.argv = argv
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_burn_invokes_subtitles_filter(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"OUT")
        return _FakePopen(argv, **kw)

    monkeypatch.setattr(_ffmpeg.subprocess, "Popen", fake_popen)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    ass = tmp_path / "caps.ass"
    ass.write_text("[Events]")
    out = tmp_path / "burned.mp4"

    result = captioner.burn(str(clip), str(ass), str(out))

    assert result == str(out)
    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    vf = argv[argv.index("-vf") + 1]
    assert vf.startswith("subtitles=") and str(ass) in vf
    assert "-c:a" in argv and "copy" in argv
    assert str(clip) in argv and str(out) in argv
