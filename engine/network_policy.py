"""Engine-wide admission and lease accounting for non-loopback network work."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock, get_ident
from typing import Iterator


class NetworkPolicyError(RuntimeError):
    """Structured policy denial that broad worker catches must re-raise."""

    def __init__(self, code: str, *, purpose: str | None = None):
        self.code = code
        self.error_category = code
        self.purpose = purpose
        if code == "offline_network_disabled":
            message = f"Offline mode blocks network work ({purpose or 'unknown purpose'})."
        elif code == "network_work_active":
            message = "Cannot enable Offline while network work is active."
        else:
            message = code
        super().__init__(message)


class _EgressLease:
    """Owner-thread token proving launch admission belongs to a live lease."""

    def __init__(self, policy: "NetworkPolicy", purpose: str):
        self._policy = policy
        self._purpose = purpose
        self._owner_ident = get_ident()
        self._active = True

    @contextmanager
    def launch_admission(self) -> Iterator[None]:
        with self._policy._launch_admission(self):
            yield


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
    def egress(self, purpose: str) -> Iterator[_EgressLease]:
        purpose = str(purpose).strip() or "network"
        with self._lock:
            if self._offline:
                raise NetworkPolicyError("offline_network_disabled", purpose=purpose)
            self._active_leases += 1
            lease = _EgressLease(self, purpose)
        try:
            yield lease
        finally:
            with self._lock:
                if lease._active:
                    lease._active = False
                    self._active_leases -= 1

    @contextmanager
    def _launch_admission(self, lease: _EgressLease) -> Iterator[None]:
        """Linearize a final live privacy check with remote process launch.

        Callers enter this only while holding an ``egress()`` lease. The same mutex
        guards ``transition()``, so the lock order is consistently policy then any
        caller-owned settings lock. Keep this section short: check live settings and
        create the process, then release it before waiting on remote work.
        """
        self._lock.acquire()
        try:
            if lease._policy is not self or not lease._active:
                raise RuntimeError("launch admission requires an active egress lease")
            if lease._owner_ident != get_ident():
                raise RuntimeError("launch admission requires the lease's owning thread")
            if self._offline:
                raise NetworkPolicyError(
                    "offline_network_disabled", purpose=lease._purpose
                )
            yield
        finally:
            self._lock.release()

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
