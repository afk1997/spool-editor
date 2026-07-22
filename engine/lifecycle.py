"""Safe process-lifecycle helpers for the in-process Flask server.

Python delivers signals on the main interpreter thread, where taking application
locks or waiting for subprocesses can deadlock.  The SIGTERM handler below only
writes one byte to a non-blocking pipe.  A dedicated thread performs the real
engine shutdown and exits after all registered service process trees are gone.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import os
import signal
import threading
import time
from typing import Callable

from process_ownership import service_processes

_STOP = b"S"
_TERMINATE = b"T"
SIGTERM_SHUTDOWN_TIMEOUT = 5.0


class _SigtermShutdown(AbstractContextManager):
    def __init__(
        self,
        shutdown: Callable[[float], None],
        *,
        timeout: float = SIGTERM_SHUTDOWN_TIMEOUT,
    ):
        self._shutdown = shutdown
        self._timeout = max(0.0, timeout)
        self._read_fd: int | None = None
        self._write_fd: int | None = None
        self._previous_handler = None
        self._worker: threading.Thread | None = None
        self._last_shutdown_error: BaseException | None = None

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
            deadline = time.monotonic() + self._timeout
            self._last_shutdown_error = None
            try:
                self._shutdown(deadline)
            except BaseException as shutdown_error:
                # Preserve the failure without entering logging locks. The relay
                # returns to the pipe so a fresh SIGTERM can make one fresh,
                # independently bounded attempt; it never spins or extends this
                # signal's absolute deadline.
                self._last_shutdown_error = shutdown_error
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


def sigterm_shutdown(
    shutdown: Callable[[float], None],
    *,
    timeout: float = SIGTERM_SHUTDOWN_TIMEOUT,
) -> AbstractContextManager:
    """Relay SIGTERM to ``shutdown`` without doing unsafe work in the handler."""
    return _SigtermShutdown(shutdown, timeout=timeout)


def run_flask_app(app=None, *, app_factory=None, **run_options):
    """Run a Flask app and close every engine-owned worker on every exit path."""
    if (app is None) == (app_factory is None):
        raise ValueError("provide exactly one of app or app_factory")

    def shutdown_from_signal(deadline: float) -> None:
        # The process-wide latch is first and lock-free. No app construction,
        # application shutdown lock, or worker may admit a fresh subprocess
        # after a termination signal has begun.
        service_processes.close()
        service_processes.shutdown_until(deadline=deadline)

    with sigterm_shutdown(shutdown_from_signal):
        if app_factory is not None:
            app = app_factory()
        shutdown = app.extensions.get("trove.shutdown")
        if not callable(shutdown):
            return app.run(**run_options)
        try:
            return app.run(**run_options)
        finally:
            shutdown(wait=True)
