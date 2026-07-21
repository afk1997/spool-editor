"""Shared bounded-admission contract for engine worker pools."""


class QueueFullError(RuntimeError):
    """Raised before admission when a worker pool has no pending capacity."""


def pending_capacity(max_workers: int) -> int:
    """Return the maximum queued-plus-running wrappers for a worker pool."""
    return min(32, max(4, 4 * max_workers))
