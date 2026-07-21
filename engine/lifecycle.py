"""Safe process-lifecycle helpers for the in-process Flask server.

Python delivers signals on the main interpreter thread, where taking application
locks or waiting for subprocesses can deadlock.  The SIGTERM handler below only
writes one byte to a non-blocking pipe.  A dedicated thread performs the real
engine shutdown and exits after all registered reasoning process trees are gone.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import logging
import os
import signal
import threading
import time
from typing import Callable


_log = logging.getLogger(__name__)
_STOP = b"S"
_TERMINATE = b"T"


class _SigtermShutdown(AbstractContextManager):
    def __init__(self, shutdown: Callable[[], None]):
        self._shutdown = shutdown
        self._read_fd: int | None = None
        self._write_fd: int | None = None
        self._previous_handler = None
        self._worker: threading.Thread | None = None

    def __enter__(self):
        if (
            os.name != "posix"
            or not hasattr(signal, "SIGTERM")
            or threading.current_thread() is not threading.main_thread()
        ):
            return self

        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._previous_handler = signal.getsignal(signal.SIGTERM)

        def relay(_signum, _frame):
            # ``write(2)`` is async-signal-safe.  Do not acquire Python locks,
            # log, terminate children, or run application callbacks here.
            try:
                os.write(write_fd, _TERMINATE)
            except OSError:
                # A full/closed pipe means a shutdown notification is already
                # pending or the context has completed.
                pass

        self._worker = threading.Thread(
            target=self._wait_for_signal,
            name="trove-sigterm-shutdown",
            daemon=True,
        )
        signal.signal(signal.SIGTERM, relay)
        # The pipe buffers a signal delivered before the worker starts.
        try:
            self._worker.start()
        except BaseException:
            try:
                signal.signal(signal.SIGTERM, self._previous_handler)
            finally:
                self._close_pipe()
                self._worker = None
            raise
        return self

    def _wait_for_signal(self) -> None:
        assert self._read_fd is not None
        while True:
            try:
                command = os.read(self._read_fd, 1)
            except InterruptedError:
                continue
            except OSError:
                return
            if not command or command == _STOP:
                return
            if command != _TERMINATE:
                continue
            delay = 0.05
            while True:
                try:
                    self._shutdown()
                except BaseException:
                    _log.exception(
                        "engine shutdown failed while handling SIGTERM; retrying"
                    )
                    # One termination request owns the drain through completion.
                    # Each registry attempt is bounded; SIGKILL remains the
                    # operator's explicit escape if a process tree never exits.
                    try:
                        time.sleep(delay)
                    except BaseException:
                        pass
                    delay = min(1.0, delay * 2)
                    continue
                os._exit(128 + signal.SIGTERM)
                return

    def __exit__(self, exc_type, exc_value, traceback):
        if self._write_fd is None:
            return False

        signal.signal(signal.SIGTERM, self._previous_handler)
        try:
            os.write(self._write_fd, _STOP)
        except OSError:
            pass
        if self._worker is not None:
            self._worker.join(timeout=1)
        self._close_pipe()
        return False

    def _close_pipe(self) -> None:
        for fd in (self._write_fd, self._read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._write_fd = None
        self._read_fd = None


def sigterm_shutdown(shutdown: Callable[[], None]) -> AbstractContextManager:
    """Relay SIGTERM to ``shutdown`` without doing unsafe work in the handler."""
    return _SigtermShutdown(shutdown)


def run_flask_app(app=None, *, app_factory=None, **run_options):
    """Run a Flask app and close every engine-owned worker on every exit path."""
    if (app is None) == (app_factory is None):
        raise ValueError("provide exactly one of app or app_factory")

    app_ready = threading.Event()
    shutdown_holder = {}

    def shutdown_from_signal():
        # SIGTERM may arrive midway through app construction. Wait for the
        # factory to either publish its complete ownership boundary or fail.
        app_ready.wait()
        shutdown = shutdown_holder.get("shutdown")
        if callable(shutdown):
            shutdown(wait=False)

    with sigterm_shutdown(shutdown_from_signal):
        if app_factory is not None:
            try:
                app = app_factory()
            finally:
                if app is None:
                    app_ready.set()
        shutdown = app.extensions.get("trove.shutdown")
        shutdown_holder["shutdown"] = shutdown
        app_ready.set()
        if not callable(shutdown):
            return app.run(**run_options)
        try:
            return app.run(**run_options)
        finally:
            delay = 0.05
            while True:
                try:
                    shutdown(wait=True)
                except BaseException:
                    _log.exception(
                        "engine shutdown is incomplete; retrying before process exit"
                    )
                    try:
                        time.sleep(delay)
                    except BaseException:
                        # Repeated Ctrl-C must not bypass ownership cleanup. An
                        # operator can still use SIGKILL for an explicit hard stop.
                        pass
                    delay = min(1.0, delay * 2)
                    continue
                break
