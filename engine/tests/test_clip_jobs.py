"""Tests for ClipJob + ClipJobManager — the clip/render job queue.

Mirrors test_transcribe_jobs.py: same lock + ThreadPoolExecutor + atomic-JSON
persistence + restart-downgrade machinery, extended with a ``kind`` and
``params``/``result`` dicts so one manager drives all six clip operations.
"""
import json
import threading
import time

import pytest

from clip_jobs import ClipJob, ClipStatus, ClipJobManager, CLIP_KINDS


def test_status_enum_values():
    assert ClipStatus.QUEUED.value == "queued"
    assert ClipStatus.RUNNING.value == "running"
    assert ClipStatus.DONE.value == "done"
    assert ClipStatus.ERROR.value == "error"
    assert ClipStatus.CANCELLED.value == "cancelled"


def test_kinds_cover_the_engine_chain():
    # "produce" = the Phase-3 recipe fan-out (find→rank→top-N→pipeline per moment).
    assert CLIP_KINDS == {"moments", "cut", "reframe", "caption", "export", "pipeline", "produce"}


def test_dataclass_defaults():
    j = ClipJob(id="x", kind="cut", source_id="src1")
    assert j.status == ClipStatus.QUEUED
    assert j.progress_pct == 0
    assert j.params == {} and j.result == {}
    assert j.clip_id is None and j.stage == ""
    assert j.process_handle is None
    assert j.dismissed_at is None


def test_submit_returns_id_and_runs(tmp_path):
    jm = ClipJobManager(max_workers=2, store_path=tmp_path / "clip.json")
    ran = []
    jid = jm.submit(kind="moments", source_id="abc",
                    params={"mode": "funny"}, target=lambda j: ran.append(j.id))
    assert isinstance(jid, str) and len(jid) == 10
    _await(jm, jid, ClipStatus.DONE)
    assert ran == [jid]
    assert jm.get(jid).progress_pct == 100
    jm.shutdown()


def test_submit_rejects_unknown_kind(tmp_path):
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    with pytest.raises(ValueError, match="unknown clip kind"):
        jm.submit(kind="bogus", target=lambda j: None)
    jm.shutdown()


def test_target_can_write_result_and_clip_id(tmp_path):
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")

    def target(j):
        j.clip_id = "clip_xyz"
        j.result = {"clip_path": "/tmp/clip.mp4", "candidates": [{"start": 1.0}]}

    jid = jm.submit(kind="cut", source_id="s", target=target)
    _await(jm, jid, ClipStatus.DONE)
    j = jm.get(jid)
    assert j.clip_id == "clip_xyz"
    assert j.result["clip_path"] == "/tmp/clip.mp4"
    jm.shutdown()


def test_target_exception_marks_error(tmp_path):
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")

    def boom(j):
        raise RuntimeError("ffmpeg exploded")

    jid = jm.submit(kind="export", target=boom)
    _await(jm, jid, ClipStatus.ERROR)
    j = jm.get(jid)
    assert j.error_message == "ffmpeg exploded"
    assert j.error_category == "unknown"
    jm.shutdown()


def test_cancel_marks_cancelled_and_kills_proc(tmp_path):
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    killed = []
    registered = threading.Event()

    class _Proc:
        def kill(self):
            killed.append(True)

    def target(j):
        j.process_handle = _Proc()
        registered.set()  # the live subprocess is now registered for cancel
        time.sleep(2)

    jid = jm.submit(kind="reframe", target=target)
    assert registered.wait(2)
    assert jm.cancel(jid) is True
    assert jm.get(jid).status == ClipStatus.CANCELLED
    assert killed == [True]
    jm.shutdown()


@pytest.mark.parametrize("status", [ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED])
def test_cancel_terminal_is_noop(status, tmp_path):
    artifact = tmp_path / "render.mp4"
    artifact.write_bytes(b"published-render")
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    job = ClipJob(id="terminal", kind="export", clip_id="clip", status=status,
                  result={"output_path": str(artifact)})
    with jm._lock:
        jm._jobs[job.id] = job
    assert jm.cancel(job.id) is False
    assert jm.get(job.id).status is status
    assert artifact.read_bytes() == b"published-render"
    jm.shutdown()


def test_update_progress_clamps_and_sets_stage(tmp_path):
    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    gate = threading.Event()
    jid = jm.submit(kind="pipeline", target=lambda j: gate.wait(2))
    _await(jm, jid, ClipStatus.RUNNING)
    jm.update_progress(jid, 250, stage="reframe")
    j = jm.get(jid)
    assert j.progress_pct == 100 and j.stage == "reframe"
    gate.set()
    jm.shutdown(wait=True)


def test_persistence_round_trip(tmp_path):
    store = tmp_path / "clip.json"
    jm = ClipJobManager(max_workers=1, store_path=store)

    def target(j):
        j.clip_id = "c1"
        j.result = {"render_id": "r1"}

    jm.submit(kind="export", source_id="p1", params={"preset": "tiktok"}, target=target)
    for _ in range(50):
        if any(j.status == ClipStatus.DONE for j in jm.snapshot_jobs()):
            break
        time.sleep(0.05)
    jm.shutdown(wait=True)

    jm2 = ClipJobManager(max_workers=1, store_path=store)
    snap = jm2.snapshot_jobs()
    assert len(snap) == 1
    j = snap[0]
    assert j.kind == "export" and j.source_id == "p1"
    assert j.params == {"preset": "tiktok"} and j.result == {"render_id": "r1"}
    assert j.clip_id == "c1"
    jm2.shutdown()


