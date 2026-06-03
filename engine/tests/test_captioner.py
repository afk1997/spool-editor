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


def test_generate_applies_style_overrides(tmp_path):
    """S8 fine styling maps to the real ASS: size/outline/fill/highlight/position/allcaps."""
    words = _write_words(tmp_path)
    out = tmp_path / "ov.ass"
    captioner.generate(
        words, clip_start=10.0, clip_end=12.0, style="opus", out_ass_path=str(out),
        overrides={"size": 120, "outline": 10, "words": 2, "fill": "#ff0000",
                   "highlight": "#00ff00", "position": 25, "allcaps": True},
    )
    content = out.read_text()
    assert "Default,Arial Black,120," in content   # size override in the style line
    assert ",1,10,3,2," in content                 # BorderStyle,Outline=10,Shadow,Alignment
    assert "&H000000FF&" in content                # fill #ff0000 → ASS BBGGRR primary
    assert "&H0000FF00&" in content                # active-word highlight #00ff00
    assert ",60,60,480,1" in content               # position 25% → MarginV 480 of 1920
    assert "HELLO" in content                       # allcaps uppercased the word text


def test_generate_overrides_can_disable_highlight(tmp_path):
    words = _write_words(tmp_path)
    out = tmp_path / "nohl.ass"
    captioner.generate(words, clip_start=10.0, clip_end=12.0, style="opus", out_ass_path=str(out),
                       overrides={"highlight": None})
    assert "&H0000FFFF&" not in out.read_text()    # opus default yellow highlight removed


def test_generate_appends_watermark_and_lower_third(tmp_path):
    """A brand kit's watermark + lower-third burn in via the same libass path (S9)."""
    words = _write_words(tmp_path)
    out = tmp_path / "wm.ass"
    captioner.generate(words, clip_start=10.0, clip_end=12.0, style="opus", out_ass_path=str(out),
                       watermark="@acme", lower_third="Local First")
    content = out.read_text()
    assert "@acme" in content and "Local First" in content   # both static lines present
    assert "\\an9" in content                                # watermark pinned top-right


# ---- caption craft (item D): speaker color · line balance · keyword emphasis ----

def _write_2spk_words(tmp_path) -> str:
    """Six words in [0,3], two speakers (S1 then S2), with one ALL-CAPS word (NASA)."""
    p = tmp_path / "two.words.json"
    spk = ["Speaker 1", "Speaker 1", "Speaker 1", "Speaker 2", "Speaker 2", "Speaker 2"]
    txt = ["the", "quick", "brown", "fox", "jumps", "NASA"]
    p.write_text(json.dumps({"schema_version": 2, "words": [
        {"idx": i, "w": txt[i], "start": i * 0.5, "end": i * 0.5 + 0.4,
         "deleted": False, "speaker": spk[i]} for i in range(6)]}))
    return str(p)


def test_caption_craft_off_by_default_is_unchanged(tmp_path):
    """With no caption-craft flags, the ASS carries NO speaker-color or scale tags — the
    output is the original (byte-identical guard is also covered by the cross-version diff)."""
    words = _write_2spk_words(tmp_path)
    out = tmp_path / "plain.ass"
    captioner.generate(words, clip_start=0.0, clip_end=3.0, style="opus", out_ass_path=str(out))
    content = out.read_text()
    assert "\\fscx120" not in content                  # no emphasis scaling
    assert "&H003CC9FF&" not in content                # no speaker-2 palette color


def test_color_speakers_tints_words_per_speaker(tmp_path):
    words = _write_2spk_words(tmp_path)
    out = tmp_path / "spk.ass"
    captioner.generate(words, clip_start=0.0, clip_end=3.0, style="opus",
                       color_speakers=True, out_ass_path=str(out))
    content = out.read_text()
    assert "&H003CC9FF&" in content                    # speaker 2 → palette[1] (gold)


def test_color_speakers_reads_speaker_from_segments(tmp_path):
    """The REAL serialized words.json carries the speaker on SEGMENTS, not the flat word list.
    Speaker-coloring must still resolve each word's speaker from its containing segment."""
    p = tmp_path / "seg.words.json"
    p.write_text(json.dumps({
        "schema_version": 2,
        "words": [   # flat words have NO speaker key (the production serialization)
            {"idx": 0, "w": "alpha", "start": 0.0, "end": 0.4, "deleted": False},
            {"idx": 1, "w": "beta", "start": 0.5, "end": 0.9, "deleted": False},
            {"idx": 2, "w": "gamma", "start": 2.0, "end": 2.4, "deleted": False},
        ],
        "segments": [   # ...but the segments do
            {"start": 0.0, "end": 1.0, "speaker": "Speaker 1", "words": []},
            {"start": 1.5, "end": 3.0, "speaker": "Speaker 2", "words": []},
        ],
    }))
    out = tmp_path / "seg.ass"
    captioner.generate(str(p), clip_start=0.0, clip_end=3.0, style="opus",
                       color_speakers=True, out_ass_path=str(out))
    assert "&H003CC9FF&" in out.read_text()        # speaker 2 (from segment lookup) → palette[1]


def test_color_speakers_noop_for_single_speaker(tmp_path):
    """One speaker in the window → captions stay exactly as they were (no palette tinting)."""
    words = _write_words(tmp_path)                      # speaker-less / single
    out = tmp_path / "one.ass"
    captioner.generate(words, clip_start=10.0, clip_end=12.0, style="opus",
                       color_speakers=True, out_ass_path=str(out))
    assert "&H003CC9FF&" not in out.read_text()


def test_emphasis_scales_allcaps_keyword(tmp_path):
    words = _write_2spk_words(tmp_path)
    out = tmp_path / "emph.ass"
    captioner.generate(words, clip_start=0.0, clip_end=3.0, style="opus",
                       emphasis=True, out_ass_path=str(out))
    content = out.read_text()
    assert "\\fscx120\\fscy120}NASA" in content         # the ALL-CAPS word is scaled up
    assert "\\fscx120\\fscy120}the" not in content      # ordinary words are not


def test_build_chunks_balance_removes_orphan_last_line():
    from clip.backhalf import ass_captions
    words = [{"start": i, "end": i + 1, "text": str(i)} for i in range(7)]
    assert [len(c) for c in ass_captions.build_chunks(words, 3)] == [3, 3, 1]            # fixed
    assert [len(c) for c in ass_captions.build_chunks(words, 3, balance=True)] == [3, 2, 2]  # balanced
    # exact-fit input is untouched by balancing
    six = [{"start": i, "end": i + 1, "text": str(i)} for i in range(6)]
    assert [len(c) for c in ass_captions.build_chunks(six, 3, balance=True)] == [3, 3]


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
