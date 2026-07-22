"""Moment finding + ranking over a transcript (spec §5 P1 / §4 ``discover.*``).

Reads trove's ``words.json`` and proposes self-contained clip candidates with a
rationale, using a moment-finding LLM. Remote reasoning is unavailable in Phase 0;
the only executable path is an explicitly injected deterministic provider whose
``egress`` metadata is the literal ``False``.

The prompt reuses the Step-1 moment-finding heuristics from **clipify** by Louise de
Sadeleer (MIT) — punchlines/reactions, reversals, awkward pauses, quotable one-liners,
audio peaks — generalized to Spool's content modes.

Phase 1: :func:`find_moments`. Phase 3: :func:`rank` + the glass-box opportunity score.
"""
from __future__ import annotations

import json
import re

import transcript_io
from network_policy import NetworkPolicy

from . import llm

# Candidate shape (engine snake_case; mirrors the §3 ``Candidate`` / packages/types):
#   {start, end, title, rationale, mode, signals: [...], source_id?, score?}
# ``score`` is added later by :func:`rank` (Phase 3); ``find_moments`` keeps the
# model's best-first order.

_CLIPIFY_CREDIT = "(Moment-finding heuristics adapted from clipify by Louise de Sadeleer, MIT.)"

# mode → (descriptor, signal bullets). ``funny`` is clipify's Step-1 list; the others
# generalize the same idea to Spool's content modes (spec §5 P3 content-type modes).
_MODE_GUIDES = {
    "funny": ("funniest", [
        'Punchlines & reactions — "what", "wait", "no way", laughter, swearing, sharp reactions.',
        "Reversals — a setup or question followed by an unexpected answer.",
        'Awkward pauses — a long gap or filler ("uh", "um").',
        "Quotable one-liners — short declarative sentences that stand alone.",
        "Audio peaks / rapid back-and-forth.",
    ]),
    "insightful": ("most insightful", [
        "A crisp idea, framework, or mental model stated plainly.",
        "A counter-intuitive claim, or a common myth being corrected.",
        "A concrete example or number that makes an abstract point land.",
        "Quotable one-liners that stand alone without context.",
    ]),
    "hot-take": ("boldest hot-take", [
        "A strong, contrarian, or controversial opinion stated with conviction.",
        '"Most people think X, but actually Y" reversals.',
        "A confident prediction or a blunt callout.",
        "Quotable one-liners that provoke a reaction.",
    ]),
    "story": ("best self-contained story", [
        "A clear arc — setup, tension, payoff — inside the window.",
        "A vivid anecdote with a beginning and an end.",
        "An emotional beat or a turning point.",
    ]),
    "how-to": ("clearest how-to / explainer", [
        "A self-contained tip, step, or actionable instruction.",
        "A clean cause→effect or problem→solution explanation.",
        "A concrete example that demonstrates the method.",
    ]),
    "q&a": ("sharpest question-and-answer", [
        "A clear question immediately followed by a punchy answer.",
        "A reversal — an unexpected answer to a simple question.",
        "A quotable takeaway from the exchange.",
    ]),
}
_GENERIC_GUIDE = ("most clip-worthy", [
    "A self-contained moment that needs no external context.",
    "A strong hook in the first few seconds.",
    "A quotable one-liner, a reversal, or a memorable reaction.",
])