def test_running_at_restart_downgrades_to_error(tmp_path):
    store = tmp_path / "clip.json"
    payload = {
        "schema_version": 1,
        "jobs": {
            "stuck1": {
                "id": "stuck1", "kind": "reframe", "source_id": "abc", "clip_id": "c",
                "status": "running", "progress_pct": 50, "stage": "rendering",
                "started_at": 0.0, "params": {}, "result": {},
                "error_category": None, "error_message": None,
            }
        },
    }
    store.write_text(json.dumps(payload))
    jm = ClipJobManager(max_workers=1, store_path=store)
    j = jm.get("stuck1")
    assert j is not None and j.status == ClipStatus.ERROR
    assert j.error_category == "server_restart"
    jm.shutdown()


def test_dismiss_marks_terminal_idempotently_and_refuses_running(tmp_path, monkeypatch):
    import clip_jobs as module
    monkeypatch.setattr(module, "_utc_now_rfc3339", lambda: "2026-07-13T12:34:56.789Z", raising=False)
    store = tmp_path / "clip.json"
    jm = ClipJobManager(max_workers=1, store_path=store)
    done = jm.submit(kind="cut", target=lambda j: None)
    _await(jm, done, ClipStatus.DONE)
    assert jm.dismiss(done) is True
    assert jm.get(done).dismissed_at == "2026-07-13T12:34:56.789Z"
    assert jm.dismiss(done) is True
    assert jm.get(done).dismissed_at == "2026-07-13T12:34:56.789Z"

    gate = threading.Event()
    running = jm.submit(kind="cut", target=lambda j: gate.wait(2))
    _await(jm, running, ClipStatus.RUNNING)
    assert jm.dismiss(running) is False
    gate.set()
    jm.shutdown(wait=True)

    restarted = ClipJobManager(max_workers=1, store_path=store)
    assert restarted.get(done).dismissed_at == "2026-07-13T12:34:56.789Z"
    restarted.shutdown()


def test_ttl_sweep_marks_terminal_once_and_preserves_artifacts(tmp_path):
    artifact = tmp_path / "render.mp4"
    artifact.write_bytes(b"published-render")
    jm = ClipJobManager(max_workers=1, ttl_seconds=0, store_path=tmp_path / "clip.json")
    job = ClipJob(id="done", kind="export", clip_id="clip", status=ClipStatus.DONE,
                  result={"output_path": str(artifact)})
    with jm._lock:
        jm._jobs[job.id] = job
    assert jm.sweep() == 1
    assert jm.get(job.id).dismissed_at is not None
    assert artifact.read_bytes() == b"published-render"
    assert jm.sweep() == 0
    jm.shutdown()


def test_lookups_by_clip_and_source(tmp_path):
    jm = ClipJobManager(max_workers=2, store_path=tmp_path / "clip.json")
    a = jm.submit(kind="reframe", source_id="srcA", clip_id="clip1", target=lambda j: None)
    b = jm.submit(kind="caption", source_id="srcA", clip_id="clip1", target=lambda j: None)
    c = jm.submit(kind="cut", source_id="srcB", target=lambda j: None)
    for jid in (a, b, c):
        _await(jm, jid, ClipStatus.DONE)
    assert {j.id for j in jm.get_by_clip("clip1")} == {a, b}
    assert {j.id for j in jm.get_by_source("srcA")} == {a, b}
    assert {j.id for j in jm.get_by_source("srcB")} == {c}
    jm.shutdown()


def test_cancel_with_live_process_handle_still_persists(tmp_path):
    """asdict() used to deep-copy process_handle (a live Popen → TypeError: cannot
    pickle _thread.lock), silently dropping the CANCELLED write — the store kept
    'running' and the job resurfaced as a spurious error after restart.

    Determinism: the store is read BEFORE gate.set(), so the worker is still parked
    inside target() and cannot race cancel()'s persist.  With the old asdict code the
    CANCELLED write raises TypeError and is silently dropped, so the file still says
    'running' and this assertion fails reliably.
    """
    class _FakeProc:
        def __init__(self):
            self._lock = threading.Lock()   # undeepcopyable, like a real Popen
        def kill(self):
            pass
    store = tmp_path / "clip.json"
    mgr = ClipJobManager(store_path=store)
    gate = threading.Event()
    def target(job):
        job.process_handle = _FakeProc()
        gate.wait(5)
    jid = mgr.submit(kind="cut", source_id="src1", params={}, target=target)
    # Poll until the handle is attached — worker is now parked inside target().
    deadline = time.time() + 5
    while mgr.get(jid).process_handle is None and time.time() < deadline:
        time.sleep(0.01)
    assert mgr.get(jid).process_handle is not None, "worker never attached handle"
    # Cancel while worker is parked: cancel()'s own _persist() runs now, no race.
    assert mgr.cancel(jid) is True
    # Assert the store NOW — before releasing the gate — so no competing writer exists.
    data = json.loads(store.read_text())
    assert data["jobs"][jid]["status"] == "cancelled"   # the write must survive the live handle
    # Clean up: release the worker and drain the thread pool.
    gate.set()
    mgr.shutdown(wait=True)


def _await(jm, jid, status, tries=100):
    for _ in range(tries):
        j = jm.get(jid)
        if j is not None and j.status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {jid} never reached {status}; last={jm.get(jid).status if jm.get(jid) else None}")
