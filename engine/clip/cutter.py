"""Lossless trim (spec §5 P1 / §4 ``clip.cut``).

A clip is just an in/out range on a source. The cut is an instant stream-copy
(``ffmpeg -c copy``) so it costs ~nothing; re-encode only happens later in reframe.
"""
from __future__ import annotations


def cut(source_path: str, start: float, end: float, out_path: str) -> str:
    """Stream-copy ``[start, end]`` of ``source_path`` to ``out_path``; return ``out_path``.

    Snapping to keyframes / word boundaries is the timeline editor's job (P2); this
    is the deterministic primitive underneath it.
    """
    raise NotImplementedError("Phase 1 — ffmpeg -c copy trim (spec §5 Phase 1, §4 clip.cut)")
