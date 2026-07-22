import os
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from pathlib import Path
from job_capacity import QueueFullError
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
    assert j.dismissed_at is None


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


@pytest.mark.parametrize("status", [TranscribeStatus.DONE, TranscribeStatus.ERROR, TranscribeStatus.CANCELLED])
def test_cancel_terminal_is_noop(status, tmp_path):
    artifact = tmp_path / "source.words.json"
    artifact.write_bytes(b"published-transcript")
    jm = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    job = TranscribeJob(id="terminal", parent_job_id="source", model_used="m", status=status)
    with jm._lock:
        jm._jobs[job.id] = job
    assert jm.cancel(job.id) is False
    assert jm.get(job.id).status is status
    assert artifact.read_bytes() == b"published-transcript"
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


def test_dismiss_marks_terminal_job_idempotently_and_persists(tmp_path, monkeypatch):
    import transcribe_jobs as module
    monkeypatch.setattr(module, "_utc_now_rfc3339", lambda: "2026-07-13T12:34:56.789Z", raising=False)
    store = tmp_path / "tj.json"
    jm = TranscribeJobManager(max_workers=1, store_path=store)
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
    assert jm.get(jid).dismissed_at == "2026-07-13T12:34:56.789Z"
    assert jm.dismiss(jid) is True
    assert jm.get(jid).dismissed_at == "2026-07-13T12:34:56.789Z"
    jm.shutdown()

    restarted = TranscribeJobManager(max_workers=1, store_path=store)
    assert restarted.get(jid).dismissed_at == "2026-07-13T12:34:56.789Z"
    restarted.shutdown()


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


def test_ttl_sweep_marks_terminal_once_and_preserves_artifacts(tmp_path):
    artifact = tmp_path / "source.srt"
    artifact.write_bytes(b"published-srt")
    jm = TranscribeJobManager(max_workers=1, ttl_seconds=0, store_path=tmp_path / "tj.json")
    job = TranscribeJob(id="done", parent_job_id="source", model_used="m", status=TranscribeStatus.DONE)
    with jm._lock:
        jm._jobs[job.id] = job
    assert jm.sweep() == 1
    assert jm.get(job.id).dismissed_at is not None
    assert artifact.read_bytes() == b"published-srt"
    assert jm.sweep() == 0
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


def test_cancel_persist_failure_restores_transcribe_state_and_preserves_staging(
    tmp_path, monkeypatch,
):
    import transcribe_jobs as module

    store = tmp_path / "tj.json"
    manager = TranscribeJobManager(max_workers=1, store_path=store)
    job = TranscribeJob(
        id="queued-persist-failure",
        parent_job_id="source",
        model_used="model.bin",
        status=TranscribeStatus.QUEUED,
    )
    job._attempt = 7
    root = tmp_path / ".attempts" / "transcribe" / job.id
    root.mkdir(parents=True)
    candidate = root / "source.words.json"
    candidate.write_bytes(b"candidate")
    job._staging_root = str(root)
    with manager._lock:
        manager._jobs[job.id] = job
    assert manager._persist() is True
    before = (job.status, job._attempt, job._cancel_flag)

    with monkeypatch.context() as patch:
        patch.setattr(
            module.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced persist failure")
            ),
        )
        with pytest.raises(RuntimeError, match="persist transcribe cancellation"):
            manager.cancel(job.id)

    assert (job.status, job._attempt, job._cancel_flag) == before
    assert candidate.read_bytes() == b"candidate"
    assert json.loads(store.read_text())["jobs"][job.id]["status"] == "queued"

    before_retry = TranscribeJobManager(max_workers=1, store_path=store)
    try:
        assert before_retry.get(job.id).status is TranscribeStatus.ERROR
    finally:
        before_retry.shutdown(wait=True)

    cleanup_states = []

    def fail_cleanup(path):
        cleanup_states.append(
            (path, json.loads(store.read_text())["jobs"][job.id]["status"])
        )
        raise OSError("forced cleanup failure")

    monkeypatch.setattr(module, "cleanup_attempt", fail_cleanup)
    assert manager.cancel(job.id) is True
    assert (job.status, job._attempt, job._cancel_flag) == (
        TranscribeStatus.CANCELLED,
        before[1] + 1,
        True,
    )
    assert cleanup_states == [(str(root), "cancelled")]
    assert candidate.read_bytes() == b"candidate"
    manager.shutdown(wait=True)

    after_retry = TranscribeJobManager(max_workers=1, store_path=store)
    try:
        assert after_retry.get(job.id).status is TranscribeStatus.CANCELLED
    finally:
        after_retry.shutdown(wait=True)


