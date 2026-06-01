"""Captions: styled ASS from word timestamps, then burn-in (spec §5 P1 / §4 ``caption.*``).

No re-transcribe: ``generate`` slices trove's ``words.json`` to the clip's
``[clip_start, clip_end]`` window (re-based to t=0) and feeds the vendored ASS
generator (``clip.backhalf.ass_captions``). Because the transcript is editable
upstream, recognition errors are fixed *before* captions are burned (spec §1.3).
``burn`` rasterizes the ASS into the video via ffmpeg's subtitles filter.

Caption timing needs no re-transcribe — we slice the existing words, never re-run whisper.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import _ffmpeg

# The vendored back-half script (kept verbatim, MIT — see THIRD_PARTY_LICENSES.md);
# it's a CLI tool, so we invoke it as one.
_ASS_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "ass_captions.py")
_VALID_STYLES = ("opus", "karaoke", "minimal")
BURN_TIMEOUT = 3600


def generate(
    words_json_path: str,
    *,
    clip_start: float,
    clip_end: float,
    style: str = "opus",
    out_ass_path: str,
) -> str:
    """Slice ``words.json`` to ``[clip_start, clip_end]`` (re-based to 0) and write a
    styled ASS file (opus / karaoke / minimal) via the vendored generator.

    Returns ``out_ass_path``. Raises ``ValueError`` for a bad style, an inverted
    window, or a window containing no words.
    """
    if style not in _VALID_STYLES:
        raise ValueError(f"unknown caption style {style!r}; expected one of {list(_VALID_STYLES)}")
    if clip_end <= clip_start:
        raise ValueError(f"clip_end ({clip_end}) must be greater than clip_start ({clip_start})")

    with open(words_json_path) as f:
        data = json.load(f)

    sliced = []
    for w in data.get("words", []):
        if w.get("deleted"):
            continue
        start, end = w.get("start"), w.get("end")
        if start is None or end is None:
            continue
        # keep any word overlapping the window
        if end <= clip_start or start >= clip_end:
            continue
        text = (w.get("w") or "").strip()
        if not text:
            continue
        sliced.append({
            "start": round(max(0.0, start - clip_start), 3),
            "end": round(max(0.0, min(end, clip_end) - clip_start), 3),
            "word": text,
        })
    if not sliced:
        raise ValueError(f"no words in clip window [{clip_start}, {clip_end}]")

    # ass_captions reads data["segments"][].words[].{start,end,word} and re-chunks
    # internally, so one segment holding the sliced words is all it needs.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="spool-cap-words.", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump({"segments": [{"words": sliced}]}, f)
        proc = subprocess.run(
            [sys.executable, _ASS_SCRIPT, tmp_path, out_ass_path, style],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ass generation failed (rc={proc.returncode}): {proc.stderr.strip()[-300:]}"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return out_ass_path


def burn(
    clip_path: str,
    ass_path: str,
    out_path: str,
    *,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Burn ``ass_path`` into ``clip_path`` (ffmpeg ``subtitles`` filter); return ``out_path``.

    Re-encodes video (captions are rasterized into the frames); audio is stream-copied.
    """
    argv = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vf", f"subtitles={_escape_filter_path(ass_path)}",
        "-c:a", "copy",
        out_path,
    ]
    _ffmpeg.run(
        argv,
        cancel_check=cancel_check,
        register_proc=register_proc,
        timeout=timeout if timeout is not None else BURN_TIMEOUT,
        cleanup_path=out_path,
        label="ffmpeg caption burn",
    )
    return out_path


def _escape_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph (the ``subtitles`` source).

    Backslash, colon and single-quote are special to the filter parser.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
