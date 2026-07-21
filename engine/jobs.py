from __future__ import annotations
import enum
import glob
import inspect
import logging
import os
import threading
import tempfile
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


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}
_PUBLISHED_SIDECAR_SUFFIXES = (".json", ".srt", ".vtt", ".txt", ".ass")


class AttemptUnwindingError(RuntimeError):
    """A paused attempt still owns its worker/staging lease."""


class _AdmissionLease:
    """Idempotent ownership of one admitted executor wrapper."""

    __slots__ = ("_manager", "_released")

    def __init__(self, manager: "JobManager"):
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


@dataclass
class Job:
    id: str
    url: str
    title: str
    status: JobStatus = JobStatus.QUEUED
    thumbnail: str = ""
    file_path: str | None = None
    filename: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    process: object | None = None  # subprocess.Popen, set by runner if it wants kill support
    created_at: float = field(default_factory=time.monotonic)
    last_accessed: float = field(default_factory=time.monotonic)
    # Progress (populated by runner during DOWNLOADING)
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0  # bytes/sec
    eta: int = 0  # seconds remaining
    # HLS / fragmented downloads expose fragment counts even when total_bytes is unknown.
    fragment_index: int = 0
    fragment_count: int = 0
    # Resume args — captured at submit time so a paused job can be re-run after restart
    format_choice: str = "video"
    format_id: str | None = None
    out_template: str = ""
    # Transient flag set by JobManager.pause() before the process is killed,
    # so runner._cleanup_glob() can be skipped (preserves .part files).
    _was_paused: bool = False
    # When True, the download worker auto-enqueues a transcribe on success
    # using the active model + env-default diarization. Set at submit time
    # by the batch endpoint or the single-URL ready-card form when the
    # "transcribe after download" checkbox is checked. Persisted so the
    # behavior survives a server restart and a paused→resumed download.
    auto_transcribe: bool = False
    # Transient hint surfaced on the DONE card when auto-transcribe was
    # requested but couldn't fire (e.g. no active model). Not persisted;
    # the hint disappears after a server restart, which is acceptable —
    # the user can still click `▸ transcribe` manually after installing
    # a model.
    _auto_transcribe_hint: str | None = None
    dismissed_at: str | None = None
    _attempt: int = field(default=0, repr=False, compare=False)
    _worker_active: bool = field(default=False, repr=False, compare=False)
    _staging_root: str = field(default="", repr=False, compare=False)


