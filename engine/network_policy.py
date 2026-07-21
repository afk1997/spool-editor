"""Engine-wide admission and lease accounting for non-loopback network work."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator


class NetworkPolicyError(RuntimeError):
    """Structured policy denial that broad worker catches must re-raise."""

    def __init__(self, code: str, *, purpose: str | None = None):
        self.code = code
        self.purpose = purpose
        if code == "offline_network_disabled":
            message = f"Offline mode blocks network work ({purpose or 'unknown purpose'})."
        elif code == "network_work_active":
            message = "Cannot enable Offline while network work is active."
        else:
            message = code
        super().__init__(message)


class NetworkPolicy:
    """Linearizes Offline transitions against active non-loopback network leases.

    The mutex protects only policy state and lease counts. ``egress()`` releases it before
    yielding to the network operation, while ``transition()`` deliberately holds it across
    the caller's short settings persistence/runtime commit so no new lease can enter between
    validation and the authoritative Offline state change.
    """

    def __init__(self, *, offline: bool = False):
        self._lock = RLock()
        self._offline = bool(offline)
        self._active_leases = 0

    @property
    def offline(self) -> bool:
        with self._lock:
            return self._offline

    @property
    def active_leases(self) -> int:
        with self._lock:
            return self._active_leases

    @contextmanager
    def egress(self, purpose: str) -> Iterator[None]:
        purpose = str(purpose).strip() or "network"
        with self._lock:
            if self._offline:
                raise NetworkPolicyError("offline_network_disabled", purpose=purpose)
            self._active_leases += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_leases -= 1

    @contextmanager
    def transition(self, offline: bool | None) -> Iterator[None]:
        """Hold the policy lock through a caller-owned atomic settings transition.

        ``None`` preserves the current Offline state while still serializing the caller.
        """
        self._lock.acquire()
        try:
            next_offline = self._offline if offline is None else bool(offline)
            if next_offline and self._active_leases:
                raise NetworkPolicyError("network_work_active")
            previous = self._offline
            self._offline = next_offline
            try:
                yield
            except BaseException:
                self._offline = previous
                raise
        finally:
            self._lock.release()

    def set_offline(self, offline: bool) -> None:
        with self.transition(offline):
            pass

    def enable_offline(self) -> None:
        self.set_offline(True)

    def disable_offline(self) -> None:
        self.set_offline(False)
