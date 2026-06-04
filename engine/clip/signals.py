"""Glass-box NON-text signals for moment candidates (spec §6 glass-box rule; item E).

Each signal is a NAMED, explainable number derived from the transcript text or the media —
**never an LLM** (the moment-finder's codex bridge stays the only egress; these run locally).
They attach to a candidate's ``features`` dict so the **Phase-3** glass-box ranking can score on
visible, reweightable inputs. This module lands the *signals*; the ranking/feedback re-rank is
Phase 3 (``moments.rank`` — coordinate, don't double-build).

  text_signals(text)                  — Q&A / sentiment-intensity / numbers / fillers / pace (cheap)
  audio_energy(media, start, end)     — loudness + dynamics over the window (peaks ≈ laughter/emphasis)
  scene_density(media, start, end)    — scene cuts per second (visual dynamism)
  annotate(cands, words=, media_path=) — attach a per-candidate ``features`` dict (text always; media opt-in)
"""
from __future__ import annotations

import re
import subprocess

# Tiny, transparent intensity lexicon — strong/charged words that mark a clip-worthy beat. NOT a
# real sentiment model; a visible heuristic (the glass-box point is you can see exactly what fired).
_INTENSITY = {
    "amazing", "incredible", "insane", "crazy", "unbelievable", "awesome", "best", "worst",
    "love", "hate", "never", "always", "huge", "massive", "terrible", "wrong", "right",
    "shocking", "wild", "ridiculous", "genius", "stupid", "perfect", "favorite", "worst",
    "money", "free", "secret", "mistake", "fail", "win", "die", "kill", "fire",
}
_QUESTION_WORDS = {"what", "why", "how", "when", "where", "who", "which", "is", "are", "do",
                   "does", "did", "can", "could", "should", "would", "will"}
_FILLERS = {"uh", "um", "like", "you", "know", "i", "mean", "sort", "kind", "basically", "literally"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def text_signals(text: str, *, duration: float | None = None) -> dict:
    """Deterministic transcript-text cues for ``text`` (glass-box; no LLM)."""
    toks = _words(text)
    n = len(toks) or 1
    first = toks[0] if toks else ""
    intensity = sum(1 for w in toks if w in _INTENSITY)
    return {
        "is_question": ("?" in (text or "")) or (first in _QUESTION_WORDS),
        "exclamation": "!" in (text or ""),
        "numbers": len(re.findall(r"\b\d[\d,.]*\b", text or "")),
        "intensity": round(intensity / n, 4),          # share of charged words
        "intensity_hits": intensity,
        "filler_ratio": round(sum(1 for w in toks if w in _FILLERS) / n, 4),
        "word_count": len(toks),
        "word_rate": round(len(toks) / duration, 3) if duration and duration > 0 else None,
    }


def audio_energy(media_path: str, start: float, end: float) -> dict | None:
    """Loudness + dynamics over [start, end] via ffmpeg ``volumedetect``: ``{mean_db, max_db,
    dynamic_db}`` (max−mean; a big spread ≈ a laugh/emphasis peak over a quiet bed). ``None`` on
    failure (best-effort — a missing signal must never break moment-finding)."""
    dur = max(0.05, float(end) - float(start))
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}",
             "-i", media_path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        ).stderr
    except Exception:
        return None
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", out)
    peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", out)
    if not mean or not peak:
        return None
    mean_db, max_db = float(mean.group(1)), float(peak.group(1))
    return {"mean_db": round(mean_db, 2), "max_db": round(max_db, 2),
            "dynamic_db": round(max_db - mean_db, 2)}


def scene_density(media_path: str, start: float, end: float, threshold: float = 0.3) -> float | None:
    """Scene cuts per second over [start, end] (ffmpeg scene detection) — visual dynamism / cut
    pace. ``None`` on failure."""
    dur = max(0.05, float(end) - float(start))
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}",
             "-i", media_path, "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        ).stderr
    except Exception:
        return None
    cuts = len(re.findall(r"pts_time:[0-9.]+", out))
    return round(cuts / dur, 4)


def _window_text(words: list[dict], start: float, end: float) -> str:
    """Visible transcript text whose words fall within [start, end] (re-uses the flat word list)."""
    parts = []
    for w in words or []:
        if w.get("deleted"):
            continue
        s, e = w.get("start"), w.get("end")
        if s is None or e is None or e <= start or s >= end:
            continue
        t = (w.get("w") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def annotate(candidates: list[dict], *, words: list[dict] | None = None,
             media_path: str | None = None) -> list[dict]:
    """Attach a named, glass-box ``features`` dict to each candidate (mutates + returns them).

    Text signals are always computed (cheap, from the transcript window). Audio + scene signals
    are computed only when ``media_path`` is given — they each run a short ffmpeg pass per
    candidate, so they're opt-in (the moment-finder stays a pure transcript read by default).
    Every failure degrades to an absent signal, never an error.
    """
    for c in candidates:
        start, end = float(c.get("start", 0)), float(c.get("end", 0))
        text = _window_text(words, start, end) if words else ""
        feats: dict = {"text": text_signals(text, duration=end - start)}
        if media_path:
            ae = audio_energy(media_path, start, end)
            if ae is not None:
                feats["audio"] = ae
            sd = scene_density(media_path, start, end)
            if sd is not None:
                feats["scene_density"] = sd
        c["features"] = feats

    # Relative loudness (glass-box; item I — discriminate on calm/talking-head content): how far
    # each window's level sits above/below the others. Absolute dB is dominated by mic gain and is
    # near-flat across an interview, so the ranking can't tell moments apart; the in-set baseline
    # (median window level) surfaces the real in-video variation a loud beat / laugh produces.
    levels = [c["features"]["audio"]["mean_db"] for c in candidates
              if c.get("features", {}).get("audio") and c["features"]["audio"].get("mean_db") is not None]
    if len(levels) >= 2:
        sl = sorted(levels)
        mid = len(sl) // 2
        # True median window level: average the two middles for even N (the typical produce pool)
        # so the baseline isn't biased toward the upper-middle element.
        baseline = sl[mid] if len(sl) % 2 else (sl[mid - 1] + sl[mid]) / 2.0
        for c in candidates:
            au = c.get("features", {}).get("audio")
            if au and au.get("mean_db") is not None:
                au["rel_db"] = round(float(au["mean_db"]) - baseline, 2)
    return candidates
