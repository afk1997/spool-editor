"""Moment finding + ranking over a transcript (spec §5 P1 / §4 ``discover.*``).

Reads trove's ``words.json`` and proposes self-contained clip candidates with a
rationale, using the moment-finding LLM (local via Ollama/llama.cpp, or the user's
hosted key). Ranking (P3) is a *glass-box* score — named, reweightable factors,
never an opaque 0–99.

Phase 1: ``find_moments``. Phase 3: ``rank`` + content-type modes.
"""
from __future__ import annotations

# Candidate shape (mirrors packages/types Candidate; see spec §3 data model):
#   {sourceId, start, end, title, rationale, mode, signals: {...}, score?}


def find_moments(
    words_json_path: str,
    *,
    mode: str = "funny",
    count: int = 5,
    transcript_window: tuple[float, float] | None = None,
) -> list[dict]:
    """Scan a transcript for ``count`` clip-worthy moments.

    ``mode`` tunes the prompt/signals (funny / insightful / hot-take / story /
    how-to / Q&A). Returns candidates ``[{start, end, title, rationale, ...}]``
    ordered best-first. Pure read of ``words.json``; no media is touched here.
    """
    raise NotImplementedError("Phase 1 — moment finding (spec §5 Phase 1, §4 discover.find_moments)")


def rank(candidates: list[dict], *, weights: dict[str, float] | None = None) -> list[dict]:
    """Attach a glass-box opportunity score (hook, self-containedness, arc, energy,
    length-fit) to each candidate and sort. Factors are visible and reweightable."""
    raise NotImplementedError("Phase 3 — glass-box ranking (spec §5 Phase 3, §4 discover.rank)")
