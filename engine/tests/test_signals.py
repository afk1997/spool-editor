"""Tests for clip.signals — glass-box NON-text moment signals (item E).

text_signals is pure + deterministic (no media). audio_energy / scene_density parse ffmpeg
output, so we mock subprocess; annotate ties them onto candidates' ``features``.
"""
from __future__ import annotations

import types

from clip import signals


# --- text_signals (deterministic, no LLM, no media) --------------------------

def test_text_signals_detects_questions():
    assert signals.text_signals("What is the secret?")["is_question"] is True   # ? and a question word
    assert signals.text_signals("Is it true")["is_question"] is True            # leading question word, no ?
    assert signals.text_signals("I built a thing.")["is_question"] is False


def test_text_signals_exclamation_numbers_and_intensity():
    s = signals.text_signals("We made 3 million in 2024 — absolutely insane!")
    assert s["exclamation"] is True
    assert s["numbers"] == 2                       # "3" and "2024"
    assert s["intensity_hits"] >= 1                # "insane"
    assert 0.0 < s["intensity"] <= 1.0


def test_text_signals_filler_ratio_and_word_rate():
    s = signals.text_signals("um uh you know", duration=2.0)
    assert s["filler_ratio"] == 1.0                # every token is a filler
    assert s["word_rate"] == 2.0                   # 4 words / 2s
    assert signals.text_signals("hello")["word_rate"] is None   # no duration → None


# --- audio_energy / scene_density (ffmpeg parsing, mocked) -------------------

def _fake_run(stderr):
    return lambda *a, **k: types.SimpleNamespace(stderr=stderr, stdout="", returncode=0)


def test_audio_energy_parses_volumedetect(monkeypatch):
    monkeypatch.setattr(signals.subprocess, "run",
                        _fake_run("[Parsed_volumedetect] mean_volume: -22.5 dB\nmax_volume: -3.0 dB\n"))
    ae = signals.audio_energy("m.mp4", 1.0, 4.0)
    assert ae == {"mean_db": -22.5, "max_db": -3.0, "dynamic_db": 19.5}


def test_audio_energy_none_when_unparseable(monkeypatch):
    monkeypatch.setattr(signals.subprocess, "run", _fake_run("no volume info here"))
    assert signals.audio_energy("m.mp4", 1.0, 4.0) is None


def test_scene_density_counts_cuts_per_second(monkeypatch):
    monkeypatch.setattr(signals.subprocess, "run",
                        _fake_run("pts_time:1.0\npts_time:2.0\npts_time:3.0\n"))   # 3 cuts over 6s
    assert signals.scene_density("m.mp4", 0.0, 6.0) == 0.5


# --- annotate ----------------------------------------------------------------

def test_annotate_attaches_text_features_always_and_skips_media_when_absent():
    cands = [{"start": 0.0, "end": 3.0}]
    words = [{"w": "Why", "start": 0.1, "end": 0.4}, {"w": "though", "start": 0.5, "end": 0.9}]
    out = signals.annotate(cands, words=words)            # no media_path
    feats = out[0]["features"]
    assert feats["text"]["is_question"] is True           # "Why ..." sliced from the window
    assert "audio" not in feats and "scene_density" not in feats


def test_annotate_adds_media_signals_when_media_present(monkeypatch):
    monkeypatch.setattr(signals, "audio_energy", lambda *a, **k: {"mean_db": -20, "max_db": -2, "dynamic_db": 18})
    monkeypatch.setattr(signals, "scene_density", lambda *a, **k: 0.4)
    cands = [{"start": 0.0, "end": 5.0}]
    out = signals.annotate(cands, words=[], media_path="m.mp4")
    assert out[0]["features"]["audio"]["dynamic_db"] == 18
    assert out[0]["features"]["scene_density"] == 0.4


def test_window_text_slices_to_the_candidate_window():
    words = [{"w": "before", "start": 0.0, "end": 0.5},
             {"w": "inside", "start": 2.0, "end": 2.4},
             {"w": "gone", "start": 2.5, "end": 2.7, "deleted": True},
             {"w": "after", "start": 9.0, "end": 9.4}]
    assert signals._window_text(words, 1.0, 5.0) == "inside"   # window + visibility filter