def find_moments(
    words_json_path: str,
    *,
    mode: str = "funny",
    count: int = 5,
    transcript_window: tuple[float, float] | None = None,
    source_id: str | None = None,
    provider: "str | llm.LLMProvider | None" = None,
    env: dict | None = None,
    network_policy: NetworkPolicy | None = None,
    privacy_state: llm.PrivacyState | None = None,
) -> list[dict]:
    """Scan a transcript for ``count`` clip-worthy moments.

    ``mode`` tunes the prompt/signals (funny / insightful / hot-take / story / how-to /
    q&a). ``transcript_window`` scopes the search to ``(start, end)`` seconds. ``provider``
    selects the LLM. Phase 0 accepts only an explicitly injected non-egress
    :class:`~clip.llm.LLMProvider`; the named/default ``none`` provider is unavailable.

    Returns candidates ``[{start, end, title, rationale, mode, signals, source_id?}]``
    in the model's best-first order. Pure read of ``words.json``; no media is touched.

    Raises ``ValueError`` if the window contains no transcript words or the reply has no
    parseable JSON, and propagates
    :class:`~clip.llm.RemoteReasoningUnavailableError` or
    :class:`~clip.llm.ProviderUnavailableError` from the provider boundary.
    """
    resolved_provider = llm.get_provider(
        provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )
    if isinstance(resolved_provider, llm.NoneProvider):
        raise llm.RemoteReasoningUnavailableError()

    data = transcript_io.load(words_json_path)
    lines = _transcript_lines(data, transcript_window)
    if not lines:
        where = f" in the window {list(transcript_window)}" if transcript_window else ""
        raise ValueError(f"no transcript words to scan{where}")

    clamp_max = float(data.get("duration") or max(e for _, e, _ in lines))
    words = data.get("words") or []
    system, prompt = _build_prompt(lines, mode=mode, count=count)
    reply = llm.complete(
        prompt,
        system=system,
        provider=resolved_provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )

    out: list[dict] = []
    for item in _parse_array(reply):
        cand = _shape(item, mode=mode, clamp_max=clamp_max, source_id=source_id)
        if cand is not None:
            snapped = _tighten_to_window(cand, lines, words, clamp_max=clamp_max)
            if snapped is not None:   # None → too close to the media end to make a >=_MIN_CLIP clip
                snapped = _snap_start_to_window(snapped, lines, words)   # open on a sentence too
            if snapped is not None:
                out.append(snapped)
                if len(out) >= count:
                    break
    return out


# Snap candidate ends into the short-form sweet spot AND onto a natural speech boundary. The
# moment-finder's ``end`` is imprecise (LLM timestamps drift, so clips "end abruptly" mid-word
# or mid-sentence) and it occasionally proposes whole-topic spans (40–140 s). This deterministic
# pass (no extra LLM call) keeps the hook (the start) and pulls the end back to the nearest
# sentence boundary within range, with a small tail pad so the last word's audio isn't clipped.
_TARGET_MAX = 30.0          # cap at the top of the length_fit plateau
_MIN_CLIP = 10.0            # never end a clip sooner than this — it needs room to land
_END_SNAP_TOLERANCE = 2.5   # search this far (s) either side of the proposed end for a boundary
_TAIL_PAD = 0.180           # keep this much audio past the last word so it isn't clipped
_SENTENCE_END = (".", "?", "!", "…")   # strong boundary — a complete thought
_CLAUSE_END = (",", ";", ":")               # soft boundary — a clause break (beats mid-word)
_CLOSERS = "\"')]}”’»"        # trailing quotes/brackets to look past for punctuation


