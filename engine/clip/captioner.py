"""Captions: styled ASS from word timestamps, then burn-in (spec §5 P1 / §4 ``caption.*``).

Caption timing needs **no re-transcribe** — we slice trove's ``words.json`` to the
clip's [start, end] range and feed the back-half ASS generator. Because the transcript
is editable upstream, recognition errors are fixed *before* captions are burned (this
is what kills the "~70% of clips need manual cleanup" problem, spec §1.3).

Phase 1: presets (opus / karaoke / minimal). Phase 2: live caption studio + match-from-image.
"""
from __future__ import annotations

from .backhalf import ass_captions  # noqa: F401  (wrapped in the impl)


def generate(
    words_json_path: str,
    *,
    clip_start: float,
    clip_end: float,
    style: str = "opus",
    out_ass_path: str,
) -> str:
    """Slice ``words.json`` to ``[clip_start, clip_end]`` (re-based to t=0) and write a
    styled ASS file via ``backhalf.ass_captions``. ``style`` ∈ {opus, karaoke, minimal}.
    Returns ``out_ass_path``."""
    raise NotImplementedError("Phase 1 — ASS generation from sliced words (spec §4 caption.generate)")


def burn(clip_path: str, ass_path: str, out_path: str) -> str:
    """Burn ``ass_path`` into ``clip_path`` (ffmpeg ``subtitles`` filter); return ``out_path``."""
    raise NotImplementedError("Phase 1 — caption burn-in (spec §4 caption.burn)")
