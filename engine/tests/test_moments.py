"""Tests for clip.moments — LLM moment-finding over words.json.

The LLM provider is always mocked (a CallableProvider wrapping a stub) — no Codex CLI
is invoked. We assert: the transcript + clipify heuristics reach the model, the JSON
reply is parsed (bare / fenced / with preamble), candidates are shaped + validated, and
the windowing / count / source_id / offline contracts hold.
"""
from __future__ import annotations

import json

import pytest

from clip import llm, moments


@pytest.fixture()
def words_json(tmp_path):
    # Six 10s segments, distinctive leading tokens so windowing is observable.
    texts = [
        ("alpha", "alpha", "hello", "world"),
        ("beta", "beta", "the", "question"),
        ("gamma", "gamma", "the", "answer"),
        ("delta", "delta", "no", "way"),
        ("epsilon", "epsilon", "quotable", "line"),
        ("zeta", "zeta", "the", "end"),
    ]
    words, segments, idx = [], [], 0
    for s, toks in enumerate(texts):
        seg_start = s * 10.0
        word_idxs = []
        for j, tok in enumerate(toks):
            ws = seg_start + j * 2.0
            words.append({"idx": idx, "w": tok, "original_w": tok,
                          "start": ws, "end": ws + 1.8, "edited": False, "deleted": False})
            word_idxs.append(idx)
            idx += 1
        segments.append({"start": seg_start, "end": seg_start + 10.0,
                         "text": " ".join(toks), "word_idxs": word_idxs, "speaker": None})
    data = {"schema_version": 2, "language": "en", "duration": 60.0,
            "edited_at": None, "words": words, "segments": segments, "bookmarks": []}
    p = tmp_path / "src.words.json"
    p.write_text(json.dumps(data))
    return str(p)


def _provider(response, capture=None, *, egress=False):
    def fn(prompt, system=None):
        if capture is not None:
            capture["prompt"] = prompt
            capture["system"] = system
        return response
    return llm.CallableProvider(fn, name="fake", egress=egress)


_TWO = json.dumps([
    {"start": 2.0, "end": 14.0, "title": "The setup", "why": "strong hook", "signals": ["punchline"]},
    {"start": 22.0, "end": 35.0, "title": "The reversal", "why": "unexpected answer", "signals": ["reversal"]},
])


def test_find_moments_returns_shaped_candidates(words_json):
    out = moments.find_moments(words_json, provider=_provider(_TWO))
    assert len(out) == 2
    first = out[0]
    assert first["start"] == 2.0 and first["end"] == 14.0
    assert first["title"] == "The setup"
    assert first["rationale"] == "strong hook"
    assert first["mode"] == "funny"
    assert first["signals"] == ["punchline"]
    # order preserved (best-first as the model returned them)
    assert out[1]["title"] == "The reversal"


def test_find_moments_sends_transcript_and_heuristics_to_model(words_json):
    cap = {}
    moments.find_moments(words_json, provider=_provider(_TWO, cap))
    blob = (cap["system"] or "") + "\n" + cap["prompt"]
    assert "hello world" in cap["prompt"]          # transcript text reaches the model
    assert "14.0" in cap["prompt"] or "0.0" in cap["prompt"]  # timestamps present
    assert "punchline" in blob.lower()             # clipify heuristic
    assert "json" in cap["prompt"].lower()         # output contract


def test_find_moments_parses_fenced_json(words_json):
    fenced = f"Here you go:\n```json\n{_TWO}\n```\n"
    out = moments.find_moments(words_json, provider=_provider(fenced))
    assert len(out) == 2 and out[0]["title"] == "The setup"


def test_find_moments_parses_json_with_preamble_and_trailer(words_json):
    noisy = f"Sure! I found these moments:\n{_TWO}\nHope that helps."
    out = moments.find_moments(words_json, provider=_provider(noisy))
    assert len(out) == 2


def test_find_moments_clamps_and_drops_invalid_ranges(words_json):
    resp = json.dumps([
        {"start": 5.0, "end": 999.0, "title": "runs past end", "why": "x"},   # clamp end→duration
        {"start": 30.0, "end": 20.0, "title": "inverted", "why": "x"},        # drop
        {"start": -3.0, "end": 4.0, "title": "negative", "why": "x"},         # drop
    ])
    out = moments.find_moments(words_json, provider=_provider(resp))
    assert len(out) == 1
    assert out[0]["title"] == "runs past end" and out[0]["end"] == 60.0


def test_find_moments_caps_to_count(words_json):
    many = json.dumps([{"start": float(i), "end": float(i) + 5, "title": f"m{i}", "why": "x"} for i in range(8)])
    out = moments.find_moments(words_json, count=3, provider=_provider(many))
    assert len(out) == 3


def test_find_moments_mode_threads_through(words_json):
    cap = {}
    out = moments.find_moments(words_json, mode="insightful", provider=_provider(_TWO, cap))
    assert out[0]["mode"] == "insightful"
    assert "insightful" in ((cap["system"] or "") + cap["prompt"]).lower()


def test_find_moments_attaches_source_id_when_given(words_json):
    out = moments.find_moments(words_json, source_id="src_123", provider=_provider(_TWO))
    assert out[0]["source_id"] == "src_123"


def test_find_moments_omits_source_id_when_absent(words_json):
    out = moments.find_moments(words_json, provider=_provider(_TWO))
    assert "source_id" not in out[0]


def test_find_moments_synthesizes_missing_title(words_json):
    resp = json.dumps([{"start": 62.0 - 60.0, "end": 8.0, "why": "no title here"}])
    # start 2.0 → "0:02"
    out = moments.find_moments(words_json, provider=_provider(resp))
    assert out[0]["title"].startswith("Moment at") and "0:02" in out[0]["title"]


def test_find_moments_empty_array_returns_empty(words_json):
    assert moments.find_moments(words_json, provider=_provider("[]")) == []


def test_find_moments_unparseable_reply_raises(words_json):
    with pytest.raises((ValueError, RuntimeError), match="(?i)json|parse"):
        moments.find_moments(words_json, provider=_provider("I couldn't find anything useful."))


def test_find_moments_respects_transcript_window(words_json):
    cap = {}
    moments.find_moments(words_json, transcript_window=(20.0, 40.0), provider=_provider(_TWO, cap))
    assert "gamma" in cap["prompt"]   # 20–30s segment is in-window
    assert "alpha" not in cap["prompt"]  # 0–10s segment is out of window
    assert "zeta" not in cap["prompt"]   # 50–60s segment is out of window


def test_find_moments_empty_window_raises(words_json):
    with pytest.raises(ValueError, match="(?i)no .*word|transcript"):
        moments.find_moments(words_json, transcript_window=(500.0, 600.0), provider=_provider(_TWO))


def test_find_moments_offline_with_default_codex_raises(words_json):
    # No provider instance → resolves the egress codex bridge → offline guard fires
    # before any CLI is touched.
    with pytest.raises(llm.OfflineError):
        moments.find_moments(words_json, provider="codex", env={"SPOOL_OFFLINE": "1"})
