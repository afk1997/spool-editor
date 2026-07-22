"""Shared ffmpeg subprocess runner for the clip engine.

Every ffmpeg step (cut, caption burn, reframe, export) needs the same plumbing —
spawn via Popen, poll for cancel, bound by a timeout, surface stderr on failure —
so it lives here once instead of being copy-pasted. Mirrors trove's
``transcriber.extract_audio`` conventions.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from process_ownership import spawn_service_process

DEFAULT_TIMEOUT = 3600


def run(
    argv: list[str],
    *,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
    cleanup_path: str | None = None,
    label: str = "ffmpeg",
) -> None:
    """Run ``argv`` to completion.

    Raises ``RuntimeError`` on non-zero exit (stderr tail included), on timeout, or
    ``RuntimeError("cancelled")`` when ``cancel_check()`` goes True. On cancel/timeout
    the partial ``cleanup_path`` (if given) is unlinked.

    - ``cancel_check() -> bool`` is polled every 0.25 s.
    - ``register_proc(proc)`` is called once with the live Popen, then ``None`` in finally.
    """
    eff_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    # Drain stderr to a tempfile — an undrained PIPE deadlocks ffmpeg once the OS
    # buffer fills; stdout is discarded.
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="spool-ffmpeg-stderr.", suffix=".log")
    proc = None
    rc = None
    try:
        proc = spawn_service_process(
            argv,
            popen=subprocess.Popen,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fd,
        )
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
                    try:
                        _kill(proc)
                    finally:
                        _cleanup(cleanup_path)
                    raise RuntimeError("cancelled")
            if time.monotonic() - started > eff_timeout:
                try:
                    _kill(proc)
                finally:
                    _cleanup(cleanup_path)
                raise RuntimeError(f"{label} timed out after {eff_timeout}s")
    finally:
        drain_error = None
        if proc is not None:
            try:
                _confirm_group_exit(proc)
            except BaseException as exc:
                drain_error = exc
        if register_proc is not None:
            try:
                register_proc(None)
            except Exception:
                pass
        if stderr_fd >= 0:
            try:
                os.close(stderr_fd)
            except OSError:
                pass
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
        if drain_error is not None:
            raise drain_error

    if rc != 0:
        raise RuntimeError(f"{label} failed (rc={rc}): {stderr_text.strip()[-300:]}")


def _kill(proc) -> None:
    proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    _confirm_group_exit(proc)


def _confirm_group_exit(proc) -> None:
    wait_for_group_exit = getattr(proc, "wait_for_group_exit", None)
    if callable(wait_for_group_exit):
        wait_for_group_exit(timeout=0.05, forced_timeout=2.0)


def _cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