def _tighten_to_window(cand: dict, lines, words=None, *, target_max: float = _TARGET_MAX,
                       clamp_max: float | None = None, tolerance: float = _END_SNAP_TOLERANCE,
                       tail_pad: float = _TAIL_PAD) -> dict | None:
    """Snap ``cand``'s end onto a natural speech boundary inside ``[start+_MIN_CLIP,
    start+target_max]`` so clips never end mid-utterance or mid-word.

    Within ``tolerance`` seconds of the (length-clamped) proposed end, preference is:
      1. a sentence end — a word ending in ``. ? ! …`` (nearest wins);
      2. a clause end — a word ending in ``, ; :`` (nearest wins);
      3. otherwise the nearest clean word end in range (never a mid-word cut);
      4. and when no per-word timing is available at all, the latest transcript-line end (legacy).
    The chosen end gets a ``tail_pad`` so the final word isn't clipped, clamped to the next word's
    start, the source end, and ``start+target_max``.

    Note: clipify's "largest silence gap" idea is intentionally NOT used — whisper.cpp word
    timings are contiguous (each word's end == the next word's start), so there are no usable
    gaps to snap to; punctuation is the reliable boundary signal on this data.

    Returns the updated candidate, or ``None`` when the source can't yield a ``>= _MIN_CLIP``
    clip from this start (the candidate sits too close to the end of the media)."""
    start, proposed = float(cand["start"]), float(cand["end"])
    source_end = _source_end(lines, words, proposed, clamp_max)
    min_end = start + _MIN_CLIP
    max_end = min(start + float(target_max), source_end)
    if max_end < min_end:
        return None
    anchor = _clamp(proposed, min_end, max_end)

    boundaries = _word_end_boundaries(words or [], min_end, max_end)
    if boundaries:
        lo, hi = max(min_end, anchor - tolerance), min(max_end, anchor + tolerance)
        near = [b for b in boundaries if lo <= b["end"] <= hi]
        sentence = [b for b in near if _ends_with(b["text"], _SENTENCE_END)]
        clause = [b for b in near if _ends_with(b["text"], _CLAUSE_END)]
        if sentence:
            chosen, kind = min(sentence, key=lambda b: abs(b["end"] - anchor)), "sentence"
        elif clause:
            chosen, kind = min(clause, key=lambda b: abs(b["end"] - anchor)), "clause"
        elif near:
            chosen, kind = min(near, key=lambda b: abs(b["end"] - anchor)), "word"   # nearest clean word end
        else:
            chosen, kind = min(boundaries, key=lambda b: abs(b["end"] - anchor)), "word"   # one long word run
        boundary_at = chosen["end"]
        end = _pad_word_end(chosen, max_end=max_end, source_end=source_end, tail_pad=tail_pad)
    else:
        end = _line_boundary_end(lines, min_end, max_end)   # legacy: no per-word timing
        kind, boundary_at = "line", end

    out = dict(cand)
    out["end"] = round(_clamp(end, min_end, max_end), 3)
    _set_boundary(out, "end", kind, proposed, out["end"], boundary_at)
    return out


def _visible_words(words) -> list[dict]:
    """Non-deleted words with valid ``start < end``, sorted by time. Defensive: the model
    output / editor state can carry deletions, blanks, or malformed timings."""
    out = []
    for w in words or []:
        if w.get("deleted") or not (w.get("w") or "").strip():
            continue
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        out.append({"start": s, "end": e, "w": w.get("w") or ""})
    return sorted(out, key=lambda w: (w["start"], w["end"]))


def _word_end_boundaries(words, min_end: float, max_end: float) -> list[dict]:
    """Candidate end points: every visible word end in ``[min_end, max_end]``, tagged with its
    token text (for punctuation) and the next word's start (for tail-pad clamping)."""
    vis = _visible_words(words)
    out = []
    for i, w in enumerate(vis):
        if min_end <= w["end"] <= max_end:
            out.append({"end": w["end"], "text": w["w"],
                        "next_start": vis[i + 1]["start"] if i + 1 < len(vis) else None})
    return out


def _ends_with(text: str, suffixes: tuple[str, ...]) -> bool:
    return str(text or "").strip().rstrip(_CLOSERS).endswith(suffixes)


def _pad_word_end(boundary: dict, *, max_end: float, source_end: float, tail_pad: float) -> float:
    end = float(boundary["end"])
    padded = min(end + max(0.0, float(tail_pad)), max_end, source_end)
    nxt = boundary.get("next_start")
    if nxt is not None and float(nxt) > end:
        padded = min(padded, float(nxt))   # don't bleed into the next word
    return max(end, padded)


