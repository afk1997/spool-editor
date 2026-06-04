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
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


class ClipStatus(str, enum.Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


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
    started_at: float = field(default_factory=time.monotonic)
    params: dict = field(default_factory=dict)   # kind-specific inputs
    result: dict = field(default_factory=dict)   # kind-specific outputs
    error_category: str | None = None
    error_message: str | None = None
    # Not persisted:
    process_handle: object | None = None
    _cancel_flag: bool = False


_PERSISTENT_FIELDS = {
    "id", "kind", "source_id", "clip_id", "status", "progress_pct", "stage",
    "started_at", "params", "result", "error_category", "error_message",
}


class ClipJobManager:
    def __init__(self, *, max_workers: int = 2, store_path: object = None):
        self.max_workers = max_workers
        self._jobs: dict[str, ClipJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._store_path = Path(store_path) if store_path else None
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
                )
            except (KeyError, ValueError):
                continue

    def _persist(self) -> None:
        if self._store_path is None:
            return
        try:
            with self._lock:
                payload = {
                    "schema_version": 1,
                    "jobs": {
                        jid: {**{k: v for k, v in asdict(j).items() if k in _PERSISTENT_FIELDS},
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
            pass  # persistence failure must never crash a clip job

    # ----- lifecycle -----------------------------------------------------

    def submit(self, *, kind: str, target: Callable[[ClipJob], None],
               source_id: str | None = None, clip_id: str | None = None,
               params: dict | None = None) -> str:
        if kind not in CLIP_KINDS:
            raise ValueError(f"unknown clip kind {kind!r}; expected one of {sorted(CLIP_KINDS)}")
        jid = uuid.uuid4().hex[:10]
        job = ClipJob(id=jid, kind=kind, source_id=source_id, clip_id=clip_id,
                      params=params or {})
        with self._lock:
            self._jobs[jid] = job
        self._persist()

        def _run():
            try:
                with self._lock:
                    job.status = ClipStatus.RUNNING
                self._persist()
                target(job)
                with self._lock:
                    if job.status not in {ClipStatus.CANCELLED, ClipStatus.ERROR}:
                        job.status = ClipStatus.DONE
                        job.progress_pct = 100
                self._persist()
            except Exception as e:
                with self._lock:
                    job.status = ClipStatus.ERROR
                    job.error_category = job.error_category or "unknown"
                    job.error_message = job.error_message or str(e)
                self._persist()

        self._executor.submit(_run)
        return jid

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return False
            if j.status in {ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED}:
                return False
            j._cancel_flag = True
            j.status = ClipStatus.CANCELLED
            proc = j.process_handle
        if proc is not None and hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        self._persist()
        return True

    def dismiss(self, jid: str) -> bool:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return False
            if j.status not in {ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED}:
                return False
            del self._jobs[jid]
        self._persist()
        return True

    def get(self, jid: str) -> ClipJob | None:
        with self._lock:
            return self._jobs.get(jid)

    def get_by_clip(self, clip_id: str) -> list[ClipJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.clip_id == clip_id]

    def get_by_source(self, source_id: str) -> list[ClipJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.source_id == source_id]

    def snapshot_jobs(self) -> list[ClipJob]:
        with self._lock:
            return list(self._jobs.values())

    def update_progress(self, jid: str, pct: int, *, stage: str | None = None) -> None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is not None and j.status == ClipStatus.RUNNING:
                j.progress_pct = max(0, min(100, int(pct)))
                if stage is not None:
                    j.stage = stage

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)
