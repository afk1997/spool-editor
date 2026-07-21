"""ClipJob + ClipJobManager — the clip/render job queue (spec §3, §5 P1).

One manager drives every clip-engine operation: moment-finding, cut, reframe,
caption, export, and the one-shot render pipeline. It is trove's render queue —
"the same machinery with new job types" (spec §1.1) — so the studio render-queue,
the MCP progress stream, and the agent's status updates all read this one model.

Same lock + ThreadPoolExecutor + atomic-JSON persistence + restart-downgrade
pattern as ``transcribe_jobs.py``; mirror it, don't reinvent it. Each ClipJob carries
a ``kind`` plus free-form ``params`` (inputs) and ``result`` (outputs: the candidate
list, the produced clip_id, output paths, a render id, …) so the manager stays generic
and the per-kind logic lives in the work closures built by ``create_app``.
"""
from __future__ import annotations

import enum
import inspect
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from attempt_staging import (
    AttemptOutcome,
    IsolatedTargetRecord,
    apply_updates,
    attempt_root,
    cleanup_attempt,
    commit_outcome,
)
from job_capacity import QueueFullError, pending_capacity


class ClipStatus(str, enum.Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = {ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED}


class _AdmissionLease:
    """Idempotent ownership of one admitted executor wrapper."""

    __slots__ = ("_manager", "_released")

    def __init__(self, manager: "ClipJobManager"):
        self._manager = manager
        self._released = False

    def release(self) -> bool:
        manager = self._manager
        with manager._lock:
            if self._released:
                return False
            self._released = True
            manager._pending_count -= 1
            return True


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The clip-engine operations, one per engine module (+ the chained pipeline).
CLIP_KINDS = {"moments", "cut", "reframe", "caption", "export", "pipeline", "produce"}


@dataclass
class ClipJob:
    id: str
    kind: str                              # one of CLIP_KINDS
    source_id: str | None = None           # the parent media job (the "source")
    clip_id: str | None = None             # the clip artifact produced/operated on
    status: ClipStatus = ClipStatus.QUEUED
    progress_pct: int = 0
    stage: str = ""                        # human stage label (pipeline: cut→reframe→…)
    started_at: float = field(default_factory=time.time)
    params: dict = field(default_factory=dict)   # kind-specific inputs
    result: dict = field(default_factory=dict)   # kind-specific outputs
    error_category: str | None = None
    error_message: str | None = None
    # Not persisted:
    process_handle: object | None = None
    _cancel_flag: bool = False
    dismissed_at: str | None = None
    last_accessed: float = field(default_factory=time.monotonic)
    _attempt: int = field(default=0, repr=False, compare=False)
    _worker_active: bool = field(default=False, repr=False, compare=False)
    _staging_root: str = field(default="", repr=False, compare=False)


_PERSISTENT_FIELDS = {
    "id", "kind", "source_id", "clip_id", "status", "progress_pct", "stage",
    "started_at", "params", "result", "error_category", "error_message",
    "dismissed_at",
}


class ClipJobManager:
    def __init__(self, *, max_workers: int = 2, ttl_seconds: int = 3600,
                 store_path: object = None):
        self.max_workers = max_workers
        self.pending_capacity = pending_capacity(max_workers)
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, ClipJob] = {}
        # _persist() takes the persistence mutex before this state lock.
        # Lifecycle callers release the state lock before entering persistence.
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._persist_dirty = False
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._pending_count = 0
        self._accepting = True
        self._store_path = Path(store_path) if store_path else None
        self._attempt_base = (
            self._store_path.parent
            if self._store_path is not None
            else Path(tempfile.mkdtemp(prefix="spool-clip-attempts-"))
        )
        if self._store_path is not None:
            self._load_from_store()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count

    def _reserve_many_locked(self, count: int) -> list[_AdmissionLease]:
        if not self._accepting:
            raise RuntimeError("clip manager is shut down")
        if self._pending_count + count > self.pending_capacity:
            raise QueueFullError("media queue full")
        self._pending_count += count
        return [_AdmissionLease(self) for _ in range(count)]

    def _reserve_locked(self) -> _AdmissionLease:
        return self._reserve_many_locked(1)[0]

    # ----- persistence ---------------------------------------------------

    def _load_from_store(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("schema_version") != 1:
            return
        for jid, raw in (data.get("jobs") or {}).items():
            try:
                status_str = raw.get("status", "queued")
                # No clip op resumes across a restart (ffmpeg/LLM have no resume),
                # so anything mid-flight becomes an error the user can retry.
                if status_str in ("running", "queued"):
                    raw["status"] = ClipStatus.ERROR.value
                    raw["error_category"] = "server_restart"
                    raw["error_message"] = "clip job interrupted by server restart"
                self._jobs[jid] = ClipJob(
                    id=raw["id"],
                    kind=raw.get("kind", ""),
                    source_id=raw.get("source_id"),
                    clip_id=raw.get("clip_id"),
                    status=ClipStatus(raw["status"]),
                    progress_pct=raw.get("progress_pct", 0),
                    stage=raw.get("stage", ""),
                    started_at=raw.get("started_at", 0.0),
                    params=raw.get("params") or {},
                    result=raw.get("result") or {},
                    error_category=raw.get("error_category"),
                    error_message=raw.get("error_message"),
                    dismissed_at=raw.get("dismissed_at"),
                )
            except (KeyError, ValueError):
                continue

    def _persist(self, *, only_if_dirty: bool = False) -> bool:
        if self._store_path is None:
            return True
        with self._persist_lock:
            try:
                if only_if_dirty and not self._persist_dirty:
                    return True
                self._persist_dirty = True
                with self._lock:
                    payload = {
                        "schema_version": 1,
                        "jobs": {
                            # Explicit getattr allowlist — NOT dataclasses.asdict, which
                            # deep-copies every field and raises TypeError on a live Popen
                            # in process_handle, silently dropping this persist (e.g. the
                            # CANCELLED write while ffmpeg is still running).
                            jid: {**{k: getattr(j, k) for k in _PERSISTENT_FIELDS if k != "status"},
                                  "status": j.status.value}
                            for jid, j in self._jobs.items()
                        },
                    }
                self._store_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".clip.", dir=str(self._store_path.parent))
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(payload, f, indent=2)
                    os.replace(tmp, self._store_path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "clip-store persist failed for %s", self._store_path, exc_info=True)
                return False
            self._persist_dirty = False
            return True

    def _prepare_attempt_locked(self, job: ClipJob) -> tuple[int, Path]:
        root = attempt_root(self._attempt_base, "clip", job.id)
        root.mkdir(parents=True, exist_ok=True)
        job._staging_root = str(root)
        job._attempt += 1
        job._worker_active = True
        return job._attempt, root

    def _mutate_current(
        self,
        jid: str,
        captured_job: ClipJob,
        attempt: int,
        expected: set[ClipStatus],
        mutate: Callable[[ClipJob], None],
    ) -> bool:
        with self._lock:
            current = self._jobs.get(jid)
            valid = (
                current is captured_job
                and current._attempt == attempt
                and current.status in expected
                and current.dismissed_at is None
            )
            if not valid:
                return False
            mutate(current)
            return True

    def register_process(self, jid: str, captured_job: ClipJob, attempt: int, process) -> bool:
        accepted = self._mutate_current(
            jid, captured_job, attempt, {ClipStatus.RUNNING},
            lambda current: setattr(current, "process_handle", process),
        )
        if not accepted and process is not None and hasattr(process, "kill"):
            try:
                process.kill()
            except Exception:
                pass
        return accepted

    def attempt_cancelled(self, jid: str, captured_job: ClipJob, attempt: int) -> bool:
        with self._lock:
            current = self._jobs.get(jid)
            return not (
                current is captured_job
                and current._attempt == attempt
                and current.status is ClipStatus.RUNNING
                and current.dismissed_at is None
            )

    @staticmethod
    def _set_error(current: ClipJob, exc: Exception) -> None:
        current.status = ClipStatus.ERROR
        current.error_category = current.error_category or getattr(exc, "error_category", "unknown")
        current.error_message = current.error_message or str(exc)

    def _invoke_target(self, target, job: ClipJob, attempt: int):
        try:
            parameter = inspect.signature(target).parameters.get("attempt")
            accepts_attempt = parameter is not None and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        except (TypeError, ValueError):
            accepts_attempt = False
        if accepts_attempt:
            return target(job, attempt=attempt)
        isolated = IsolatedTargetRecord(
            job,
            process_field="process_handle",
            register_process=lambda process: self.register_process(
                job.id, job, attempt, process,
            ),
        )
        return target(isolated)

    def _run_attempt(
        self,
        job: ClipJob,
        attempt: int,
        target: Callable[[ClipJob], object],
        reservation: _AdmissionLease,
    ) -> None:
        try:
            started = self._mutate_current(
                job.id, job, attempt, {ClipStatus.QUEUED},
                lambda current: setattr(current, "status", ClipStatus.RUNNING),
            )
            if not started:
                return
            self._persist()

            outcome = self._invoke_target(target, job, attempt)
            after_commit = None

            def finish(current: ClipJob) -> None:
                nonlocal after_commit
                if isinstance(outcome, AttemptOutcome):
                    committed = commit_outcome(outcome)
                    apply_updates(current, committed.updates)
                    after_commit = committed.after_commit
                current.status = ClipStatus.DONE
                current.progress_pct = 100
                current.process_handle = None

            accepted = self._mutate_current(
                job.id, job, attempt, {ClipStatus.RUNNING}, finish,
            )
            if accepted:
                self._persist()
                if after_commit is not None:
                    try:
                        after_commit(job)
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "clip post-commit hook failed for %s", job.id, exc_info=True,
                        )
        except Exception as exc:
            accepted = self._mutate_current(
                job.id, job, attempt, {ClipStatus.RUNNING},
                lambda current: self._set_error(current, exc),
            )
            if accepted:
                self._persist()
        finally:
            try:
                with self._lock:
                    current = self._jobs.get(job.id)
                    if current is job:
                        if current._staging_root:
                            try:
                                cleanup_attempt(current._staging_root)
                            except Exception:
                                logging.getLogger(__name__).warning(
                                    "clip attempt cleanup failed for %s",
                                    current.id,
                                    exc_info=True,
                                )
                        current.process_handle = None
                        current._worker_active = False
            finally:
                reservation.release()

    def _new_job_locked(
        self,
        *,
        kind: str,
        target: Callable[[ClipJob], object],
        source_id: str | None,
        clip_id: str | None,
        params: dict | None,
    ) -> tuple[ClipJob, int, Callable[[ClipJob], object]]:
        jid = uuid.uuid4().hex[:10]
        job = ClipJob(
            id=jid, kind=kind, source_id=source_id, clip_id=clip_id,
            params=params or {},
        )
        try:
            attempt, _root = self._prepare_attempt_locked(job)
            self._jobs[jid] = job
            return job, attempt, target
        except Exception:
            # A staging/setup failure can happen after the lease/root exists.
            # This helper owns that partial record until it returns, so unwind
            # it here before the outer fan-out transaction can see it.
            self._jobs.pop(jid, None)
            job.process_handle = None
            job._worker_active = False
            if job._staging_root:
                try:
                    cleanup_attempt(job._staging_root)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "failed to clean partial clip child %s", jid, exc_info=True,
                    )
            raise

    # ----- lifecycle -----------------------------------------------------

    def submit(self, *, kind: str, target: Callable[[ClipJob], None],
               source_id: str | None = None, clip_id: str | None = None,
               params: dict | None = None) -> str:
        if kind not in CLIP_KINDS:
            raise ValueError(f"unknown clip kind {kind!r}; expected one of {sorted(CLIP_KINDS)}")
        reservation = None
        job = None
        try:
            with self._lock:
                reservation = self._reserve_locked()
                try:
                    job, attempt, target = self._new_job_locked(
                        kind=kind, target=target, source_id=source_id,
                        clip_id=clip_id, params=params,
                    )
                    self._executor.submit(
                        self._run_attempt, job, attempt, target, reservation,
                    )
                except Exception:
                    if job is not None:
                        self._jobs.pop(job.id, None)
                        job.process_handle = None
                        job._worker_active = False
                        if job._staging_root:
                            try:
                                cleanup_attempt(job._staging_root)
                            except Exception:
                                logging.getLogger(__name__).warning(
                                    "failed to clean rejected clip %s",
                                    job.id,
                                    exc_info=True,
                                )
                    reservation.release()
                    raise
        except (QueueFullError, RuntimeError):
            if reservation is not None:
                self._persist()
            raise
        except Exception:
            self._persist()
            raise
        self._persist()
        return job.id

    def submit_children_if_current(
        self,
        parent: ClipJob,
        attempt: int,
        specs: list[dict],
    ) -> list[str]:
        """Atomically admit every produce child or none of them.

        Executor work is submitted while the manager RLock is held.  Child
        closures acquire that same lock before leaving QUEUED, so none can run
        until the complete child set and the parent's child-id result are
        visible together.
        """
        overflow = False
        with self._lock:
            current = self._jobs.get(parent.id)
            if not (
                current is parent
                and current._attempt == attempt
                and current.status is ClipStatus.RUNNING
                and current.dismissed_at is None
            ):
                return []

            # Validate the complete batch before allocating an id, lease, or
            # staging directory for any child.
            validated: list[tuple[str, Callable[[ClipJob], object], str | None,
                                  str | None, dict | None]] = []
            for spec in specs:
                if not isinstance(spec, dict):
                    raise ValueError("child spec must be a mapping")
                kind = spec.get("kind")
                if kind not in CLIP_KINDS:
                    raise ValueError(
                        f"unknown clip kind {kind!r}; expected one of {sorted(CLIP_KINDS)}",
                    )
                target = spec.get("target")
                if not callable(target):
                    raise ValueError("child target must be callable")
                validated.append((
                    kind, target, spec.get("source_id"), spec.get("clip_id"),
                    spec.get("params"),
                ))

            try:
                reservations = self._reserve_many_locked(len(validated))
            except QueueFullError:
                current.status = ClipStatus.ERROR
                current.error_category = "queue_full"
                current.error_message = "media queue full"
                current.result = {
                    "error": "queue_full",
                    "requested": len(validated),
                    "clip_jobs": [],
                }
                overflow = True

            if overflow:
                child_ids = []
            else:
                original_result = current.result
                initial_job_ids = set(self._jobs)
                submissions: list[
                    tuple[
                        ClipJob,
                        int,
                        Callable[[ClipJob], object],
                        _AdmissionLease,
                    ]
                ] = []
                futures: list[tuple[object, _AdmissionLease]] = []
                try:
                    for reservation, (
                        kind, target, source_id, clip_id, params,
                    ) in zip(reservations, validated):
                        child, child_attempt, child_target = self._new_job_locked(
                            kind=kind,
                            target=target,
                            source_id=source_id,
                            clip_id=clip_id,
                            params=params,
                        )
                        submissions.append((
                            child,
                            child_attempt,
                            child_target,
                            reservation,
                        ))

                    for child, child_attempt, child_target, reservation in submissions:
                        future = self._executor.submit(
                            self._run_attempt,
                            child,
                            child_attempt,
                            child_target,
                            reservation,
                        )
                        futures.append((future, reservation))

                    child_ids = [
                        child.id
                        for child, _attempt, _target, _reservation in submissions
                    ]
                    current.result = {
                        **(original_result or {}),
                        "count": len(child_ids),
                        "clip_jobs": child_ids,
                    }
                except Exception:
                    # Child wrappers cannot leave QUEUED while this RLock is
                    # held. Remove their identities below; wrappers that could
                    # not be cancelled then drain as stale no-ops and release
                    # their own reservations.
                    submitted_reservations = {
                        id(reservation) for _future, reservation in futures
                    }
                    for future, reservation in futures:
                        try:
                            if future.cancel():
                                reservation.release()
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "failed to cancel rolled-back clip child",
                                exc_info=True,
                            )
                    for reservation in reservations:
                        if id(reservation) not in submitted_reservations:
                            reservation.release()

                    new_children = [
                        child for jid, child in self._jobs.items()
                        if jid not in initial_job_ids
                    ]
                    for child in new_children:
                        self._jobs.pop(child.id, None)
                        child.process_handle = None
                        child._worker_active = False
                        if child._staging_root:
                            try:
                                cleanup_attempt(child._staging_root)
                            except Exception:
                                logging.getLogger(__name__).warning(
                                    "failed to clean rolled-back clip child %s",
                                    child.id, exc_info=True,
                                )
                    current.result = original_result
                    raise
        self._persist()
        return child_ids

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return False
            if j.status in {ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED}:
                return False
            j._cancel_flag = True
            j._attempt += 1
            j.status = ClipStatus.CANCELLED
            proc = j.process_handle
            if not j._worker_active and j._staging_root:
                cleanup_attempt(j._staging_root)
        if proc is not None and hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        self._persist()
        return True

    def dismiss(self, jid: str) -> bool:
        return self._mark_dismissed(jid)

    def _mark_dismissed(self, jid: str) -> bool:
        changed = False
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return False
            if j.status not in _TERMINAL_STATUSES:
                return False
            if j.dismissed_at is None:
                j.dismissed_at = _utc_now_rfc3339()
                changed = True
        self._persist(only_if_dirty=not changed)
        return True

    def get(self, jid: str) -> ClipJob | None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is not None:
                j.last_accessed = time.monotonic()
            return j

    def get_by_clip(self, clip_id: str) -> list[ClipJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.clip_id == clip_id]

    def get_by_source(self, source_id: str) -> list[ClipJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.source_id == source_id]

    def snapshot_jobs(self) -> list[ClipJob]:
        with self._lock:
            return list(self._jobs.values())

    def update_progress(self, jid: str, *args, stage: str | None = None) -> bool:
        if len(args) == 1:
            pct = args[0]
            with self._lock:
                captured = self._jobs.get(jid)
                if captured is None:
                    return False
                attempt = captured._attempt
        elif len(args) == 3:
            captured, attempt, pct = args
        else:
            raise TypeError("update_progress expects pct or captured_job, attempt, pct")

        def mutate(current: ClipJob) -> None:
            current.progress_pct = max(0, min(100, int(pct)))
            if stage is not None:
                current.stage = stage

        accepted = self._mutate_current(
            jid, captured, attempt, {ClipStatus.RUNNING}, mutate,
        )
        if accepted:
            self._persist()
        return accepted

    def sweep(self) -> int:
        cutoff = time.monotonic() - self.ttl_seconds
        changed = 0
        dismissed_at = _utc_now_rfc3339()
        with self._lock:
            for j in self._jobs.values():
                if j.status not in _TERMINAL_STATUSES or j.dismissed_at is not None:
                    continue
                if j.last_accessed > cutoff:
                    continue
                j.dismissed_at = dismissed_at
                changed += 1
        self._persist(only_if_dirty=not changed)
        return changed

    def start_sweeper(self, interval_seconds: int = 300) -> None:
        def loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    self.sweep()
                except Exception:
                    pass
        threading.Thread(
            target=loop, daemon=True, name="trove-clip-sweeper",
        ).start()

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._accepting = False
        self._executor.shutdown(wait=wait)
        if wait:
            with self._lock:
                if self._pending_count != 0:
                    raise RuntimeError("clip manager shutdown left pending work")
