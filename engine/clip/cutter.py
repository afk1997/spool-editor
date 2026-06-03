"""Frame-accurate trim (spec §5 P1 / §4 ``clip.cut``).

A clip is an in/out range on a source. The cut fast-seeks to the keyframe at/before
``start`` then re-encodes, decoding the preroll and re-emitting from ``start`` exactly, so
the clip begins on the requested frame — not up to a GOP early, as a ``-c copy`` stream-copy
would. That precision is load-bearing: the captioner re-bases word times to ``start``, so a
clip that began at the prior keyframe would desync every caption by that offset (and start
before the chosen moment). The clip is re-encoded again by reframe downstream, so the old
stream-copy's "lossless" win never survived into the rendered clip anyway. This is the
deterministic primitive the timeline editor builds on.
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
        "-ss", f"{start:.3f}",                 # fast input seek to the nearest prior keyframe
        "-i", source_path,
        "-t", f"{duration:.3f}",
        # Re-encode (don't stream-copy): a stream-copy can only begin on a keyframe, so the
        # clip would start up to a GOP early — and the captioner re-bases word times to the
        # *requested* start, so that preroll shows up as a constant caption↔audio desync (and a
        # clip that begins before the chosen moment). Decoding from the keyframe and re-emitting
        # from `start` makes the cut frame-accurate. The clip is re-encoded by reframe downstream
        # anyway, so the old "lossless" copy bought nothing the rendered clip kept.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
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

    Unlike ``cut`` (a single re-encoded range), this stitches several kept ranges via a
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
