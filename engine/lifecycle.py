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
        self._worker.start()
        signal.signal(signal.SIGTERM, relay)
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
            try:
                self._shutdown()
            except BaseException:
                _log.exception("engine shutdown failed while handling SIGTERM")
            finally:
                os._exit(128 + signal.SIGTERM)

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
        for fd in (self._write_fd, self._read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._write_fd = None
        self._read_fd = None
        return False


def sigterm_shutdown(shutdown: Callable[[], None]) -> AbstractContextManager:
    """Relay SIGTERM to ``shutdown`` without doing unsafe work in the handler."""
    return _SigtermShutdown(shutdown)


def run_flask_app(app, **run_options):
    """Run a Flask app and close every engine-owned worker on every exit path."""
    shutdown = app.extensions.get("trove.shutdown")
    if not callable(shutdown):
        return app.run(**run_options)

    with sigterm_shutdown(lambda: shutdown(wait=False)):
        try:
            return app.run(**run_options)
        finally:
            shutdown(wait=True)