class JobManager:
    def __init__(
        self,
        *,
        max_workers: int = 4,
        ttl_seconds: int = 3600,
        store_path: object = None,  # Path or None; None disables persistence
    ):
        self.max_workers = max_workers
        self.pending_capacity = pending_capacity(max_workers)
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, Job] = {}
        # Lock order is persistence mutex -> state lock. Lifecycle callers always
        # release the state lock before entering _persist(), so concurrent writes
        # cannot deadlock or replace a newer snapshot with an older one.
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
            else Path(tempfile.mkdtemp(prefix="spool-download-attempts-"))
        )
        if self._store_path is not None:
            self._load_from_store()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count

    def _reserve_locked(self) -> _AdmissionLease:
        if not self._accepting:
            raise RuntimeError("job manager is shut down")
        if self._pending_count >= self.pending_capacity:
            raise QueueFullError("download queue full")
        self._pending_count += 1
        return _AdmissionLease(self)

    def _load_from_store(self) -> None:
        from jobs_store import load_jobs
        loaded = load_jobs(self._store_path)
        for jid, job in loaded.items():
            # Downgrade rules per design §4.2:
            # DOWNLOADING / QUEUED → PAUSED (interrupted by restart, no live thunk)
            # DONE / ERROR / CANCELLED / PAUSED kept as-is
            if job.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                job.status = JobStatus.PAUSED
            self._jobs[jid] = job

    def _persist(self, *, only_if_dirty: bool = False) -> bool:
        if self._store_path is None:
            return True
        with self._persist_lock:
            if only_if_dirty and not self._persist_dirty:
                return True
            self._persist_dirty = True
            try:
                from jobs_store import persist_atomic
                with self._lock:
                    snapshot = dict(self._jobs)
                persist_atomic(snapshot, self._store_path)
            except Exception:
                # A failed write stays dirty so an idempotent dismiss or sweep can
                # retry the latest full snapshot without crashing lifecycle work.
                logging.getLogger(__name__).warning(
                    "job-store persist failed for %s", self._store_path, exc_info=True)
                return False
            self._persist_dirty = False
            return True

    def _ensure_download_staging_locked(self, job: Job) -> Path:
        """Normalize legacy output templates into this job's private root.

        Older stores point ``out_template`` at the published download root.
        Move only unpublished candidates into staging; a recorded published
        file and transcript sidecars are immutable inputs and stay in place.
        """
        root = attempt_root(self._attempt_base, "download", job.id)
        root.mkdir(parents=True, exist_ok=True)
        old_template = job.out_template
        new_template = root / f"{job.id}.%(ext)s"
        if old_template and Path(old_template).parent != root:
            published = os.path.realpath(job.file_path) if job.file_path else None
            for candidate in glob.glob(old_template.replace("%(ext)s", "*")):
                if not os.path.isfile(candidate):
                    continue
                if published and os.path.realpath(candidate) == published:
                    continue
                if candidate.lower().endswith(_PUBLISHED_SIDECAR_SUFFIXES):
                    continue
                destination = root / Path(candidate).name
                try:
                    os.replace(candidate, destination)
                except OSError:
                    logging.getLogger(__name__).warning(
                        "failed to migrate legacy partial %s into %s",
                        candidate, destination, exc_info=True,
                    )
        job._staging_root = str(root)
        job.out_template = str(new_template)
        return root

    def _mutate_current(
        self,
        job_id: str,
        captured_job: Job,
        attempt: int,
        expected: set[JobStatus],
        mutate: Callable[[Job], None],
    ) -> bool:
        """Validate and mutate one captured attempt in one critical section."""
        with self._lock:
            current = self._jobs.get(job_id)
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

    def register_process(self, job_id: str, captured_job: Job, attempt: int, process) -> bool:
        accepted = self._mutate_current(
            job_id, captured_job, attempt, {JobStatus.DOWNLOADING},
            lambda current: setattr(current, "process", process),
        )
        if not accepted and process is not None and hasattr(process, "kill"):
            try:
                process.kill()
            except Exception:
                pass
        return accepted

    def update_progress(
        self,
        job_id: str,
        captured_job: Job,
        attempt: int,
        *,
        downloaded: int,
        total: int,
        speed: float,
        eta: int,
        fragment_index: int,
        fragment_count: int,
    ) -> bool:
        def mutate(current: Job) -> None:
            current.downloaded_bytes = downloaded
            current.total_bytes = total
            current.speed = speed
            current.eta = eta
            current.fragment_index = fragment_index
            current.fragment_count = fragment_count

        accepted = self._mutate_current(
            job_id, captured_job, attempt, {JobStatus.DOWNLOADING}, mutate,
        )
        if accepted:
            self._persist()
        return accepted

    def attempt_cancelled(self, job_id: str, captured_job: Job, attempt: int) -> bool:
        """Read-only cancellation probe for long-running external tools."""
        with self._lock:
            current = self._jobs.get(job_id)
            return not (
                current is captured_job
                and current._attempt == attempt
                and current.status is JobStatus.DOWNLOADING
                and current.dismissed_at is None
            )

    @staticmethod
    def _set_error(current: Job, exc: Exception) -> None:
        current.status = JobStatus.ERROR
        current.error_category = current.error_category or getattr(exc, "error_category", "unknown")
        current.error_message = current.error_message or str(exc)

    def _invoke_target(self, target, job: Job, attempt: int):
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
            process_field="process",
            register_process=lambda process: self.register_process(
                job.id, job, attempt, process,
            ),
        )
        return target(isolated)

    def _run_attempt(
        self,
        job: Job,
        attempt: int,
        target: Callable[[Job], object],
        *,
        queued_start: bool,
        reservation: _AdmissionLease,
    ) -> None:
        try:
            if queued_start:
                # Preserve the observable QUEUED hand-off used by API callers;
                # attempt validation below still decides whether work may start.
                time.sleep(0.001)
                started = self._mutate_current(
                    job.id, job, attempt, {JobStatus.QUEUED},
                    lambda current: setattr(current, "status", JobStatus.DOWNLOADING),
                )
            else:
                # resume() exposes DOWNLOADING immediately, but the captured
                # identity/attempt still must be live when its worker slot opens.
                # Normalize legacy partials only after the executor accepts this
                # wrapper.  A rejected resume must leave both bytes and its
                # persisted row exactly where they were for a later retry.
                started = self._mutate_current(
                    job.id,
                    job,
                    attempt,
                    {JobStatus.DOWNLOADING},
                    lambda current: self._ensure_download_staging_locked(current),
                )
            if not started:
                return
            self._persist()

            outcome = self._invoke_target(target, job, attempt)
            after_commit = None

            def finish(current: Job) -> None:
                nonlocal after_commit
                if isinstance(outcome, AttemptOutcome):
                    committed = commit_outcome(outcome)
                    apply_updates(current, committed.updates)
                    after_commit = committed.after_commit
                current.status = JobStatus.DONE
                current._was_paused = False
                current.process = None

            accepted = self._mutate_current(
                job.id, job, attempt, {JobStatus.DOWNLOADING}, finish,
            )
            if accepted:
                self._persist()
                if after_commit is not None:
                    try:
                        # Entitlement was captured at the accepted finish
                        # linearization point.  A later dismiss must not suppress
                        # it, and external hooks never run under the state lock.
                        after_commit(job)
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "download post-commit hook failed for %s", job.id, exc_info=True,
                        )
        except Exception as exc:
            accepted = self._mutate_current(
                job.id, job, attempt, {JobStatus.DOWNLOADING},
                lambda current: self._set_error(current, exc),
            )
            if accepted:
                self._persist()
        finally:
            try:
                # Cleanup and worker release share the state lock.  A resume
                # cannot reuse the path until the old attempt has preserved or
                # cleaned its staging root.
                with self._lock:
                    current = self._jobs.get(job.id)
                    if current is job:
                        if current.status is not JobStatus.PAUSED and current._staging_root:
                            try:
                                cleanup_attempt(current._staging_root)
                            except Exception:
                                logging.getLogger(__name__).warning(
                                    "download attempt cleanup failed for %s",
                                    current.id,
                                    exc_info=True,
                                )
                        current.process = None
                        current._worker_active = False
            finally:
                reservation.release()

    def submit(
        self,
        *,
        target: Callable[[Job], None],
        title: str,
        url: str,
        auto_transcribe: bool = False,
        thumbnail: str = "",
        format_choice: str = "video",
        format_id: str | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex[:10]
        job = Job(
            id=job_id, url=url, title=title, status=JobStatus.QUEUED,
            auto_transcribe=auto_transcribe, thumbnail=thumbnail,
            format_choice=format_choice, format_id=format_id,
        )
        reservation = None
        try:
            with self._lock:
                reservation = self._reserve_locked()
                try:
                    self._ensure_download_staging_locked(job)
                    job._attempt += 1
                    attempt = job._attempt
                    job._worker_active = True
                    self._jobs[job_id] = job
                    self._executor.submit(
                        self._run_attempt,
                        job,
                        attempt,
                        target,
                        queued_start=True,
                        reservation=reservation,
                    )
                except Exception:
                    self._jobs.pop(job_id, None)
                    job._worker_active = False
                    if job._staging_root:
                        try:
                            cleanup_attempt(job._staging_root)
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "failed to clean rejected download %s",
                                job_id,
                                exc_info=True,
                            )
                    reservation.release()
                    raise
        except (QueueFullError, RuntimeError):
            # Admission rejection touches neither state nor persistence. An
            # executor RuntimeError happens after provisional state and must
            # persist the rollback; distinguish it by whether a lease existed.
            if reservation is not None:
                self._persist()
            raise
        except Exception:
            self._persist()
            raise
        self._persist()
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is not None:
                j.last_accessed = time.monotonic()
            return j

    def snapshot_jobs(self) -> list[Job]:
        """Return a list copy of the current jobs in insertion order.

        Returns live Job references — the caller may observe torn reads of
        `downloaded_bytes` / `total_bytes` etc. if a worker thread mutates
        them mid-render. That's cosmetic (the next 2s status poll resolves
        it). Used for rendering persisted jobs on page reload.
        """
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        cleanup_root = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in _TERMINAL_STATUSES:
                return False
            proc = job.process
            self._ensure_download_staging_locked(job)
            job._attempt += 1
            job._was_paused = False
            job.status = JobStatus.CANCELLED
            if not job._worker_active:
                cleanup_root = job._staging_root
        if not self._persist():
            raise RuntimeError("failed to persist download cancellation")
        if proc is not None and hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        if cleanup_root is not None:
            try:
                cleanup_attempt(cleanup_root)
            except Exception:
                logging.getLogger(__name__).warning(
                    "failed to clean cancelled download %s",
                    job_id,
                    exc_info=True,
                )
        return True

    def dismiss(self, job_id: str) -> bool:
        """Hide a terminal job from queue projections without deleting history."""
        return self._mark_dismissed(job_id)

    def _mark_dismissed(self, job_id: str) -> bool:
        changed = False
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in _TERMINAL_STATUSES:
                return False
            if job.dismissed_at is None:
                job.dismissed_at = _utc_now_rfc3339()
                changed = True
        self._persist(only_if_dirty=not changed)
        return True

    def pause(self, job_id: str) -> bool:
        """Pause an active or queued job. Keeps .part files for resume.

        Returns True if the job is now paused (or was already paused).
        Returns False if the job is unknown or in a terminal state.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}:
                return False
            if job.status == JobStatus.PAUSED:
                return True  # idempotent
            proc = job.process
            self._ensure_download_staging_locked(job)
            job._attempt += 1
            job._was_paused = True       # tell runner: skip cleanup
            job.status = JobStatus.PAUSED
        # Outside lock: kill the subprocess if any.
        if proc is not None and hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        self._persist()
        return True

    def resume(self, job_id: str, *, target: Callable[[Job], None]) -> bool:
        """Resume a paused job. Re-submits the work target to the executor.

        The caller (app.py) is responsible for constructing the target closure
        from the persisted Job.format_choice / format_id / out_template /
        url / title etc. — this method just re-runs whatever target the
        caller supplies.

        Returns True if the job is now downloading.
        Returns False if the job is unknown or in a terminal state.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}:
                return False
            if job.status == JobStatus.DOWNLOADING:
                return True  # idempotent — already running
            if job._worker_active:
                raise AttemptUnwindingError("paused attempt is still unwinding")
            reservation = self._reserve_locked()
            before = {
                field_name: getattr(job, field_name)
                for field_name in (
                    "_attempt", "status", "_was_paused", "error_category",
                    "error_message", "_worker_active", "_staging_root",
                    "out_template",
                )
            }
            try:
                job._attempt += 1
                attempt = job._attempt
                job.status = JobStatus.DOWNLOADING
                job._was_paused = False
                job.error_category = None
                job.error_message = None
                job._worker_active = True
                self._executor.submit(
                    self._run_attempt,
                    job,
                    attempt,
                    target,
                    queued_start=False,
                    reservation=reservation,
                )
            except Exception as exc:
                for field_name, value in before.items():
                    setattr(job, field_name, value)
                reservation.release()
                submit_error = exc
            else:
                submit_error = None
        if submit_error is not None:
            self._persist()
            raise submit_error
        self._persist()
        return True

    def sweep(self) -> int:
        """Mark newly expired terminal jobs as dismissed without deleting them."""
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

    def start_sweeper(
        self,
        interval_seconds: int = 300,
    ) -> None:
        def loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    self.sweep()
                except Exception:
                    pass
        t = threading.Thread(target=loop, daemon=True, name="trove-sweeper")
        t.start()

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._accepting = False
        self._executor.shutdown(wait=wait)
        if wait:
            with self._lock:
                if self._pending_count != 0:
                    raise RuntimeError("job manager shutdown left pending work")
