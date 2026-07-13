"""TranscribeJob + TranscribeJobManager.

Same lock + ThreadPoolExecutor + JSON persistence pattern as jobs.py.
Operates on parent media jobs by id. Each TranscribeJob has its own
lifecycle independent of the media Job's status.
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
    apply_updates,
    attempt_root,
    cleanup_attempt,
    commit_outcome,
)


class TranscribeStatus(str, enum.Enum):
    QUEUED      = "queued"
    RUNNING     = "running"
    DONE        = "done"
    ERROR       = "error"
    CANCELLED   = "cancelled"


_TERMINAL_STATUSES = {
    TranscribeStatus.DONE, TranscribeStatus.ERROR, TranscribeStatus.CANCELLED,
}


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TranscribeJob:
    id: str
    parent_job_id: str
    model_used: str
    status: TranscribeStatus = TranscribeStatus.QUEUED
    progress_pct: int = 0
    started_at: float = field(default_factory=time.time)
    duration_seconds: float = 0.0
    language_detected: str = ""
    error_category: str | None = None
    error_message: str | None = None
    # Diarization outcome — independent of overall transcribe status.
    # ``None``     → not attempted (feature off or deps missing)
    # "complete"  → ran successfully and chunks were applied
    # "empty"     → ran successfully but no speech detected (no chunks)
    # "failed"    → enabled but raised; error captured in diarization_error
    diarization_status: str | None = None
    diarization_error: str | None = None
    speaker_count: int | None = None
    # Not persisted:
    process_handle: object | None = None
    _cancel_flag: bool = False
    dismissed_at: str | None = None
    last_accessed: float = field(default_factory=time.monotonic)
    _attempt: int = field(default=0, repr=False, compare=False)
    _worker_active: bool = field(default=False, repr=False, compare=False)
    _staging_root: str = field(default="", repr=False, compare=False)


_PERSISTENT_FIELDS = {
    "id", "parent_job_id", "status", "progress_pct", "started_at",
    "duration_seconds", "model_used", "language_detected",
    "error_category", "error_message",
    "dismissed_at",
    "diarization_status", "diarization_error", "speaker_count",
}


class TranscribeJobManager:
    def __init__(self, *, max_workers: int = 1, ttl_seconds: int = 3600,
                 store_path: object = None):
        self.max_workers = max_workers
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, TranscribeJob] = {}
        # _persist() takes the persistence mutex before this state lock.
        # Lifecycle callers release the state lock before entering persistence.
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._persist_dirty = False
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._store_path = Path(store_path) if store_path else None
        self._attempt_base = (
            self._store_path.parent
            if self._store_path is not None
            else Path(tempfile.mkdtemp(prefix="spool-transcribe-attempts-"))
        )
        if self._store_path is not None:
            self._load_from_store()

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
                # Downgrade running → error on restart (whisper has no resume)
                if status_str in ("running", "queued"):
                    raw["status"] = TranscribeStatus.ERROR.value
                    raw["error_category"] = "server_restart"
                    raw["error_message"] = "transcribe interrupted by server restart"
                job = TranscribeJob(
                    id=raw["id"],
                    parent_job_id=raw["parent_job_id"],
                    model_used=raw.get("model_used", ""),
                    status=TranscribeStatus(raw["status"]),
                    progress_pct=raw.get("progress_pct", 0),
                    started_at=raw.get("started_at", 0.0),
                    duration_seconds=raw.get("duration_seconds", 0.0),
                    language_detected=raw.get("language_detected", ""),
                    error_category=raw.get("error_category"),
                    error_message=raw.get("error_message"),
                    dismissed_at=raw.get("dismissed_at"),
                    diarization_status=raw.get("diarization_status"),
                    diarization_error=raw.get("diarization_error"),
                    speaker_count=raw.get("speaker_count"),
                )
                self._jobs[jid] = job
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
                fd, tmp = tempfile.mkstemp(prefix=".tj.", dir=str(self._store_path.parent))
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(payload, f, indent=2)
                    os.replace(tmp, self._store_path)
                except Exception:
                    try: os.unlink(tmp)
                    except OSError: pass
                    raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "transcribe-store persist failed for %s", self._store_path, exc_info=True)
                return False
            self._persist_dirty = False
            return True

    def _prepare_attempt_locked(self, job: TranscribeJob) -> tuple[int, Path]:
        root = attempt_root(self._attempt_base, "transcribe", job.id)
        root.mkdir(parents=True, exist_ok=True)
        job._staging_root = str(root)
        job._attempt += 1
        job._worker_active = True
        return job._attempt, root

    def _mutate_current(
        self,
        jid: str,
        captured_job: TranscribeJob,
        attempt: int,
        expected: set[TranscribeStatus],
        mutate: Callable[[TranscribeJob], None],
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

    def register_process(
        self, jid: str, captured_job: TranscribeJob, attempt: int, process,
    ) -> bool:
        accepted = self._mutate_current(
            jid, captured_job, attempt, {TranscribeStatus.RUNNING},
            lambda current: setattr(current, "process_handle", process),
        )
        if not accepted and process is not None and hasattr(process, "kill"):
            try:
                process.kill()
            except Exception:
                pass
        return accepted

    def attempt_cancelled(self, jid: str, captured_job: TranscribeJob, attempt: int) -> bool:
        with self._lock:
            current = self._jobs.get(jid)
            return not (
                current is captured_job
                and current._attempt == attempt
                and current.status is TranscribeStatus.RUNNING
                and current.dismissed_at is None
            )

    @staticmethod
    def _set_error(current: TranscribeJob, exc: Exception) -> None:
        current.status = TranscribeStatus.ERROR
        current.error_category = current.error_category or getattr(exc, "error_category", "unknown")
        current.error_message = current.error_message or str(exc)

    @staticmethod
    def _invoke_target(target, job: TranscribeJob, model_path: str, attempt: int):
        try:
            parameters = inspect.signature(target).parameters.values()
            accepts_attempt = any(
                parameter.name == "attempt"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_attempt = False
        if accepts_attempt:
            return target(job, model_path=model_path, attempt=attempt)
        return target(job, model_path=model_path)

    def _run_attempt(
        self,
        job: TranscribeJob,
        attempt: int,
        model_path: str,
        target: Callable[[TranscribeJob], object],
    ) -> None:
        try:
            started = self._mutate_current(
                job.id, job, attempt, {TranscribeStatus.QUEUED},
                lambda current: setattr(current, "status", TranscribeStatus.RUNNING),
            )
            if not started:
                return
            self._persist()

            outcome = self._invoke_target(target, job, model_path, attempt)
            after_commit = None

            def finish(current: TranscribeJob) -> None:
                nonlocal after_commit
                if isinstance(outcome, AttemptOutcome):
                    committed = commit_outcome(outcome)
                    apply_updates(current, committed.updates)
                    after_commit = committed.after_commit
                current.status = TranscribeStatus.DONE
                current.progress_pct = 100
                current.process_handle = None

            accepted = self._mutate_current(
                job.id, job, attempt, {TranscribeStatus.RUNNING}, finish,
            )
            if accepted:
                self._persist()
                if after_commit is not None:
                    try:
                        self._mutate_current(
                            job.id, job, attempt, {TranscribeStatus.DONE}, after_commit,
                        )
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "transcribe post-commit hook failed for %s", job.id, exc_info=True,
                        )
        except Exception as exc:
            accepted = self._mutate_current(
                job.id, job, attempt, {TranscribeStatus.RUNNING},
                lambda current: self._set_error(current, exc),
            )
            if accepted:
                self._persist()
        finally:
            with self._lock:
                current = self._jobs.get(job.id)
                if current is job:
                    if current._staging_root:
                        cleanup_attempt(current._staging_root)
                    current.process_handle = None
                    current._worker_active = False

    # ----- lifecycle -----------------------------------------------------

    def submit(self, *, parent_job_id: str, model_path: str,
               target: Callable[[TranscribeJob], None]) -> str:
        jid = uuid.uuid4().hex[:10]
        model_name = Path(model_path).name if model_path else ""
        job = TranscribeJob(id=jid, parent_job_id=parent_job_id, model_used=model_name)
        with self._lock:
            attempt, _root = self._prepare_attempt_locked(job)
            self._jobs[jid] = job
        self._persist()
        try:
            self._executor.submit(self._run_attempt, job, attempt, model_path, target)
        except Exception:
            with self._lock:
                self._jobs.pop(jid, None)
                job._worker_active = False
                cleanup_attempt(job._staging_root)
            self._persist()
            raise
        return jid

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return False
            if j.status in {TranscribeStatus.DONE, TranscribeStatus.ERROR, TranscribeStatus.CANCELLED}:
                return False
            j._cancel_flag = True
            j._attempt += 1
            j.status = TranscribeStatus.CANCELLED
            proc = j.process_handle
            if not j._worker_active and j._staging_root:
                cleanup_attempt(j._staging_root)
        if proc is not None and hasattr(proc, "kill"):
            try: proc.kill()
            except Exception: pass
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

    def get(self, jid: str) -> TranscribeJob | None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is not None:
                j.last_accessed = time.monotonic()
            return j

    def get_by_parent(self, parent_job_id: str) -> TranscribeJob | None:
        """Return the most recent TranscribeJob for this parent, if any."""
        with self._lock:
            matching = [j for j in self._jobs.values() if j.parent_job_id == parent_job_id]
        if not matching:
            return None
        return max(matching, key=lambda j: j.started_at)

    def snapshot_jobs(self) -> list[TranscribeJob]:
        with self._lock:
            return list(self._jobs.values())

    def update_progress(self, jid: str, *args) -> bool:
        """Guarded progress update.

        Production callbacks pass ``(captured_job, attempt, pct)``.  The
        two-argument legacy form remains for external callers and captures the
        current identity/attempt while already under the same state lock.
        """
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

        accepted = self._mutate_current(
            jid, captured, attempt, {TranscribeStatus.RUNNING},
            lambda current: setattr(
                current, "progress_pct", max(0, min(100, int(pct))),
            ),
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
            target=loop, daemon=True, name="trove-transcribe-sweeper",
        ).start()

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)