def test_cancel_active_persist_failure_does_not_kill_transcribe_process(
    tmp_path, monkeypatch,
):
    import transcribe_jobs as module

    store = tmp_path / "tj.json"
    manager = TranscribeJobManager(max_workers=1, store_path=store)
    killed_after_status = []

    class Process:
        def kill(self):
            killed_after_status.append(
                json.loads(store.read_text())["jobs"][job.id]["status"]
            )

    process = Process()
    job = TranscribeJob(
        id="active-persist-failure",
        parent_job_id="source",
        model_used="model.bin",
        status=TranscribeStatus.RUNNING,
        process_handle=process,
    )
    job._attempt = 11
    job._worker_active = True
    with manager._lock:
        manager._jobs[job.id] = job
    assert manager._persist() is True
    before = (job.status, job._attempt, job._cancel_flag)

    with monkeypatch.context() as patch:
        patch.setattr(
            module.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced persist failure")
            ),
        )
        with pytest.raises(RuntimeError, match="persist transcribe cancellation"):
            manager.cancel(job.id)

    assert (job.status, job._attempt, job._cancel_flag) == before
    assert manager.get(job.id).process_handle is process
    assert killed_after_status == []

    assert manager.cancel(job.id) is True
    assert killed_after_status == ["cancelled"]
    manager.shutdown(wait=True)

    restarted = TranscribeJobManager(max_workers=1, store_path=store)
    try:
        assert restarted.get(job.id).status is TranscribeStatus.CANCELLED
    finally:
        restarted.shutdown(wait=True)


