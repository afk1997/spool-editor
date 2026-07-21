"""Tests for clip.signals — glass-box NON-text moment signals (item E).

text_signals is pure + deterministic (no media). audio_energy / scene_density parse ffmpeg
output, so we mock subprocess; annotate ties them onto candidates' ``features``.
"""
from __future__ import annotations

import types
from pathlib import Path

from clip import signals


def _filesystem_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Capture durable file bytes + mtimes so a read path cannot hide a rewrite."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


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


def test_annotate_attaches_relative_loudness_across_the_set(monkeypatch):
    # Item I: each window gets rel_db = its mean level minus the set's median — so the ranking can
    # see in-video loudness variation that an absolute dB (mic-gain-dominated) hides.
    levels = {0.0: -20.0, 10.0: -26.0, 20.0: -23.0}
    monkeypatch.setattr(signals, "audio_energy",
                        lambda m, s, e: {"mean_db": levels[s], "max_db": levels[s] + 5, "dynamic_db": 5})
    monkeypatch.setattr(signals, "scene_density", lambda *a, **k: 0.0)
    cands = [{"start": 0.0, "end": 5.0}, {"start": 10.0, "end": 15.0}, {"start": 20.0, "end": 25.0}]
    out = signals.annotate(cands, words=[], media_path="m.mp4")
    # median of [-20, -26, -23] is -23 → rel_db = mean − median
    assert out[0]["features"]["audio"]["rel_db"] == 3.0    # loudest, +3 dB over baseline
    assert out[1]["features"]["audio"]["rel_db"] == -3.0   # quietest
    assert out[2]["features"]["audio"]["rel_db"] == 0.0    # at the baseline


def test_annotate_relative_loudness_uses_true_median_for_even_n(monkeypatch):
    # For an even-N pool (the typical produce set), the baseline is the AVERAGE of the two middle
    # levels — not the upper-middle element. Levels [-30, -26, -22, -20] sorted → middles -26/-22
    # → median -24, so rel_db = mean − (-24).
    levels = {0.0: -30.0, 10.0: -26.0, 20.0: -22.0, 30.0: -20.0}
    monkeypatch.setattr(signals, "audio_energy",
                        lambda m, s, e: {"mean_db": levels[s], "max_db": levels[s] + 5, "dynamic_db": 5})
    monkeypatch.setattr(signals, "scene_density", lambda *a, **k: 0.0)
    cands = [{"start": 0.0, "end": 5.0}, {"start": 10.0, "end": 15.0},
             {"start": 20.0, "end": 25.0}, {"start": 30.0, "end": 35.0}]
    out = signals.annotate(cands, words=[], media_path="m.mp4")
    assert out[0]["features"]["audio"]["rel_db"] == -6.0   # -30 − (-24)
    assert out[1]["features"]["audio"]["rel_db"] == -2.0   # -26 − (-24)
    assert out[2]["features"]["audio"]["rel_db"] == 2.0    # -22 − (-24)
    assert out[3]["features"]["audio"]["rel_db"] == 4.0    # -20 − (-24)


def test_rms_db_series_parses_and_floors_silence(monkeypatch):
    class _R:
        stdout = ("lavfi.astats.Overall.RMS_level=-23.5\n"
                  "lavfi.astats.Overall.RMS_level=-inf\n"      # a silent second → floored
                  "noise line ignored\n"
                  "lavfi.astats.Overall.RMS_level=-12.0\n")
    monkeypatch.setattr(signals.subprocess, "run", lambda *a, **k: _R())
    assert signals._rms_db_series("m.mp4") == [-23.5, -120.0, -12.0]


def test_energy_envelope_buckets_and_normalizes(monkeypatch):
    # 8 per-second dB values → 4 bars (mean per pair: -40,-20,-30,-10), min–max normalized to
    # [0.06, 1.0]. start/end given so the cache path is skipped (no disk).
    monkeypatch.setattr(signals, "_rms_db_series",
                        lambda *a, **k: [-40, -40, -20, -20, -30, -30, -10, -10])
    bars = signals.energy_envelope("m.mp4", buckets=4, start=0, end=8)
    assert len(bars) == 4
    assert bars[0] == 0.06 and bars[3] == 1.0          # quietest → floor, loudest → top
    assert bars[1] > bars[2] > bars[0]                 # -20 > -30 > -40 ordering preserved
    monkeypatch.setattr(signals, "_rms_db_series", lambda *a, **k: None)   # no audio
    assert signals.energy_envelope("m.mp4", buckets=4, start=0, end=8) is None


def test_energy_envelope_no_cache_returns_fresh_result_without_filesystem_delta(
    tmp_path, monkeypatch
):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    cache = tmp_path / "source.energy.json"
    cache.write_text('{"db": [-1.0, -1.0]}', encoding="utf-8")
    monkeypatch.setattr(
        signals,
        "_rms_db_series",
        lambda *args, **kwargs: [-40.0, -20.0, -30.0, -10.0],
    )
    before = _filesystem_snapshot(tmp_path)

    bars = signals.energy_envelope(str(media), buckets=4, use_cache=False)

    assert bars == [0.06, 0.6867, 0.3733, 1.0]
    assert _filesystem_snapshot(tmp_path) == before


def test_filmstrip_no_cache_returns_fresh_result_without_filesystem_delta(
    tmp_path, monkeypatch
):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    cache = tmp_path / "source.1.00-5.00-2x48.strip.txt"
    cache.write_text("data:image/jpeg;base64,OLD", encoding="utf-8")

    def fake_run(args, **kwargs):
        Path(args[-1]).write_bytes(b"fresh-jpeg")
        return types.SimpleNamespace(stderr="", stdout="", returncode=0)

    monkeypatch.setattr(signals.subprocess, "run", fake_run)
    before = _filesystem_snapshot(tmp_path)

    strip = signals.filmstrip(str(media), 1.0, 5.0, frames=2, use_cache=False)

    assert strip == "data:image/jpeg;base64,ZnJlc2gtanBlZw=="
    assert _filesystem_snapshot(tmp_path) == before


def test_scene_cuts_offsets_window_relative_times_to_absolute(monkeypatch):
    # -ss makes showinfo pts_time window-relative; scene_cuts adds `start` back to absolute source time.
    class _R:
        stderr = ("[Parsed_showinfo] n:0 pts_time:1.500 type:I\n"
                  "[Parsed_showinfo] n:1 pts_time:4.250 type:P\n")
    monkeypatch.setattr(signals.subprocess, "run", lambda *a, **k: _R())
    assert signals.scene_cuts("m.mp4", start=60.0, end=120.0) == [61.5, 64.25]


def test_window_text_slices_to_the_candidate_window():
    words = [{"w": "before", "start": 0.0, "end": 0.5},
             {"w": "inside", "start": 2.0, "end": 2.4},
             {"w": "gone", "start": 2.5, "end": 2.7, "deleted": True},
             {"w": "after", "start": 9.0, "end": 9.4}]
    assert signals._window_text(words, 1.0, 5.0) == "inside"   # window + visibility filter