def _line_boundary_end(lines, min_end: float, max_end: float) -> float:
    """Legacy fallback when no per-word timing is available: the latest transcript-line end in
    range, else a hard cap at ``max_end``."""
    ends = []
    for _ls, le, _t in lines or []:
        try:
            le = float(le)
        except (TypeError, ValueError):
            continue
        if min_end <= le <= max_end:
            ends.append(le)
    return max(ends) if ends else max_end


def _source_end(lines, words, fallback: float, clamp_max: float | None) -> float:
    if clamp_max is not None:
        return max(0.0, float(clamp_max))
    ends = [float(fallback)]
    ends.extend(w["end"] for w in _visible_words(words or []))
    for _ls, le, _t in lines or []:
        try:
            ends.append(float(le))
        except (TypeError, ValueError):
            pass
    return max(ends)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# --- start-boundary snapping: mirror the end snap so the hook doesn't begin mid-thought ---
_START_SNAP_TOLERANCE = _END_SNAP_TOLERANCE


def _snap_start_to_window(cand: dict, lines, words=None, *, target_max: float = _TARGET_MAX,
                          clamp_min: float = 0.0, tolerance: float = _START_SNAP_TOLERANCE) -> dict | None:
    """Snap ``cand``'s start onto a sentence start (the first word after a sentence end), else a
    clause start, else the nearest clean word start — within ``tolerance`` of the proposed start —
    so the hook opens on a complete thought. Runs AFTER the end snap (it needs the final end to keep
    ``end - start >= _MIN_CLIP``). Returns the updated candidate, or ``None`` if it can't keep a
    ``>= _MIN_CLIP`` clip."""
    start, end = float(cand["start"]), float(cand["end"])
    min_start = max(float(clamp_min), end - float(target_max))
    max_start = end - _MIN_CLIP
    if max_start < min_start:
        return None
    anchor = _clamp(start, min_start, max_start)

    boundaries = _word_start_boundaries(words or [], min_start, max_start)
    chosen = None
    if boundaries:
        lo, hi = max(min_start, anchor - tolerance), min(max_start, anchor + tolerance)
        near = [b for b in boundaries if lo <= b["start"] <= hi]
        for kind in ("sentence", "clause", "word"):
            opts = [b for b in near if b["kind"] == kind]
            if opts:
                chosen = min(opts, key=lambda b: abs(b["start"] - anchor))
                break
    else:
        chosen = _line_boundary_start(lines, min_start, max_start, anchor, tolerance)

    out = dict(cand)
    if chosen is not None:
        out["start"] = round(float(chosen["start"]), 3)
        _set_boundary(out, "start", chosen["kind"], start, out["start"], chosen["start"])
    else:
        out["start"] = round(anchor, 3)
        _set_boundary(out, "start", "none", start, out["start"], None)
    if float(out["end"]) - float(out["start"]) < _MIN_CLIP:
        return None
    return out


def _word_start_boundaries(words, min_start: float, max_start: float) -> list[dict]:
    """Word starts in ``[min_start, max_start]``, each tagged: ``sentence`` (first word, or the word
    after a sentence end), ``clause`` (after a clause end), else ``word``."""
    vis = _visible_words(words)
    out = []
    for i, w in enumerate(vis):
        prev = vis[i - 1]["w"] if i else ""
        if i == 0 or _ends_with(prev, _SENTENCE_END):
            kind = "sentence"
        elif _ends_with(prev, _CLAUSE_END):
            kind = "clause"
        else:
            kind = "word"
        if min_start <= w["start"] <= max_start:
            out.append({"start": w["start"], "kind": kind, "text": w["w"]})
    return out


def _line_boundary_start(lines, min_start: float, max_start: float, anchor: float, tolerance: float):
    """Legacy fallback (no per-word timing): the transcript-line start nearest the anchor, in range."""
    starts = [{"start": float(ls), "kind": "line"} for ls, _le, _t in (lines or [])
              if _is_num(ls) and min_start <= float(ls) <= max_start]
    lo, hi = max(min_start, anchor - tolerance), min(max_start, anchor + tolerance)
    near = [b for b in starts if lo <= b["start"] <= hi]
    return min(near, key=lambda b: abs(b["start"] - anchor)) if near else None


