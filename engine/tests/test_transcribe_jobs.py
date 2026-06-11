import os
import time
import json
import pytest
from pathlib import Path
from transcribe_jobs import (
    TranscribeJob, TranscribeStatus, TranscribeJobManager
)


def test_status_enum_values():
    assert TranscribeStatus.QUEUED.value == "queued"
    assert TranscribeStatus.RUNNING.value == "running"
    assert TranscribeStatus.DONE.value == "done"
    assert TranscribeStatus.ERROR.value == "error"
    assert TranscribeStatus.CANCELLED.value == "cancelled"


def test_dataclass_defaults():
    j = TranscribeJob(id="x", parent_job_id="p", model_used="ggml-base.bin")
    assert j.status == TranscribeStatus.QUEUED
    assert j.progress_pct == 0
    assert j.language_detected == ""
    assert j.process_handle is None


def test_submit_returns_id_and_runs(tmp_path):
    jm = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    runs = []
    jid = jm.submit(
        parent_job_id="abc",
        model_path=str(tmp_path / "fake.bin"),
        target=lambda j, **_: runs.append(j.id),
    )
    assert isinstance(jid, str) and len(jid) == 10
    for _ in range(50):
        if jm.get(jid).status == TranscribeStatus.DONE:
            break
        time.sleep(0.05)
    assert runs == [jid]
    jm.shutdown()


def test_cancel_marks_cancelled(tmp_path):
    jm = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    jid = jm.submit(
        parent_job_id="abc",
        model_path=str(tmp_path / "fake.bin"),
        target=lambda j, **_: time.sleep(2),
    )
    time.sleep(0.1)  # let it start
    assert jm.cancel(jid) is True
    assert jm.get(jid).status == TranscribeStatus.CANCELLED
    jm.shutdown()


def test_persistence_round_trip(tmp_path):
    store = tmp_path / "tj.json"
    jm = TranscribeJobManager(max_workers=1, store_path=store)
    jm.submit(
        parent_job_id="p1",
        model_path=str(tmp_path / "fake.bin"),
        target=lambda j, **_: None,
    )
    for _ in range(50):
        if any(j.status == TranscribeStatus.DONE for j in jm.snapshot_jobs()):
            break
        time.sleep(0.05)
    jm.shutdown()

    # Reopen — snapshot survives
    jm2 = TranscribeJobManager(max_workers=1, store_path=store)
    snap = jm2.snapshot_jobs()
    assert len(snap) == 1
    assert snap[0].parent_job_id == "p1"
    jm2.shutdown()


def test_running_at_restart_downgrades_to_error(tmp_path):
    """A job stuck in RUNNING from a crashed process becomes ERROR on reload."""
    store = tmp_path / "tj.json"
    payload = {
        "schema_version": 1,
        "jobs": {
            "stuck1": {
                "id": "stuck1",
                "parent_job_id": "abc",
                "status": "running",
                "progress_pct": 50,
                "started_at": 0.0,
                "duration_seconds": 0.0,
                "model_used": "ggml-base.bin",
                "language_detected": "",
                "error_category": None,
                "error_message": None,
            }
        },
    }
    store.write_text(json.dumps(payload))

    jm = TranscribeJobManager(max_workers=1, store_path=store)
    j = jm.get("stuck1")
    assert j is not None
    assert j.status == TranscribeStatus.ERROR
    assert j.error_category == "server_restart"
    jm.shutdown()


def test_dismiss_removes_terminal_job(tmp_path):
    jm = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    jid = jm.submit(
        parent_job_id="abc",
        model_path=str(tmp_path / "fake.bin"),
        target=lambda j, **_: None,
    )
    for _ in range(50):
        if jm.get(jid).status == TranscribeStatus.DONE:
            break
        time.sleep(0.05)
    assert jm.dismiss(jid) is True
    assert jm.get(jid) is None
    jm.shutdown()


def test_dismiss_refuses_running(tmp_path):
    jm = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    jid = jm.submit(
        parent_job_id="abc",
        model_path=str(tmp_path / "fake.bin"),
        target=lambda j, **_: time.sleep(2),
    )
    time.sleep(0.1)
    assert jm.dismiss(jid) is False
    jm.shutdown()


def test_cancel_with_live_process_handle_still_persists(tmp_path):
    """asdict() used to deep-copy process_handle (a live Popen → TypeError: cannot
    pickle _thread.lock), silently dropping the CANCELLED write — the store kept
    'running' and the job resurfaced as a spurious error after restart."""
    import threading as _threading
    class _FakeProc:
        def __init__(self):
            self._lock = _threading.Lock()   # undeepcopyable, like a real Popen
        def kill(self):
            pass
    store = tmp_path / "tj.json"
    mgr = TranscribeJobManager(store_path=store)
    gate = _threading.Event()
    def target(job, model_path):
        job.process_handle = _FakeProc()
        gate.wait(5)
    jid = mgr.submit(parent_job_id="p", model_path="m.bin", target=target)
    deadline = time.time() + 5
    while mgr.get(jid).status is not TranscribeStatus.RUNNING and time.time() < deadline:
        time.sleep(0.01)
    assert mgr.cancel(jid) is True
    gate.set()
    data = json.loads(store.read_text())
    assert data["jobs"][jid]["status"] == "cancelled"   # the write must survive the live handle
