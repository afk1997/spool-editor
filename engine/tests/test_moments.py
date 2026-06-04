"""Tests for clip.moments — LLM moment-finding over words.json.

The LLM provider is always mocked (a CallableProvider wrapping a stub) — no Codex CLI
is invoked. We assert: the transcript + clipify heuristics reach the model, the JSON
reply is parsed (bare / fenced / with preamble), candidates are shaped + validated, and
the windowing / count / source_id / offline contracts hold.
"""
from __future__ import annotations

import json

import pytest

from clip import llm, moments, signals


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
        {"start": 50.0, "end": 999.0, "title": "runs past end", "why": "x"},  # clamp end→duration (then 10s, no trim)
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


# ---- clip tightness: produced clips must land in the short-form sweet spot, not topic spans ----
#
# The moment-finder occasionally returns long topic-length spans (40–140 s). A deterministic
# trim pass pulls any over-long candidate into ~10–30 s, keeping the hook (the start) and ending
# on a clean transcript-line boundary so the clip is genuinely short-form.

def test_find_moments_tightens_overlong_spans(words_json):
    # The model returns the whole 58 s topic; it must be trimmed into the sweet spot.
    resp = json.dumps([{"start": 0.0, "end": 58.0, "title": "whole topic", "why": "x", "signals": ["hook"]}])
    out = moments.find_moments(words_json, provider=_provider(resp))
    assert out[0]["start"] == 0.0                          # hook (the start) preserved
    assert out[0]["end"] - out[0]["start"] <= 30.0         # trimmed into the short-form sweet spot
    assert out[0]["end"] == 27.8                           # last clean line-end within 30 s of the start


def test_find_moments_keeps_well_sized_spans(words_json):
    resp = json.dumps([{"start": 2.0, "end": 24.0, "title": "good clip", "why": "x"}])
    out = moments.find_moments(words_json, provider=_provider(resp))
    assert out[0]["start"] == 2.0 and out[0]["end"] == 24.0   # 22 s — already short-form, untouched


def test_tighten_to_window_hard_caps_when_no_clean_boundary():
    # One giant unbroken line and no boundary in range → hard-cap at target_max (still short-form).
    lines = [(0.0, 200.0, "one massive unbroken line")]
    c = {"start": 0.0, "end": 200.0, "title": "t"}
    out = moments._tighten_to_window(c, lines, target_max=30.0)
    assert out["end"] == 30.0 and out["start"] == 0.0


def test_prompt_demands_short_self_contained_windows(words_json):
    cap = {}
    moments.find_moments(words_json, provider=_provider(_TWO, cap))
    blob = ((cap["system"] or "") + cap["prompt"]).lower()
    assert "10–30" in blob or "10-30" in blob or "30 second" in blob   # a firm short-form target
    assert "a little longer is fine" not in blob                       # the hedge that invited topic spans is gone


# ---- rank: glass-box opportunity score (Phase 3 — spec §5 / §4 discover.rank) ----
#
# rank() scores ON the signals already attached by signals.annotate (the candidate's
# ``features`` dict + the LLM ``signals`` list) — it never re-extracts them. The score is
# a TRANSPARENT linear combination of five named, reweightable factors, so every point
# traces to a visible factor (spec §6.6 glass-box rule). No LLM, no media here — pure.

_FACTOR_KEYS = {"hook", "self_contained", "arc", "energy", "length_fit"}


def _rank_cand(start, end, *, cues=None, text="", audio=None, scene_density=None):
    """A candidate shaped like signals.annotate output (features attached)."""
    feats = {"text": signals.text_signals(text, duration=float(end) - float(start))}
    if audio is not None:
        feats["audio"] = audio
    if scene_density is not None:
        feats["scene_density"] = scene_density
    return {"start": float(start), "end": float(end), "title": "t", "rationale": "r",
            "mode": "funny", "signals": list(cues or []), "features": feats}