def _is_num(x) -> bool:
    return isinstance(x, (int, float))


def _set_boundary(cand: dict, side: str, kind: str, proposed: float, actual: float,
                  boundary_time: float | None) -> None:
    """Record on ``cand['boundary'][side]`` which kind of boundary the snap landed on (sentence /
    clause / word / line / none) — read by the ``boundary_quality`` ranking factor. Survives
    :func:`signals.annotate` because it lives at the top level, not under ``features``."""
    meta = dict(cand.get("boundary") or {})
    item = {"kind": kind, "snapped": abs(float(actual) - float(proposed)) > 0.001, "time": round(float(actual), 3)}
    if boundary_time is not None:
        item["boundary_time"] = round(float(boundary_time), 3)
    meta[side] = item
    cand["boundary"] = meta


def rank(candidates: list[dict], *, weights: dict[str, float] | None = None) -> list[dict]:
    """Attach a glass-box opportunity score to each candidate and sort best-first.

    The score is a **transparent** linear combination of six named, reweightable factors
    (``hook`` / ``self_contained`` / ``arc`` / ``energy`` / ``length_fit`` / ``boundary_quality``), each in ``[0, 1]``::

        score = 100 · Σ(factorₖ · normalized-weightₖ)

    so every point traces to a visible factor — never an opaque 0–99 (spec §6.6 glass-box
    rule). Factors are derived from the signals :func:`clip.signals.annotate` already attached
    (the candidate's ``features`` dict + the LLM ``signals`` cues); ``rank`` scores **on** them
    and never re-extracts. ``weights`` overrides :data:`DEFAULT_WEIGHTS` (missing/zero factors
    drop out; an all-zero vector falls back to the defaults). Each candidate gains ``factors``,
    ``weights`` (the effective, normalized weights), and ``score``. Mutates + returns the list.
    """
    eff = _normalized_weights(weights)
    for cand in candidates:
        factors = _candidate_factors(cand)
        cand["factors"] = factors
        cand["weights"] = eff
        cand["score"] = round(100.0 * sum(factors[k] * eff[k] for k in RANK_FACTORS), 1)
    # stable sort → ties keep the model's best-first order
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


# --- glass-box ranking ---------------------------------------------------

#: The six named factors, in display order. Visible + reweightable (spec §5 Phase 3).
RANK_FACTORS = ("hook", "self_contained", "arc", "energy", "length_fit", "boundary_quality")

#: Default factor weights (sum to 1). What makes a short-form clip land: a strong open and a
#: self-contained thought first, then energy/arc, with length a gentle nudge. ``boundary_quality``
#: is a light 5% tie-breaker — it rewards clips that start AND end on a real sentence boundary
#: (after the deterministic snap, ends are almost always clean, so it mostly discriminates on the
#: start). The studio reweight panel and ``discover.rank`` pass a ``weights`` override.
DEFAULT_WEIGHTS = {
    "hook": 0.30,
    "self_contained": 0.25,
    "energy": 0.20,
    "arc": 0.15,
    "length_fit": 0.05,
    "boundary_quality": 0.05,
}

# LLM ``signals`` cues that imply each factor (case-insensitive substring match). The
# moment-finder already tags moments with these; ranking reads them as glass-box inputs.
_HOOK_CUES = ("hook", "punchline", "reaction", "reversal", "question", "quotable",
              "one-liner", "one liner", "callout", "prediction", "hot-take", "hot take")
_ARC_CUES = ("reversal", "story", "arc", "payoff", "setup", "tension", "anecdote", "turning")
_SELF_CUES = ("quotable", "one-liner", "one liner", "self-contained", "self contained",
              "story", "takeaway", "complete")


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _b(flag) -> float:
    return 1.0 if flag else 0.0


