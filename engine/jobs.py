from __future__ import annotations
import enum
import glob
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Full
from typing import Callable


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}
_PUBLISHED_SIDECAR_SUFFIXES = (".json", ".srt", ".vtt", ".txt", ".ass")


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


class JobManager:
    def __init__(
        self,
        *,
        max_workers: int = 4,
        ttl_seconds: int = 3600,
        queue_size: int | None = None,
        store_path: object = None,  # Path or None; None disables persistence
    ):
        from pathlib import Path  # local import to avoid module-level coupling
        self.max_workers = max_workers
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, Job] = {}
        # RLock: cancel() persists while already holding the lock, and _persist itself
        # takes the lock to snapshot — reentrancy keeps that legal.
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._inflight = 0
        self._queue_size = queue_size
        self._store_path = Path(store_path) if store_path else None
        if self._store_path is not None:
            self._load_from_store()

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

    def _persist(self) -> None:
        if self._store_path is None:
            return
        try:
            from jobs_store import persist_atomic
            with self._lock:
                snapshot = dict(self._jobs)   # never serialize the live dict unlocked
            persist_atomic(snapshot, self._store_path)
        except Exception:
            # Persistence failure shouldn't crash a download — but a dropped terminal
            # write IS data loss across restart, so say so instead of swallowing.
            logging.getLogger(__name__).warning(
                "job-store persist failed for %s", self._store_path, exc_info=True)

    def submit(
        self,
        *,
        target: Callable[[Job], None],
        title: str,
        url: str,
        auto_transcribe: bool = False,
    ) -> str:
        job_id = uuid.uuid4().hex[:10]
        job = Job(
            id=job_id, url=url, title=title, status=JobStatus.QUEUED,
            auto_transcribe=auto_transcribe,
        )
        with self._lock:
            if self._queue_size == 0 and self._inflight >= self.max_workers:
                raise RuntimeError("pool full")
            self._jobs[job_id] = job
            self._inflight += 1
        self._persist()

        def _run():
            time.sleep(0.001)
            try:
                with self._lock:
                    # The user may have cancelled or paused this job while it sat QUEUED
                    # waiting for a worker slot. Starting it anyway resurrected the job
                    # (and orphaned its yt-dlp subprocess on cancel). Paused stays paused —
                    # resume() is the only sanctioned restart path.
                    if job.status is not JobStatus.QUEUED:
                        return
                    job.status = JobStatus.DOWNLOADING
                self._persist()
                target(job)
                with self._lock:
                    if job.status == JobStatus.PAUSED and job.file_path:
                        # Pause raced runner success — runner already wrote a real file.
                        job.status = JobStatus.DONE
                        job._was_paused = False
                    elif job.status not in {JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED}:
                        job.status = JobStatus.DONE
                self._persist()
            except Exception as e:
                with self._lock:
                    job.status = JobStatus.ERROR
                    job.error_category = job.error_category or "unknown"
                    job.error_message = job.error_message or str(e)
                self._persist()
            finally:
                with self._lock:
                    self._inflight -= 1

        self._executor.submit(_run)
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
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in _TERMINAL_STATUSES:
                return False
            proc = job.process
            out_template = job.out_template
            published_path = os.path.realpath(job.file_path) if job.file_path else None
            job.status = JobStatus.CANCELLED
        if proc is not None and hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        if out_template:
            for candidate in glob.glob(out_template.replace("%(ext)s", "*")):
                if published_path and os.path.realpath(candidate) == published_path:
                    continue
                if candidate.lower().endswith(_PUBLISHED_SIDECAR_SUFFIXES):
                    continue
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        self._persist()
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
        if changed:
            self._persist()
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
            job.status = JobStatus.DOWNLOADING
            job._was_paused = False
            self._inflight += 1
        self._persist()

        def _run():
            try:
                target(job)
                with self._lock:
                    if job.status == JobStatus.PAUSED and job.file_path:
                        # Pause raced runner success — runner already wrote a real file.
                        job.status = JobStatus.DONE
                        job._was_paused = False
                    elif job.status not in {JobStatus.ERROR, JobStatus.CANCELLED, JobStatus.PAUSED}:
                        job.status = JobStatus.DONE
                self._persist()
            except Exception as e:
                with self._lock:
                    job.status = JobStatus.ERROR
                    job.error_category = job.error_category or "unknown"
                    job.error_message = job.error_message or str(e)
                self._persist()
            finally:
                with self._lock:
                    self._inflight -= 1

        self._executor.submit(_run)
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
        if changed:
            self._persist()
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
        self._executor.shutdown(wait=wait)