def test_rank_attaches_named_factors_weights_and_score():
    [c] = moments.rank([_rank_cand(0, 18, cues=["hook"], text="Why does this keep happening?!")])
    assert set(c["factors"]) == _FACTOR_KEYS                  # the five named factors
    assert all(0.0 <= v <= 1.0 for v in c["factors"].values())
    assert 0.0 <= c["score"] <= 100.0
    assert set(c["weights"]) == _FACTOR_KEYS                  # a visible weight per factor


def test_rank_score_is_transparent_weighted_sum():
    # score == 100 * Σ(factor · normalized-weight) — no opaque model. This is the contract
    # the studio's instant-reweight slider mirrors client-side.
    w = {"hook": 2, "self_contained": 1, "arc": 1, "energy": 1, "length_fit": 1}
    [c] = moments.rank([_rank_cand(0, 18, cues=["hook"], text="What a story, 3 times!")], weights=w)
    tot = sum(w.values())
    expected = 100.0 * sum(c["factors"][k] * (w[k] / tot) for k in c["factors"])
    assert c["score"] == round(expected, 1)


def test_rank_only_energy_weight_isolates_the_energy_factor():
    # Normalization + isolation: weight only energy → score == 100 * the energy factor.
    [c] = moments.rank(
        [_rank_cand(0, 18, text="hello there", audio={"mean_db": -24, "max_db": -2, "dynamic_db": 22})],
        weights={"energy": 1},
    )
    assert c["score"] == round(100.0 * c["factors"]["energy"], 1)


def test_rank_sorts_by_score_descending():
    flat = _rank_cand(0, 4, text="um uh you know like basically")           # weak + bad length
    strong = _rank_cand(0, 18, cues=["hook", "punchline"], text="Why does this keep happening?!")
    ranked = moments.rank([flat, strong])
    assert ranked[0]["start"] == strong["start"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rank_factors_are_reweightable_and_change_order():
    high_energy = _rank_cand(0, 18, text="and then we walked over there",
                             audio={"mean_db": -20, "max_db": -1, "dynamic_db": 34},
                             scene_density=2.0)                              # loud/dynamic, no hook
    high_hook = _rank_cand(0, 18, cues=["hook", "punchline"],
                           text="Why does this keep happening to me?!")      # hooky, quiet
    default = moments.rank([high_energy, high_hook])
    assert default[0]["start"] == high_hook["start"]                        # default leans on hook
    energy_first = moments.rank([high_energy, high_hook], weights={"energy": 1})
    assert energy_first[0]["start"] == high_energy["start"]                 # reweight → energy wins


def test_rank_length_fit_prefers_short_form_window():
    fit = lambda dur: moments.rank(
        [_rank_cand(0, dur, text="a clean self contained thought")])[0]["factors"]["length_fit"]
    assert fit(18) > fit(4)      # too short to land
    assert fit(18) > fit(90)     # too long for a short-form clip


def test_rank_audio_signals_raise_the_energy_factor():
    # The Phase-3 differentiator: NON-text signals (audio) feed the score, not text alone.
    quiet = moments.rank([_rank_cand(0, 18, text="and then we walked over there")])[0]
    loud = moments.rank([_rank_cand(0, 18, text="and then we walked over there",
                                    audio={"mean_db": -23, "max_db": -1, "dynamic_db": 30})])[0]
    assert loud["factors"]["energy"] > quiet["factors"]["energy"]


def test_rank_handles_candidate_without_features():
    # signals.annotate is best-effort; a candidate can arrive with no features at all. rank
    # must still score it (length-fit is always computable from start/end) and never raise.
    bare = {"start": 0.0, "end": 18.0, "title": "t", "mode": "funny", "signals": []}
    [c] = moments.rank([bare])
    assert set(c["factors"]) == _FACTOR_KEYS
    assert c["factors"]["length_fit"] > 0.0
    assert 0.0 <= c["score"] <= 100.0