def _has_cue(cues, vocab) -> bool:
    blob = " ".join(str(s).lower() for s in (cues or []))
    return any(c in blob for c in vocab)


def _audio_term(audio: dict | None) -> float:
    """Loudness+dynamics → ``[0, 1]``: the spread (``dynamic_db`` — a peak over a quiet bed)
    plus how close the peak sits to 0 dBFS. Absent audio (text-only candidate) → ``0``.

    When ``rel_db`` is present (loudness vs the in-video baseline — :func:`signals.annotate`), it
    drives most of the term: on calm/talking-head content absolute dB is near-flat across moments
    (mic-gain-dominated), so the relative level is what actually discriminates clip-worthy beats."""
    if not audio:
        return 0.0
    dyn = _clip01(float(audio.get("dynamic_db", 0.0)) / 25.0)
    loud = _clip01((float(audio.get("max_db", -60.0)) + 30.0) / 30.0)   # -30 dB → 0, 0 dB → 1
    rel = audio.get("rel_db")
    if rel is not None:
        rel_t = _clip01((float(rel) + 3.0) / 6.0)   # +3 dB over baseline → 1.0, baseline → 0.5, -3 → 0
        return _clip01(0.40 * dyn + 0.25 * loud + 0.35 * rel_t)
    return _clip01(0.6 * dyn + 0.4 * loud)


def _length_fit(duration: float) -> float:
    """Closeness to the short-form sweet spot: plateau 1.0 in ``[12, 30]`` s, linear ramp to
    0 at ~3 s (too short to land) and ~75 s (too long for a vertical clip)."""
    d = max(0.0, float(duration))
    if 12.0 <= d <= 30.0:
        return 1.0
    if d < 12.0:
        return _clip01((d - 3.0) / 9.0)        # 3 s → 0, 12 s → 1
    return _clip01((75.0 - d) / 45.0)          # 30 s → 1, 75 s → 0


def _normalized_weights(weights: dict | None) -> dict:
    if weights:
        raw = {k: max(0.0, float(weights.get(k, 0.0))) for k in RANK_FACTORS}
        total = sum(raw.values())
    else:
        raw, total = dict(DEFAULT_WEIGHTS), sum(DEFAULT_WEIGHTS.values())
    if total <= 0:   # an all-zero vector would zero every score — fall back to defaults
        raw, total = dict(DEFAULT_WEIGHTS), sum(DEFAULT_WEIGHTS.values())
    return {k: raw[k] / total for k in RANK_FACTORS}


def _candidate_factors(cand: dict) -> dict:
    """The six named factors in ``[0, 1]`` for one candidate, from its attached signals."""
    feats = cand.get("features") or {}
    text = feats.get("text") or {}
    audio = feats.get("audio")
    scene = feats.get("scene_density")
    cues = cand.get("signals") or []
    duration = max(0.0, float(cand.get("end", 0.0)) - float(cand.get("start", 0.0)))

    intensity = float(text.get("intensity") or 0.0)
    filler = float(text.get("filler_ratio") or 0.0)
    wr = text.get("word_rate")
    word_rate = float(wr) if wr is not None else 0.0

    audio_t = _audio_term(audio)
    scene_t = _clip01(float(scene) / 0.5) if scene is not None else 0.0
    intensity_t = _clip01(intensity / 0.15)     # ~1 charged word per 7 → full
    rate_t = _clip01(word_rate / 3.5)           # ~3.5 words/s ≈ brisk delivery
    room_t = _clip01(duration / 15.0)           # enough runway for setup→payoff

    factors = {
        "hook": 0.45 * _b(_has_cue(cues, _HOOK_CUES)) + 0.25 * _b(text.get("is_question"))
                + 0.15 * _b(text.get("exclamation")) + 0.15 * intensity_t,
        "self_contained": 0.55 * (1.0 - filler) + 0.45 * _b(_has_cue(cues, _SELF_CUES)),
        "arc": 0.60 * _b(_has_cue(cues, _ARC_CUES)) + 0.25 * audio_t + 0.15 * room_t,
        "energy": 0.45 * audio_t + 0.25 * intensity_t + 0.15 * rate_t + 0.15 * scene_t,
        "length_fit": _length_fit(duration),
        "boundary_quality": _boundary_quality(cand),
    }
    return {k: round(_clip01(v), 4) for k, v in factors.items()}


