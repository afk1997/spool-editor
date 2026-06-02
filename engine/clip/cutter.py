"""Lossless trim (spec §5 P1 / §4 ``clip.cut``).

A clip is an in/out range on a source. The cut is an instant stream-copy
(``ffmpeg -c copy``) so it costs ~nothing; re-encode only happens later in reframe.
Stream-copy seeks to the nearest keyframe at/before ``start`` (you can't cut mid-GOP
without re-encoding), so a clip may begin a fraction early — frame-accurate trimming
is the timeline editor's job (P2). This is the deterministic primitive under it.
"""
from __future__ import annotations

from . import _ffmpeg

# Stream-copy is fast regardless of source length (it seeks, then copies the range),
# but bound it so a wedged ffmpeg can't hang a worker forever.
CUT_TIMEOUT = 300
SPANS_TIMEOUT = 1800  # ripple cut re-encodes, so allow longer than the stream-copy cut


def cut(
    source_path: str,
    start: float,
    end: float,
    out_path: str,
    *,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Stream-copy ``[start, end]`` of ``source_path`` to ``out_path``; return ``out_path``.

    Raises ``ValueError`` for a non-positive range, ``RuntimeError`` on ffmpeg failure,
    and ``RuntimeError("cancelled")`` if ``cancel_check()`` goes True mid-cut.
    """
    if start < 0:
        raise ValueError(f"start must be >= 0, got {start}")
    duration = end - start
    if duration <= 0:
        raise ValueError(f"end ({end}) must be greater than start ({start})")

    argv = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",                 # input seek — fast, keyframe-aligned
        "-i", source_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",                          # stream copy — lossless + instant
        "-avoid_negative_ts", "make_zero",     # re-base timestamps so the clip starts at 0
        out_path,
    ]
    _ffmpeg.run(
        argv,
        cancel_check=cancel_check,
        register_proc=register_proc,
        timeout=timeout if timeout is not None else CUT_TIMEOUT,
        cleanup_path=out_path,
        label="ffmpeg cut",
    )
    return out_path


def cut_spans(
    source_path: str,
    spans,
    out_path: str,
    *,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Cut + concat several kept ranges into one clip — the **ripple cut** behind
    transcript editing: deleting words removes their time spans, and what's left is
    stitched back together. ``spans`` is ``[(start, end), …]`` in source seconds.

    Unlike ``cut`` (lossless stream-copy of one range), this re-encodes via a
    trim/concat filtergraph so the joins are frame-accurate. Raises ``ValueError`` if
    no positive-length span is given.
    """
    parts, maps, n = [], [], 0
    for s, e in spans:
        s, e = float(s), float(e)
        if e <= s:
            continue
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{n}];"
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{n}]"
        )
        maps.append(f"[v{n}][a{n}]")
        n += 1
    if n == 0:
        raise ValueError("cut_spans needs at least one positive-length span")

    fc = ";".join(parts) + ";" + "".join(maps) + f"concat=n={n}:v=1:a=1[v][a]"
    argv = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        out_path,
    ]
    _ffmpeg.run(
        argv,
        cancel_check=cancel_check,
        register_proc=register_proc,
        timeout=timeout if timeout is not None else SPANS_TIMEOUT,
        cleanup_path=out_path,
        label="ffmpeg ripple-cut",
    )
    return out_path
