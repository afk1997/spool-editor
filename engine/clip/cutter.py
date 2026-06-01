"""Lossless trim (spec §5 P1 / §4 ``clip.cut``).

A clip is an in/out range on a source. The cut is an instant stream-copy
(``ffmpeg -c copy``) so it costs ~nothing; re-encode only happens later in reframe.
Stream-copy seeks to the nearest keyframe at/before ``start`` (you can't cut mid-GOP
without re-encoding), so a clip may begin a fraction early — frame-accurate trimming
is the timeline editor's job (P2). This is the deterministic primitive under it.

Follows trove's ffmpeg-subprocess conventions (see ``transcriber.extract_audio``):
spawn via ``Popen``, poll with an optional ``cancel_check``, surface stderr on failure.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

# Stream-copy is fast regardless of source length (it seeks, then copies the range),
# but bound it so a wedged ffmpeg can't hang a worker forever.
CUT_TIMEOUT = 300


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
    and ``RuntimeError("cancelled")`` if ``cancel_check()`` goes True mid-cut (the caller
    treats that as a clean abort, matching the transcribe path).
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
    eff_timeout = timeout if timeout is not None else CUT_TIMEOUT

    # Drain stderr to a tempfile (ffmpeg writes progress there; an undrained PIPE would
    # deadlock once the OS buffer fills). stdout is discarded.
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="spool-cut-stderr.", suffix=".log")
    proc = None
    rc = None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=stderr_fd)
        os.close(stderr_fd)
        stderr_fd = -1

        if register_proc is not None:
            try:
                register_proc(proc)
            except Exception:
                pass

        started = time.monotonic()
        while True:
            try:
                rc = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel_check is not None:
                try:
                    cancelled = bool(cancel_check())
                except Exception:
                    cancelled = False
                if cancelled:
                    proc.kill()
                    try: proc.wait(timeout=2)
                    except subprocess.TimeoutExpired: pass
                    try:
                        if os.path.exists(out_path):
                            os.remove(out_path)
                    except OSError:
                        pass
                    raise RuntimeError("cancelled")
            if time.monotonic() - started > eff_timeout:
                proc.kill()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
                raise RuntimeError(f"ffmpeg cut timed out after {eff_timeout}s")
    finally:
        if register_proc is not None:
            try:
                register_proc(None)
            except Exception:
                pass
        if stderr_fd >= 0:
            try: os.close(stderr_fd)
            except OSError: pass
        stderr_text = ""
        try:
            with open(stderr_path, "r", errors="replace") as f:
                stderr_text = f.read()
        except OSError:
            pass
        try:
            os.unlink(stderr_path)
        except OSError:
            pass

    if rc != 0:
        raise RuntimeError(f"ffmpeg cut failed (rc={rc}): {stderr_text.strip()[-300:]}")
    return out_path