#: How clean a snapped boundary is, per kind (set by the start/end snappers via ``_set_boundary``).
_BOUNDARY_KIND_SCORE = {"sentence": 1.0, "clause": 0.65, "word": 0.35, "line": 0.25, "none": 0.0}


def _boundary_quality(cand: dict) -> float:
    """Glass-box factor in ``[0, 1]``: how cleanly the clip starts AND ends on a real boundary,
    from the ``boundary`` metadata the snappers attach. Candidates with no metadata (a direct
    :func:`rank` caller that never ran the snappers) are neutral (``1.0``) — never penalized."""
    meta = cand.get("boundary")
    if not meta:
        return 1.0

    def side(name: str) -> float:
        kind = str((meta.get(name) or {}).get("kind") or "").lower()
        return _BOUNDARY_KIND_SCORE.get(kind, 1.0) if kind else 1.0

    return round(_clip01(side("start") * side("end")), 4)


# --- transcript → prompt -------------------------------------------------

# Sentence-sized lines for the prompt: break on sentence-ending punctuation so the model
# reasons over whole thoughts, with caps (segment end / words / seconds) so a punctuation-sparse
# run never collapses into one giant line.
_LINE_MAX_WORDS = 12
_LINE_MAX_SECONDS = 8.0


def _transcript_lines(data: dict, window: tuple[float, float] | None) -> list[tuple[float, float, str]]:
    """Timestamped ``(start, end, text)`` lines, split into sentences, deletions excluded.

    Lines break at a sentence end (a word ending in ``. ? ! …``), at a whisper-segment end, or
    at a length cap (``_LINE_MAX_WORDS`` / ``_LINE_MAX_SECONDS``) — whichever comes first — so the
    model sees sentence-sized units instead of arbitrary segment spans, and a long punctuation-less
    run still degrades to capped chunks rather than one mega-line. Word timing is authoritative:
    each line's start/end is its first/last *visible* word.
    """
    words = data.get("words") or []
    by_idx = {w["idx"]: w for w in words if isinstance(w.get("idx"), int)}

    def visible(w):
        return bool(
            w and not w.get("deleted") and (w.get("w") or "").strip()
            and w.get("start") is not None and w.get("end") is not None
        )

    def in_window(w):
        return window is None or (float(w["end"]) > window[0] and float(w["start"]) < window[1])

    flat = [w for w in words if visible(w) and in_window(w)]
    flat.sort(key=lambda w: (float(w["start"]), float(w["end"]), int(w.get("idx", 0))))
    if not flat:
        return []

    # Last visible+in-window word of each segment — a secondary (paragraph) break point.
    seg_ends = set()
    for seg in data.get("segments") or []:
        vis = [by_idx[i] for i in (seg.get("word_idxs") or [])
               if i in by_idx and visible(by_idx[i]) and in_window(by_idx[i])]
        if vis:
            seg_ends.add(vis[-1].get("idx"))

    def make_line(buf):
        return (float(buf[0]["start"]), float(buf[-1]["end"]),
                " ".join((w.get("w") or "").strip() for w in buf))

    lines: list[tuple[float, float, str]] = []
    buf: list[dict] = []
    for w in flat:
        buf.append(w)
        span = float(buf[-1]["end"]) - float(buf[0]["start"])
        if (_ends_with(w.get("w") or "", _SENTENCE_END) or w.get("idx") in seg_ends
                or len(buf) >= _LINE_MAX_WORDS or span >= _LINE_MAX_SECONDS):
            lines.append(make_line(buf))
            buf = []
    if buf:
        lines.append(make_line(buf))
    return lines


