"""Private, same-filesystem staging for one worker attempt.

Workers write candidate artifacts below ``<download-dir>/.attempts`` and return
an :class:`AttemptOutcome`.  A manager may commit that outcome only while it
holds its state lock and after it has revalidated object identity, attempt
number, status, and dismissal state.  ``os.replace`` then makes each individual
artifact publication atomic on the destination filesystem.

Phase 0 deliberately does not claim transactionality across several files.  A
later artifact reconciler can provide crash recovery for multi-file commits;
this module is the safety fuse that prevents cancelled/stale workers from
publishing at all.
"""
from __future__ import annotations

import copy
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


_KINDS = frozenset({"download", "transcribe", "clip"})


class IsolatedTargetRecord:
    """Deep snapshot passed to callbacks that do not accept an attempt token.

    Legacy callbacks may still read the record and attach a subprocess through
    the historical process field, but all other writes stay on this private
    snapshot.  In particular, a callback that returns after cancellation can no
    longer overwrite canonical status or artifact fields outside the manager's
    attempt fence.
    """

    __slots__ = ("_snapshot", "_process_field", "_register_process")

    def __init__(
        self,
        record: object,
        *,
        process_field: str,
        register_process: Callable[[object], bool],
    ) -> None:
        snapshot: dict[str, Any] = {}
        for name, value in vars(record).items():
            if name == process_field:
                snapshot[name] = None
                continue
            try:
                snapshot[name] = copy.deepcopy(value)
            except Exception as exc:
                # Never fall back to an aliased mutable value: that would let a
                # legacy callback reach canonical state through a nested object.
                raise TypeError(
                    f"cannot isolate legacy target field {name!r}",
                ) from exc
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_process_field", process_field)
        object.__setattr__(self, "_register_process", register_process)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._snapshot[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        if name == self._process_field:
            accepted = self._register_process(value)
            self._snapshot[name] = value if accepted else None
            return
        self._snapshot[name] = value


@dataclass(frozen=True)
class Promotion:
    """Move one staged file to its canonical published location."""

    staged: Path
    final: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "staged", Path(self.staged))
        object.__setattr__(self, "final", Path(self.final))


@dataclass(frozen=True)
class AttemptOutcome:
    """Local worker data awaiting a manager-guarded commit."""

    updates: dict[str, Any] = field(default_factory=dict)
    promotions: tuple[Promotion, ...] = ()
    after_commit: Callable[[object], None] | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "updates", dict(self.updates))
        object.__setattr__(self, "promotions", tuple(self.promotions))


def attempt_root(download_dir: str | os.PathLike[str], kind: str, job_id: str) -> Path:
    """Return the private root for one logical job.

    Job ids originate inside the managers, but validate them anyway so a future
    caller cannot turn cleanup/promotion into path traversal.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown attempt kind {kind!r}")
    if not job_id or Path(job_id).name != job_id or job_id in {".", ".."}:
        raise ValueError("invalid attempt job id")
    return Path(download_dir) / ".attempts" / kind / job_id


def tree_promotions(staged_root: str | os.PathLike[str], final_root: str | os.PathLike[str]) -> tuple[Promotion, ...]:
    """Describe publication of every file below ``staged_root``."""
    staged_root = Path(staged_root)
    final_root = Path(final_root)
    if not staged_root.exists():
        return ()
    return tuple(
        Promotion(path, final_root / path.relative_to(staged_root))
        for path in sorted(staged_root.rglob("*"))
        if path.is_file()
    )


def _rewrite_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite_paths(item, replacements) for item in value)
    return value


def commit_outcome(outcome: AttemptOutcome) -> AttemptOutcome:
    """Publish an already-validated outcome and rewrite staged result paths.

    The caller must hold its manager lock for the entire call.  That lock is
    the linearization point between cancellation and publication.
    """
    replacements: dict[str, str] = {}
    for promotion in outcome.promotions:
        staged = promotion.staged
        final = promotion.final
        if not staged.is_file():
            raise FileNotFoundError(f"staged artifact missing: {staged}")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, final)
        replacements[str(staged)] = str(final)
    return AttemptOutcome(
        updates=_rewrite_paths(outcome.updates, replacements),
        promotions=outcome.promotions,
        after_commit=outcome.after_commit,
    )


def cleanup_attempt(root: str | os.PathLike[str]) -> None:
    """Best-effort removal of one private attempt tree only."""
    root = Path(root)
    # Refuse to recursively delete a corrupted/published path.  A valid root is
    # always ``.../.attempts/<kind>/<job-id>``.
    parents = root.parents
    if len(parents) < 2 or parents[1].name != ".attempts" or root.parent.name not in _KINDS:
        raise ValueError(f"refusing to clean non-attempt path: {root}")
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        pass


def apply_updates(record: object, updates: dict[str, Any]) -> None:
    """Apply a committed manager-owned update set to a canonical record."""
    for name, value in updates.items():
        if name.startswith("_") or not hasattr(record, name):
            raise ValueError(f"invalid attempt update field: {name}")
        setattr(record, name, value)