def _wait_tj(mgr, jid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mgr.get(jid).status in (TranscribeStatus.DONE, TranscribeStatus.ERROR):
            return
        time.sleep(0.01)


def test_started_at_is_wall_clock(tmp_path):
    mgr = TranscribeJobManager(store_path=tmp_path / "tj.json")
    jid = mgr.submit(parent_job_id="p", model_path="m.bin", target=lambda j, model_path: None)
    job = mgr.get(jid)
    assert abs(job.started_at - time.time()) < 10, "started_at must be epoch seconds, not monotonic"


def test_get_by_parent_prefers_most_recent_across_restart(tmp_path):
    store = tmp_path / "tj.json"
    m1 = TranscribeJobManager(store_path=store)
    a = m1.submit(parent_job_id="p", model_path="m.bin", target=lambda j, model_path: None)
    _wait_tj(m1, a)
    time.sleep(0.02)
    b = m1.submit(parent_job_id="p", model_path="m.bin", target=lambda j, model_path: None)
    _wait_tj(m1, b)
    m2 = TranscribeJobManager(store_path=store)   # restart: ordering must survive the reload
    got = m2.get_by_parent("p")
    assert got is not None and got.id == b


def _wait_worker_inactive(mgr, jid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not mgr.get(jid)._worker_active:
            return True
        time.sleep(0.01)
    return False


def test_transcribe_success_is_not_visible_before_attempt_cleanup(tmp_path, monkeypatch):
    from attempt_staging import AttemptOutcome, Promotion, cleanup_attempt as real_cleanup
    import transcribe_jobs as transcribe_module

    manager = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    published = tmp_path / "source.words.json"

    def delayed_cleanup(root):
        cleanup_started.set()
        assert release_cleanup.wait(5), "test did not release staging cleanup"
        real_cleanup(root)

    def target(job, *, model_path, attempt):
        staged = Path(job._staging_root) / published.name
        staged.write_bytes(b"published")
        return AttemptOutcome(
            updates={"duration_seconds": 1.0},
            promotions=(Promotion(staged, published),),
        )

    monkeypatch.setattr(transcribe_module, "cleanup_attempt", delayed_cleanup)
    jid = manager.submit(parent_job_id="source", model_path="m.bin", target=target)
    captured = manager._jobs[jid]
    try:
        assert cleanup_started.wait(2), "worker never reached staging cleanup"
        assert captured.status is TranscribeStatus.RUNNING
        release_cleanup.set()
        _wait_tj(manager, jid)
        assert manager.get(jid).status is TranscribeStatus.DONE
        assert not Path(manager.get(jid)._staging_root).exists()
        assert published.read_bytes() == b"published"
    finally:
        release_cleanup.set()
        manager.shutdown(wait=True)


def test_queued_cancel_transcribe_never_runs_target(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    gate = threading.Event()
    ran = []
    first = mgr.submit(parent_job_id="a", model_path="m.bin",
                       target=lambda job, **_: gate.wait(5))
    cancelled = mgr.submit(parent_job_id="b", model_path="m.bin",
                           target=lambda job, **_: ran.append(job.id))
    assert mgr.get(cancelled).status is TranscribeStatus.QUEUED
    assert mgr.cancel(cancelled) is True
    gate.set()
    _wait_tj(mgr, first)
    assert _wait_worker_inactive(mgr, cancelled)
    assert ran == []
    assert mgr.get(cancelled).status is TranscribeStatus.CANCELLED
    mgr.shutdown(wait=True)


@pytest.mark.parametrize("raises", [False, True])
def test_running_cancel_transcribe_rejects_result_error_and_progress(tmp_path, raises):
    from attempt_staging import AttemptOutcome, Promotion

    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    published = tmp_path / "source.words.json"
    published.write_bytes(b"old-words")
    started = threading.Event()
    release = threading.Event()

    def target(job, **_):
        attempt = job._attempt
        staged = Path(job._staging_root) / "source.words.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"new-words")
        started.set()
        release.wait(5)
        mgr.update_progress(job.id, job, attempt, 91)
        if raises:
            raise RuntimeError("late transcribe error")
        return AttemptOutcome(
            updates={
                "duration_seconds": 12.0,
                "language_detected": "en",
                "diarization_status": "complete",
                "speaker_count": 2,
            },
            promotions=(Promotion(staged, published),),
        )

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    job = mgr.get(jid)
    assert job.status is TranscribeStatus.CANCELLED
    assert job.progress_pct == 0
    assert job.duration_seconds == 0.0 and job.language_detected == ""
    assert job.diarization_status is None and job.speaker_count is None
    assert job.error_category is None and job.error_message is None
    assert published.read_bytes() == b"old-words"
    assert not Path(job._staging_root).exists()
    mgr.shutdown(wait=True)


def test_stale_attempt_transcribe_late_process_registration_is_killed(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    started = threading.Event()
    release = threading.Event()
    killed = []

    class Proc:
        def kill(self):
            killed.append(True)

    def target(job, **_):
        attempt = job._attempt
        started.set()
        release.wait(5)
        assert mgr.register_process(job.id, job, attempt, Proc()) is False

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert killed == [True]
    assert mgr.get(jid).process_handle is None
    mgr.shutdown(wait=True)


def test_stale_attempt_transcribe_target_receives_dispatch_token(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def target(job, *, model_path, attempt=None):
        entered.set()
        release.wait(5)
        observed.append(attempt)

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    assert entered.wait(2)
    dispatched = mgr.get(jid)._attempt
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert observed == [dispatched]
    mgr.shutdown(wait=True)


def test_legacy_transcribe_target_cannot_mutate_canonical_job_after_cancel(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    started = threading.Event()
    release = threading.Event()
    killed = []

    class Proc:
        def kill(self):
            killed.append(True)

    def legacy_target(job, *, model_path):
        started.set()
        release.wait(5)
        job.status = TranscribeStatus.DONE
        job.duration_seconds = 99.0
        job.language_detected = "late"
        job.process_handle = Proc()

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=legacy_target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    canonical = mgr.get(jid)
    assert canonical.status is TranscribeStatus.CANCELLED
    assert canonical.duration_seconds == 0.0 and canonical.language_detected == ""
    assert canonical.process_handle is None
    assert killed == [True]
    mgr.shutdown(wait=True)


def test_transcribe_positional_only_attempt_is_not_passed_as_keyword(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    observed = []

    def legacy_target(job, attempt=None, /, *, model_path):
        observed.append((attempt, job is mgr.get(job.id)))

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=legacy_target)
    _wait_tj(mgr, jid)
    assert mgr.get(jid).status is TranscribeStatus.DONE
    assert observed == [(None, False)]
    mgr.shutdown(wait=True)


def test_transcribe_post_commit_entitlement_survives_concurrent_dismiss(tmp_path):
    from attempt_staging import AttemptOutcome

    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    done_persisted = threading.Event()
    release_persist = threading.Event()
    worker_ident = []
    hook_calls = []
    blocked = False
    real_persist = mgr._persist

    def persist_with_done_barrier(*args, **kwargs):
        nonlocal blocked
        result = real_persist(*args, **kwargs)
        if worker_ident and threading.get_ident() == worker_ident[0]:
            with mgr._lock:
                is_done = any(job.status is TranscribeStatus.DONE for job in mgr._jobs.values())
            if is_done and not blocked:
                blocked = True
                done_persisted.set()
                assert release_persist.wait(5)
        return result

    mgr._persist = persist_with_done_barrier

    def target(job, *, model_path, attempt):
        worker_ident.append(threading.get_ident())
        return AttemptOutcome(after_commit=lambda committed: hook_calls.append(committed.id))

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    assert done_persisted.wait(2)
    assert mgr.dismiss(jid) is True
    release_persist.set()
    assert _wait_worker_inactive(mgr, jid)
    assert hook_calls == [jid]
    mgr.shutdown(wait=True)


def test_stale_attempt_transcribe_callback_cannot_mutate_dismissed_job(tmp_path):
    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    started = threading.Event()
    release = threading.Event()
    callbacks = []

    def target(job, **_):
        attempt = job._attempt
        callbacks.append(lambda: mgr.update_progress(job.id, job, attempt, 80))
        started.set()
        release.wait(5)

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    assert mgr.dismiss(jid) is True
    assert callbacks[0]() is False
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    job = mgr.get(jid)
    assert job.status is TranscribeStatus.CANCELLED
    assert job.dismissed_at is not None and job.progress_pct == 0
    mgr.shutdown(wait=True)


def test_attempt_staging_transcribe_success_promotes_all_sidecars(tmp_path):
    from attempt_staging import AttemptOutcome, Promotion

    mgr = TranscribeJobManager(max_workers=1, store_path=tmp_path / "tj.json")
    finals = [tmp_path / f"source{suffix}" for suffix in (".words.json", ".txt", ".srt", ".vtt")]

    def target(job, **_):
        root = Path(job._staging_root)
        promotions = []
        for final in finals:
            staged = root / final.name
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(f"new:{final.suffix}")
            promotions.append(Promotion(staged, final))
        return AttemptOutcome(
            updates={"duration_seconds": 4.0, "language_detected": "en"},
            promotions=tuple(promotions),
        )

    jid = mgr.submit(parent_job_id="source", model_path="m.bin", target=target)
    _wait_tj(mgr, jid)
    assert mgr.get(jid).status is TranscribeStatus.DONE
    assert all(path.exists() for path in finals)
    assert mgr.get(jid).duration_seconds == 4.0
    assert not Path(mgr.get(jid)._staging_root).exists()
    mgr.shutdown(wait=True)


def test_transcribe_attempt_runtime_fields_are_not_persisted(tmp_path):
    store = tmp_path / "tj.json"
    mgr = TranscribeJobManager(max_workers=1, store_path=store)
    gate = threading.Event()
    jid = mgr.submit(parent_job_id="source", model_path="m.bin",
                     target=lambda job, **_: gate.wait(5))
    deadline = time.time() + 2
    while mgr.get(jid).status is not TranscribeStatus.RUNNING and time.time() < deadline:
        time.sleep(0.01)
    payload = json.loads(store.read_text())["jobs"][jid]
    assert "_attempt" not in payload
    assert "_worker_active" not in payload
    assert "_staging_root" not in payload
    gate.set()
    mgr.shutdown(wait=True)


# ---- bounded pending admission --------------------------------------


def _wait_transcribe_pending(manager, expected, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager.pending_count == expected:
            return True
        time.sleep(0.01)
    return False


def _fill_transcribe_capacity(manager, gate):
    return [
        manager.submit(
            parent_job_id=f"parent-{index}",
            model_path="model.bin",
            target=lambda _job, **_kwargs: gate.wait(5),
        )
        for index in range(manager.pending_capacity)
    ]


def test_transcribe_capacity_plus_one_creates_no_record_or_executor_work(
    tmp_path, monkeypatch,
):
    store = tmp_path / "transcribe.json"
    manager = TranscribeJobManager(max_workers=1, store_path=store)
    gate = threading.Event()
    try:
        admitted = _fill_transcribe_capacity(manager, gate)
        assert manager.pending_count == manager.pending_capacity == 4
        before_ids = [job.id for job in manager.snapshot_jobs()]
        executor_calls = []
        real_submit = manager._executor.submit
        monkeypatch.setattr(
            manager._executor,
            "submit",
            lambda *args, **kwargs: executor_calls.append((args, kwargs))
            or real_submit(*args, **kwargs),
        )

        with pytest.raises(QueueFullError):
            manager.submit(
                parent_job_id="overflow",
                model_path="model.bin",
                target=lambda _job, **_kwargs: None,
            )

        assert [job.id for job in manager.snapshot_jobs()] == before_ids == admitted
        assert list(json.loads(store.read_text())["jobs"]) == admitted
        assert manager.pending_count == manager.pending_capacity
        assert executor_calls == []
    finally:
        gate.set()
        manager.shutdown(wait=True)
    assert manager.pending_count == 0


@pytest.mark.parametrize("raises", [False, True])
def test_transcribe_reservation_recovers_after_target_completion(raises):
    manager = TranscribeJobManager(max_workers=1)

    def target(_job, **_kwargs):
        if raises:
            raise RuntimeError("target failed")

    jid = manager.submit(
        parent_job_id="parent", model_path="model.bin", target=target,
    )
    assert _wait_worker_inactive(manager, jid)
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_transcribe_queued_cancel_keeps_reservation_until_stale_wrapper_drains(
    monkeypatch,
):
    manager = TranscribeJobManager(max_workers=1)
    gate = threading.Event()
    cancel_calls = []
    real_submit = manager._executor.submit

    def tracked_submit(*args, **kwargs):
        future = real_submit(*args, **kwargs)
        real_cancel = future.cancel

        def tracked_cancel():
            cancel_calls.append(True)
            return real_cancel()

        monkeypatch.setattr(future, "cancel", tracked_cancel)
        return future

    monkeypatch.setattr(manager._executor, "submit", tracked_submit)
    try:
        admitted = _fill_transcribe_capacity(manager, gate)
        queued = admitted[-1]
        assert manager.get(queued).status is TranscribeStatus.QUEUED
        assert manager.cancel(queued) is True
        assert cancel_calls == []
        assert manager.pending_count == manager.pending_capacity
        with pytest.raises(QueueFullError):
            manager.submit(
                parent_job_id="still-full",
                model_path="model.bin",
                target=lambda _job, **_kwargs: None,
            )
    finally:
        gate.set()
        manager.shutdown(wait=True)

    assert cancel_calls == []
    assert manager.pending_count == 0


class _RejectingTranscribeExecutor:
    def __init__(self):
        self.submit_calls = 0

    def submit(self, *_args, **_kwargs):
        self.submit_calls += 1
        raise RuntimeError("executor rejected")

    def shutdown(self, wait=False, **_kwargs):
        return None


def test_transcribe_submit_rejection_rolls_back_reservation_record_and_store(tmp_path):
    store = tmp_path / "transcribe.json"
    manager = TranscribeJobManager(max_workers=1, store_path=store)
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingTranscribeExecutor()
    manager._executor = rejecting

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.submit(
            parent_job_id="rejected",
            model_path="model.bin",
            target=lambda _job, **_kwargs: None,
        )

    assert manager.pending_count == 0
    assert manager.snapshot_jobs() == []
    assert json.loads(store.read_text()) == {"schema_version": 1, "jobs": {}}
    attempts = tmp_path / ".attempts" / "transcribe"
    assert not attempts.exists() or list(attempts.iterdir()) == []
    assert rejecting.submit_calls == 1
    manager.shutdown(wait=True)


def test_transcribe_cleanup_failure_does_not_leak_reservation(
    tmp_path, monkeypatch,
):
    import transcribe_jobs as module

    manager = TranscribeJobManager(max_workers=1, store_path=tmp_path / "transcribe.json")
    monkeypatch.setattr(
        module,
        "cleanup_attempt",
        lambda _root: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    jid = manager.submit(
        parent_job_id="parent",
        model_path="model.bin",
        target=lambda _job, **_kwargs: None,
    )

    assert _wait_worker_inactive(manager, jid)
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_transcribe_shutdown_wait_false_rejects_new_work_and_drains():
    manager = TranscribeJobManager(max_workers=1)
    gate = threading.Event()
    jid = manager.submit(
        parent_job_id="active",
        model_path="model.bin",
        target=lambda _job, **_kwargs: gate.wait(5),
    )
    deadline = time.time() + 2
    while manager.get(jid).status is not TranscribeStatus.RUNNING and time.time() < deadline:
        time.sleep(0.01)

    manager.shutdown(wait=False)
    assert manager.pending_count == 1
    before_ids = [job.id for job in manager.snapshot_jobs()]
    with pytest.raises(RuntimeError, match="shut down"):
        manager.submit(
            parent_job_id="late",
            model_path="model.bin",
            target=lambda _job, **_kwargs: None,
        )
    assert [job.id for job in manager.snapshot_jobs()] == before_ids

    gate.set()
    assert _wait_transcribe_pending(manager, 0)
    manager.shutdown(wait=True)


def test_transcribe_shutdown_wait_true_returns_with_zero_pending():
    manager = TranscribeJobManager(max_workers=1)
    gate = threading.Event()
    manager.submit(
        parent_job_id="active",
        model_path="model.bin",
        target=lambda _job, **_kwargs: gate.wait(5),
    )
    returned = threading.Event()
    thread = threading.Thread(
        target=lambda: (manager.shutdown(wait=True), returned.set()),
    )
    thread.start()
    try:
        assert not returned.wait(0.05)
    finally:
        gate.set()
        thread.join(5)

    assert returned.is_set()
    assert manager.pending_count == 0


class _BlockingTranscribeSubmitExecutor:
    def __init__(self):
        self.inner = ThreadPoolExecutor(max_workers=1)
        self.submit_entered = threading.Event()
        self.allow_submit = threading.Event()

    def submit(self, fn, *args, **kwargs):
        self.submit_entered.set()
        assert self.allow_submit.wait(5)
        return self.inner.submit(fn, *args, **kwargs)

    def shutdown(self, wait=False, **kwargs):
        return self.inner.shutdown(wait=wait, **kwargs)


def test_transcribe_submit_is_linearized_before_shutdown():
    manager = TranscribeJobManager(max_workers=1)
    manager._executor.shutdown(wait=True)
    blocking = _BlockingTranscribeSubmitExecutor()
    manager._executor = blocking
    submitted = []
    submit_error = []

    def submit_work():
        try:
            submitted.append(manager.submit(
                parent_job_id="racing",
                model_path="model.bin",
                target=lambda _job, **_kwargs: None,
            ))
        except Exception as exc:
            submit_error.append(exc)

    submit_thread = threading.Thread(target=submit_work)
    submit_thread.start()
    assert blocking.submit_entered.wait(2)
    shutdown_returned = threading.Event()
    shutdown_thread = threading.Thread(
        target=lambda: (manager.shutdown(wait=True), shutdown_returned.set()),
    )
    shutdown_thread.start()
    try:
        assert not shutdown_returned.wait(0.05)
    finally:
        blocking.allow_submit.set()
        submit_thread.join(5)
        shutdown_thread.join(5)

    assert submit_error == []
    assert len(submitted) == 1
    assert shutdown_returned.is_set()
    assert manager.pending_count == 0