def _build_prompt(lines: list[tuple[float, float, str]], *, mode: str, count: int) -> tuple[str, str]:
    descriptor, bullets = _MODE_GUIDES.get(mode.strip().lower(), _GENERIC_GUIDE)
    system = (
        f"You are an expert short-form clip producer. Find the {descriptor}, self-contained "
        "moments in a video transcript so they can be cut into standalone vertical clips.\n"
        f"{_CLIPIFY_CREDIT}\n\n"
        "Signals to look for:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\n"
        "What makes a good clip:\n"
        "- It stands alone — a natural entry point in, ending on a beat, no dangling context.\n"
        "- It has a hook in the first ~3 seconds.\n"
        "- It is 10–30 seconds long. Pick the single tightest self-contained window — NOT a whole "
        "topic, section, or long back-and-forth. If a passage runs long, choose the most clip-worthy "
        "10–30 s slice of it (clips longer than ~30 s do not perform as short-form vertical video)."
    )
    transcript = "\n".join(f"[{s:.2f}–{e:.2f}] {t}" for s, e, t in lines)
    prompt = (
        "Transcript (each line is [start–end in seconds] text):\n\n"
        f"{transcript}\n\n"
        f"Find the {count} best moments. Each MUST be 10–30 seconds long — prefer several short, "
        "punchy moments over a few long ones. Use the exact timestamps above to choose start and "
        "end (seconds, as decimals). Give each a short, punchy title and a one-sentence reason.\n\n"
        "Return ONLY a JSON array — no prose, no markdown fences — exactly like:\n"
        '[{"start": 12.3, "end": 30.1, "title": "...", "why": "...", "signals": ["punchline"]}]\n'
        '"signals" lists which of the signals above the moment matched.'
    )
    return system, prompt


# --- reply → candidates --------------------------------------------------

def _parse_array(text: str) -> list:
    """Extract a JSON array of moments from a model reply (bare, fenced, or with prose)."""
    text = (text or "").strip()
    for candidate in (text, _strip_fence(text), _slice_brackets(text)):
        if not candidate:
            continue
        try:
            val = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(val, list):
            return val
        if isinstance(val, dict):  # tolerate {"moments": [...]} style wrappers
            for key in ("moments", "clips", "candidates", "results"):
                if isinstance(val.get(key), list):
                    return val[key]
    raise ValueError("could not parse a JSON array of moments from the model reply")


def _strip_fence(text: str) -> str | None:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _slice_brackets(text: str) -> str | None:
    a, b = text.find("["), text.rfind("]")
    return text[a:b + 1] if a != -1 and b > a else None


def _shape(item, *, mode: str, clamp_max: float, source_id: str | None) -> dict | None:
    """Validate + normalize one model item into a Candidate, or ``None`` to drop it."""
    if not isinstance(item, dict):
        return None
    try:
        start = float(item["start"])
        end = min(float(item["end"]), clamp_max)
    except (KeyError, TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None

    signals = item.get("signals") or []
    if isinstance(signals, str):
        signals = [signals]
    signals = [str(s).strip() for s in signals if str(s).strip()]

    cand = {
        "start": round(start, 3),
        "end": round(end, 3),
        "title": (str(item.get("title") or "").strip()) or _auto_title(start),
        "rationale": str(item.get("why") or item.get("rationale") or "").strip(),
        "mode": mode,
        "signals": signals,
    }
    if source_id is not None:
        cand["source_id"] = source_id
    return cand


def _auto_title(start: float) -> str:
    m, s = divmod(int(start), 60)
    return f"Moment at {m}:{s:02d}"
