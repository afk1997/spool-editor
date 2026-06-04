"""Moment finding + ranking over a transcript (spec §5 P1 / §4 ``discover.*``).

Reads trove's ``words.json`` and proposes self-contained clip candidates with a
rationale, using a moment-finding LLM. The LLM is a **pluggable provider** (see
:mod:`clip.llm`); the default is the *codex bridge* (the user's ChatGPT/Codex
subscription via the Codex CLI — no API key, no local GPU). Only transcript *text*
leaves the machine; offline-mode disables the egress provider.

The prompt reuses the Step-1 moment-finding heuristics from **clipify** by Louise de
Sadeleer (MIT) — punchlines/reactions, reversals, awkward pauses, quotable one-liners,
audio peaks — generalized to Spool's content modes.

Phase 1: :func:`find_moments`. Phase 3: :func:`rank` + the glass-box opportunity score.
"""
from __future__ import annotations

import json
import re

import transcript_io

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
) -> list[dict]:
    """Scan a transcript for ``count`` clip-worthy moments.

    ``mode`` tunes the prompt/signals (funny / insightful / hot-take / story / how-to /
    q&a). ``transcript_window`` scopes the search to ``(start, end)`` seconds. ``provider``
    selects the LLM (a name, an :class:`~clip.llm.LLMProvider` instance for the injected
    agent LLM, or ``None`` for the configured default = the codex bridge).

    Returns candidates ``[{start, end, title, rationale, mode, signals, source_id?}]``
    in the model's best-first order. Pure read of ``words.json``; no media is touched.

    Raises ``ValueError`` if the window contains no transcript words or the reply has no
    parseable JSON, and propagates :class:`~clip.llm.OfflineError` /
    :class:`~clip.llm.ProviderUnavailableError` from the provider.
    """
    data = transcript_io.load(words_json_path)
    lines = _transcript_lines(data, transcript_window)
    if not lines:
        where = f" in the window {list(transcript_window)}" if transcript_window else ""
        raise ValueError(f"no transcript words to scan{where}")

    clamp_max = float(data.get("duration") or max(e for _, e, _ in lines))
    system, prompt = _build_prompt(lines, mode=mode, count=count)
    reply = llm.complete(prompt, system=system, provider=provider, env=env)

    out: list[dict] = []
    for item in _parse_array(reply):
        cand = _shape(item, mode=mode, clamp_max=clamp_max, source_id=source_id)
        if cand is not None:
            out.append(cand)
            if len(out) >= count:
                break
    return out


def rank(candidates: list[dict], *, weights: dict[str, float] | None = None) -> list[dict]:
    """Attach a glass-box opportunity score to each candidate and sort best-first.

    The score is a **transparent** linear combination of five named, reweightable factors
    (``hook`` / ``self_contained`` / ``arc`` / ``energy`` / ``length_fit``), each in ``[0, 1]``::

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

#: The five named factors, in display order. Visible + reweightable (spec §5 Phase 3).
RANK_FACTORS = ("hook", "self_contained", "arc", "energy", "length_fit")

#: Default factor weights (sum to 1). What makes a short-form clip land: a strong open and a
#: self-contained thought first, then energy/arc, with length a gentle nudge. The studio
#: reweight panel and ``discover.rank`` pass a ``weights`` override.
DEFAULT_WEIGHTS = {
    "hook": 0.30,
    "self_contained": 0.25,
    "energy": 0.20,
    "arc": 0.15,
    "length_fit": 0.10,
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
    plus how close the peak sits to 0 dBFS. Absent audio (text-only candidate) → ``0``."""
    if not audio:
        return 0.0
    dyn = _clip01(float(audio.get("dynamic_db", 0.0)) / 25.0)
    loud = _clip01((float(audio.get("max_db", -60.0)) + 30.0) / 30.0)   # -30 dB → 0, 0 dB → 1
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
    """The five named factors in ``[0, 1]`` for one candidate, from its attached signals."""
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
    }
    return {k: round(_clip01(v), 4) for k, v in factors.items()}


# --- transcript → prompt -------------------------------------------------

def _transcript_lines(data: dict, window: tuple[float, float] | None) -> list[tuple[float, float, str]]:
    """Timestamped ``(start, end, text)`` lines, segment-grouped, deletions excluded.

    Word timing is authoritative (``transcript_io``), so each line's start/end is the
    first/last *visible* word — tighter and more accurate than the nominal segment bounds.
    """
    words = data.get("words") or []
    by_idx = {w["idx"]: w for w in words if isinstance(w.get("idx"), int)}

    def visible(w):
        return bool(
            w and not w.get("deleted") and (w.get("w") or "").strip()
            and w.get("start") is not None and w.get("end") is not None
        )

    def in_window(s, e):
        return window is None or (e > window[0] and s < window[1])

    def line_from(vis):
        s = min(w["start"] for w in vis)
        e = max(w["end"] for w in vis)
        return (s, e, " ".join((w["w"] or "").strip() for w in vis)) if in_window(s, e) else None

    lines: list[tuple[float, float, str]] = []
    segments = data.get("segments") or []
    if segments:
        for seg in segments:
            vis = [by_idx[i] for i in (seg.get("word_idxs") or []) if i in by_idx and visible(by_idx[i])]
            if vis and (ln := line_from(vis)):
                lines.append(ln)
    else:  # no paragraphs (rare) — chunk the visible words into ~sentence-sized lines
        flat = [w for w in words if visible(w)]
        for i in range(0, len(flat), 12):
            if ln := line_from(flat[i:i + 12]):
                lines.append(ln)
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
        "- It is roughly 10–25 seconds long (a little longer is fine if the moment needs it)."
    )
    transcript = "\n".join(f"[{s:.2f}–{e:.2f}] {t}" for s, e, t in lines)
    prompt = (
        "Transcript (each line is [start–end in seconds] text):\n\n"
        f"{transcript}\n\n"
        f"Find the {count} best moments. Use the exact timestamps above to choose start and "
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
