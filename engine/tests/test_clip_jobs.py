"""Tests for ClipJob + ClipJobManager — the clip/render job queue.

Mirrors test_transcribe_jobs.py: same lock + ThreadPoolExecutor + atomic-JSON
persistence + restart-downgrade machinery, extended with a ``kind`` and
``params``/``result`` dicts so one manager drives all six clip operations.
"""
import copy
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from clip_jobs import ClipJob, ClipStatus, ClipJobManager, CLIP_KINDS
from job_capacity import QueueFullError


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
    from attempt_staging import AttemptOutcome

    jm = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")

    def target(j):
        return AttemptOutcome(updates={
            "clip_id": "clip_xyz",
            "result": {"clip_path": "/tmp/clip.mp4", "candidates": [{"start": 1.0}]},
        })

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
    from attempt_staging import AttemptOutcome

    store = tmp_path / "clip.json"
    jm = ClipJobManager(max_workers=1, store_path=store)

    def target(j):
        return AttemptOutcome(updates={"clip_id": "c1", "result": {"render_id": "r1"}})

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


def test_cancel_persist_failure_restores_clip_state_and_preserves_staging(
    tmp_path, monkeypatch,
):
    import clip_jobs as module

    store = tmp_path / "clip.json"
    manager = ClipJobManager(max_workers=1, store_path=store)
    job = ClipJob(
        id="queued-persist-failure",
        kind="cut",
        source_id="source",
        status=ClipStatus.QUEUED,
    )
    job._attempt = 7
    root = tmp_path / ".attempts" / "clip" / job.id
    root.mkdir(parents=True)
    candidate = root / "clip.mp4"
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
        with pytest.raises(RuntimeError, match="persist clip cancellation"):
            manager.cancel(job.id)

    assert (job.status, job._attempt, job._cancel_flag) == before
    assert candidate.read_bytes() == b"candidate"
    assert json.loads(store.read_text())["jobs"][job.id]["status"] == "queued"

    before_retry = ClipJobManager(max_workers=1, store_path=store)
    try:
        assert before_retry.get(job.id).status is ClipStatus.ERROR
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
        ClipStatus.CANCELLED,
        before[1] + 1,
        True,
    )
    assert cleanup_states == [(str(root), "cancelled")]
    assert candidate.read_bytes() == b"candidate"
    manager.shutdown(wait=True)

    after_retry = ClipJobManager(max_workers=1, store_path=store)
    try:
        assert after_retry.get(job.id).status is ClipStatus.CANCELLED
    finally:
        after_retry.shutdown(wait=True)


def test_cancel_active_persist_failure_does_not_kill_clip_process(
    tmp_path, monkeypatch,
):
    import clip_jobs as module

    store = tmp_path / "clip.json"
    manager = ClipJobManager(max_workers=1, store_path=store)
    killed_after_status = []

    class Process:
        def kill(self):
            killed_after_status.append(
                json.loads(store.read_text())["jobs"][job.id]["status"]
            )

    process = Process()
    job = ClipJob(
        id="active-persist-failure",
        kind="cut",
        source_id="source",
        status=ClipStatus.RUNNING,
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
        with pytest.raises(RuntimeError, match="persist clip cancellation"):
            manager.cancel(job.id)

    assert (job.status, job._attempt, job._cancel_flag) == before
    assert manager.get(job.id).process_handle is process
    assert killed_after_status == []

    assert manager.cancel(job.id) is True
    assert killed_after_status == ["cancelled"]
    manager.shutdown(wait=True)

    restarted = ClipJobManager(max_workers=1, store_path=store)
    try:
        assert restarted.get(job.id).status is ClipStatus.CANCELLED
    finally:
        restarted.shutdown(wait=True)


def _await(jm, jid, status, tries=100):
    for _ in range(tries):
        j = jm.get(jid)
        if j is not None and j.status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {jid} never reached {status}; last={jm.get(jid).status if jm.get(jid) else None}")


def _wait_worker_inactive(mgr, jid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not mgr.get(jid)._worker_active:
            return True
        time.sleep(0.01)
    return False


def test_queued_cancel_clip_never_runs_target(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    gate = threading.Event()
    ran = []
    first = mgr.submit(kind="cut", target=lambda job: gate.wait(5))
    cancelled = mgr.submit(kind="cut", target=lambda job: ran.append(job.id))
    assert mgr.get(cancelled).status is ClipStatus.QUEUED
    assert mgr.cancel(cancelled) is True
    gate.set()
    _await(mgr, first, ClipStatus.DONE)
    assert _wait_worker_inactive(mgr, cancelled)
    assert ran == []
    assert mgr.get(cancelled).status is ClipStatus.CANCELLED
    mgr.shutdown(wait=True)


@pytest.mark.parametrize("raises", [False, True])
def test_running_cancel_clip_rejects_result_error_progress_and_publication(tmp_path, raises):
    from attempt_staging import AttemptOutcome, Promotion

    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    published = tmp_path / "clips" / "clip-a" / "clip.mp4"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"old-clip")
    started = threading.Event()
    release = threading.Event()

    def target(job):
        attempt = job._attempt
        staged = Path(job._staging_root) / "clip-a" / "clip.mp4"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"new-clip")
        started.set()
        release.wait(5)
        mgr.update_progress(job.id, job, attempt, 92, stage="late")
        if raises:
            raise RuntimeError("late render failure")
        return AttemptOutcome(
            updates={"clip_id": "clip-a", "result": {"clip_path": str(staged)}},
            promotions=(Promotion(staged, published),),
        )

    jid = mgr.submit(kind="cut", source_id="source", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    job = mgr.get(jid)
    assert job.status is ClipStatus.CANCELLED
    assert job.progress_pct == 0 and job.stage == ""
    assert job.clip_id is None and job.result == {}
    assert job.error_category is None and job.error_message is None
    assert published.read_bytes() == b"old-clip"
    assert not Path(job._staging_root).exists()
    mgr.shutdown(wait=True)


def test_stale_attempt_clip_late_process_registration_is_killed(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    started = threading.Event()
    release = threading.Event()
    killed = []

    class Proc:
        def kill(self):
            killed.append(True)

    def target(job):
        attempt = job._attempt
        started.set()
        release.wait(5)
        assert mgr.register_process(job.id, job, attempt, Proc()) is False

    jid = mgr.submit(kind="cut", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert killed == [True]
    assert mgr.get(jid).process_handle is None
    mgr.shutdown(wait=True)


def test_stale_attempt_clip_target_receives_dispatch_token(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def target(job, *, attempt=None):
        entered.set()
        release.wait(5)
        observed.append(attempt)

    jid = mgr.submit(kind="cut", target=target)
    assert entered.wait(2)
    dispatched = mgr.get(jid)._attempt
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert observed == [dispatched]
    mgr.shutdown(wait=True)


def test_legacy_clip_target_cannot_mutate_canonical_job_after_cancel(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    started = threading.Event()
    release = threading.Event()
    killed = []

    class Proc:
        def kill(self):
            killed.append(True)

    def legacy_target(job):
        started.set()
        release.wait(5)
        job.status = ClipStatus.DONE
        job.clip_id = "late"
        job.result = {"output_path": str(tmp_path / "late.mp4")}
        job.params["nested"]["value"] = "late"
        job.process_handle = Proc()

    jid = mgr.submit(
        kind="cut", params={"nested": {"value": "canonical"}}, target=legacy_target,
    )
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    canonical = mgr.get(jid)
    assert canonical.status is ClipStatus.CANCELLED
    assert canonical.clip_id is None and canonical.result == {}
    assert canonical.params == {"nested": {"value": "canonical"}}
    assert canonical.process_handle is None
    assert killed == [True]
    mgr.shutdown(wait=True)


def test_clip_positional_only_attempt_is_not_passed_as_keyword(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    observed = []

    def legacy_target(job, attempt=None, /):
        observed.append((attempt, job is mgr.get(job.id)))

    jid = mgr.submit(kind="cut", target=legacy_target)
    _await(mgr, jid, ClipStatus.DONE)
    assert observed == [(None, False)]
    mgr.shutdown(wait=True)


def test_clip_post_commit_entitlement_survives_concurrent_dismiss(tmp_path):
    from attempt_staging import AttemptOutcome

    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
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
                is_done = any(job.status is ClipStatus.DONE for job in mgr._jobs.values())
            if is_done and not blocked:
                blocked = True
                done_persisted.set()
                assert release_persist.wait(5)
        return result

    mgr._persist = persist_with_done_barrier

    def target(job, *, attempt):
        worker_ident.append(threading.get_ident())
        return AttemptOutcome(after_commit=lambda committed: hook_calls.append(committed.id))

    jid = mgr.submit(kind="cut", target=target)
    assert done_persisted.wait(2)
    assert mgr.dismiss(jid) is True
    release_persist.set()
    assert _wait_worker_inactive(mgr, jid)
    assert hook_calls == [jid]
    mgr.shutdown(wait=True)


def _seed_running_produce_parent(mgr):
    parent = ClipJob(
        id="produce-parent", kind="produce", status=ClipStatus.RUNNING,
        result={"keep": {"nested": [1, 2]}, "count": 7, "clip_jobs": ["old"]},
    )
    parent._attempt = 3
    parent._worker_active = True
    with mgr._lock:
        mgr._jobs[parent.id] = parent
    return parent


def test_atomic_fanout_prevalidates_all_specs_before_admitting_any_child(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    parent = _seed_running_produce_parent(mgr)
    original_result = copy.deepcopy(parent.result)

    with pytest.raises(ValueError, match="unknown clip kind"):
        mgr.submit_children_if_current(parent, parent._attempt, [
            {"kind": "cut", "target": lambda child: None},
            {"kind": "invalid-later", "target": lambda child: None},
        ])

    assert [job.id for job in mgr.snapshot_jobs()] == [parent.id]
    assert parent.result == original_result
    attempts = tmp_path / ".attempts" / "clip"
    assert not attempts.exists() or list(attempts.iterdir()) == []
    mgr.shutdown(wait=True)


@pytest.mark.parametrize("failure_stage", ["creation", "submission"])
def test_atomic_fanout_rolls_back_every_child_and_exact_parent_result(
    tmp_path, monkeypatch, failure_stage,
):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    parent = _seed_running_produce_parent(mgr)
    original_result = parent.result
    created = []

    real_new_job = mgr._new_job_locked

    def create_then_fail(**kwargs):
        child = real_new_job(**kwargs)
        created.append(child[0])
        if failure_stage == "creation" and len(created) == 2:
            raise RuntimeError("creation failed")
        return child

    monkeypatch.setattr(mgr, "_new_job_locked", create_then_fail)

    class FailSecondSubmission:
        def __init__(self):
            self.calls = 0

        def submit(self, _fn, child, _attempt, _target, _reservation):
            self.calls += 1
            if failure_stage == "submission" and self.calls == 2:
                raise RuntimeError("submission failed")
            return Future()

        def shutdown(self, wait=True):
            return None

    mgr._executor.shutdown(wait=True)
    mgr._executor = FailSecondSubmission()

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        mgr.submit_children_if_current(parent, parent._attempt, [
            {"kind": "cut", "clip_id": "a", "target": lambda child: None},
            {"kind": "cut", "clip_id": "b", "target": lambda child: None},
        ])

    assert [job.id for job in mgr.snapshot_jobs()] == [parent.id]
    assert parent.result is original_result
    assert parent.result == {
        "keep": {"nested": [1, 2]}, "count": 7, "clip_jobs": ["old"],
    }
    assert created and all(child._worker_active is False for child in created)
    assert all(not Path(child._staging_root).exists() for child in created)
    mgr.shutdown(wait=True)


def test_cancel_produce_before_atomic_fanout_submits_zero_children(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    started = threading.Event()
    release = threading.Event()
    observed = []

    def target(parent, *, attempt):
        started.set()
        release.wait(5)
        observed.extend(mgr.submit_children_if_current(parent, attempt, [
            {"kind": "cut", "source_id": "source", "clip_id": "clip-a",
             "params": {}, "target": lambda child: None},
            {"kind": "cut", "source_id": "source", "clip_id": "clip-b",
             "params": {}, "target": lambda child: None},
        ]))

    jid = mgr.submit(kind="produce", source_id="source", target=target)
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert observed == []
    assert len(mgr.snapshot_jobs()) == 1
    mgr.shutdown(wait=True)


def test_cancel_produce_after_atomic_fanout_keeps_all_admitted_children(tmp_path):
    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    admitted = threading.Event()
    release = threading.Event()
    child_gate = threading.Event()
    observed = []

    def target(parent, *, attempt):
        observed.extend(mgr.submit_children_if_current(parent, attempt, [
            {"kind": "cut", "source_id": "source", "clip_id": "clip-a",
             "params": {}, "target": lambda child: child_gate.wait(5)},
            {"kind": "cut", "source_id": "source", "clip_id": "clip-b",
             "params": {}, "target": lambda child: child_gate.wait(5)},
        ]))
        admitted.set()
        release.wait(5)

    jid = mgr.submit(kind="produce", source_id="source", target=target)
    assert admitted.wait(2)
    assert mgr.cancel(jid) is True
    assert len(observed) == 2
    assert mgr.get(jid).result["clip_jobs"] == observed
    release.set()
    child_gate.set()
    assert _wait_worker_inactive(mgr, jid)
    assert all(mgr.get(child) is not None for child in observed)
    mgr.shutdown(wait=True)


def test_attempt_staging_clip_success_promotes_and_rewrites_result(tmp_path):
    from attempt_staging import AttemptOutcome, Promotion

    mgr = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    final = tmp_path / "clips" / "clip-a" / "renders" / "r.mp4"

    def target(job):
        staged = Path(job._staging_root) / "clip-a" / "renders" / "r.mp4"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"render")
        return AttemptOutcome(
            updates={"clip_id": "clip-a", "result": {"output_path": str(staged)}},
            promotions=(Promotion(staged, final),),
        )

    jid = mgr.submit(kind="export", source_id="source", target=target)
    _await(mgr, jid, ClipStatus.DONE)
    job = mgr.get(jid)
    assert final.read_bytes() == b"render"
    assert job.result["output_path"] == str(final)
    assert not Path(job._staging_root).exists()
    mgr.shutdown(wait=True)


def test_clip_attempt_runtime_fields_are_not_persisted(tmp_path):
    store = tmp_path / "clip.json"
    mgr = ClipJobManager(max_workers=1, store_path=store)
    gate = threading.Event()
    jid = mgr.submit(kind="cut", target=lambda job: gate.wait(5))
    _await(mgr, jid, ClipStatus.RUNNING)
    payload = json.loads(store.read_text())["jobs"][jid]
    assert "_attempt" not in payload
    assert "_worker_active" not in payload
    assert "_staging_root" not in payload
    gate.set()
    mgr.shutdown(wait=True)


# ---- bounded pending admission --------------------------------------


def _wait_clip_pending(manager, expected, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager.pending_count == expected:
            return True
        time.sleep(0.01)
    return False


def _fill_clip_capacity(manager, gate):
    return [
        manager.submit(
            kind="cut",
            source_id=f"source-{index}",
            target=lambda _job: gate.wait(5),
        )
        for index in range(manager.pending_capacity)
    ]


def test_clip_capacity_plus_one_creates_no_record_or_executor_work(monkeypatch):
    manager = ClipJobManager(max_workers=1)
    gate = threading.Event()
    try:
        admitted = _fill_clip_capacity(manager, gate)
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
            manager.submit(kind="cut", target=lambda _job: None)

        assert [job.id for job in manager.snapshot_jobs()] == before_ids == admitted
        assert manager.pending_count == manager.pending_capacity
        assert executor_calls == []
    finally:
        gate.set()
        manager.shutdown(wait=True)
    assert manager.pending_count == 0


@pytest.mark.parametrize("raises", [False, True])
def test_clip_reservation_recovers_after_target_completion(raises):
    manager = ClipJobManager(max_workers=1)

    def target(_job):
        if raises:
            raise RuntimeError("target failed")

    jid = manager.submit(kind="cut", target=target)
    assert _wait_worker_inactive(manager, jid)
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_clip_queued_cancel_keeps_reservation_until_stale_wrapper_drains(monkeypatch):
    manager = ClipJobManager(max_workers=1)
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
        admitted = _fill_clip_capacity(manager, gate)
        queued = admitted[-1]
        assert manager.get(queued).status is ClipStatus.QUEUED
        assert manager.cancel(queued) is True
        assert cancel_calls == []
        assert manager.pending_count == manager.pending_capacity
        with pytest.raises(QueueFullError):
            manager.submit(kind="cut", target=lambda _job: None)
    finally:
        gate.set()
        manager.shutdown(wait=True)

    assert cancel_calls == []
    assert manager.pending_count == 0


class _RejectingClipExecutor:
    def __init__(self):
        self.submit_calls = 0

    def submit(self, *_args, **_kwargs):
        self.submit_calls += 1
        raise RuntimeError("executor rejected")

    def shutdown(self, wait=False, **_kwargs):
        return None


def test_clip_submit_rejection_rolls_back_reservation_record_and_store(tmp_path):
    store = tmp_path / "clip.json"
    manager = ClipJobManager(max_workers=1, store_path=store)
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingClipExecutor()
    manager._executor = rejecting

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.submit(kind="cut", target=lambda _job: None)

    assert manager.pending_count == 0
    assert manager.snapshot_jobs() == []
    assert json.loads(store.read_text()) == {"schema_version": 1, "jobs": {}}
    assert rejecting.submit_calls == 1
    manager.shutdown(wait=True)


def test_clip_cleanup_failure_does_not_leak_reservation(tmp_path, monkeypatch):
    import clip_jobs as module

    manager = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    monkeypatch.setattr(
        module,
        "cleanup_attempt",
        lambda _root: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    jid = manager.submit(kind="cut", target=lambda _job: None)

    assert _wait_worker_inactive(manager, jid)
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_clip_shutdown_wait_false_rejects_new_work_and_drains():
    manager = ClipJobManager(max_workers=1)
    gate = threading.Event()
    jid = manager.submit(kind="cut", target=lambda _job: gate.wait(5))
    _await(manager, jid, ClipStatus.RUNNING)

    manager.shutdown(wait=False)
    assert manager.pending_count == 1
    before_ids = [job.id for job in manager.snapshot_jobs()]
    with pytest.raises(RuntimeError, match="shut down"):
        manager.submit(kind="cut", target=lambda _job: None)
    assert [job.id for job in manager.snapshot_jobs()] == before_ids

    gate.set()
    assert _wait_clip_pending(manager, 0)
    manager.shutdown(wait=True)


def test_clip_shutdown_wait_true_returns_with_zero_pending():
    manager = ClipJobManager(max_workers=1)
    gate = threading.Event()
    manager.submit(kind="cut", target=lambda _job: gate.wait(5))
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


class _BlockingClipSubmitExecutor:
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


def test_clip_submit_is_linearized_before_shutdown():
    manager = ClipJobManager(max_workers=1)
    manager._executor.shutdown(wait=True)
    blocking = _BlockingClipSubmitExecutor()
    manager._executor = blocking
    submitted = []
    submit_error = []

    def submit_work():
        try:
            submitted.append(manager.submit(kind="cut", target=lambda _job: None))
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


def test_atomic_fanout_capacity_overflow_marks_parent_error_without_children(
    tmp_path, monkeypatch,
):
    manager = ClipJobManager(max_workers=1, store_path=tmp_path / "clip.json")
    fanout_ready = threading.Event()
    allow_fanout = threading.Event()
    filler_gate = threading.Event()
    observed = []

    def parent_target(parent, *, attempt):
        fanout_ready.set()
        assert allow_fanout.wait(5)
        observed.extend(manager.submit_children_if_current(parent, attempt, [
            {"kind": "cut", "clip_id": "a", "target": lambda child: None},
            {"kind": "cut", "clip_id": "b", "target": lambda child: None},
        ]))

    parent_id = manager.submit(kind="produce", target=parent_target)
    assert fanout_ready.wait(2)
    fillers = [
        manager.submit(kind="cut", target=lambda _job: filler_gate.wait(5))
        for _ in range(manager.pending_capacity - 1)
    ]
    assert manager.pending_count == manager.pending_capacity == 4
    new_job_calls = []
    real_new_job = manager._new_job_locked
    monkeypatch.setattr(
        manager,
        "_new_job_locked",
        lambda **kwargs: new_job_calls.append(kwargs) or real_new_job(**kwargs),
    )

    allow_fanout.set()
    _await(manager, parent_id, ClipStatus.ERROR)
    parent = manager.get(parent_id)
    assert observed == []
    assert new_job_calls == []
    assert parent.error_category == "queue_full"
    assert parent.error_message == "media queue full"
    assert parent.result == {
        "error": "queue_full",
        "requested": 2,
        "clip_jobs": [],
    }
    assert [job.id for job in manager.snapshot_jobs()] == [parent_id, *fillers]

    filler_gate.set()
    manager.shutdown(wait=True)
    assert manager.pending_count == 0


def test_atomic_fanout_children_observe_complete_parent_child_list():
    manager = ClipJobManager(max_workers=3)
    observed = []
    child_ids = []

    def parent_target(parent, *, attempt):
        child_ids.extend(manager.submit_children_if_current(parent, attempt, [
            {
                "kind": "cut",
                "clip_id": "a",
                "target": lambda _child: observed.append(list(parent.result["clip_jobs"])),
            },
            {
                "kind": "cut",
                "clip_id": "b",
                "target": lambda _child: observed.append(list(parent.result["clip_jobs"])),
            },
        ]))

    parent_id = manager.submit(kind="produce", target=parent_target)
    _await(manager, parent_id, ClipStatus.DONE)
    assert len(child_ids) == 2
    for child_id in child_ids:
        _await(manager, child_id, ClipStatus.DONE)

    assert observed == [child_ids, child_ids]
    manager.shutdown(wait=True)
    assert manager.pending_count == 0


class _DeterministicUUID:
    def __init__(self, hex_value):
        self.hex = hex_value


_COLLIDING_UUID_HEX = "deadbeef00" + ("1" * 22)
_FIRST_CHILD_UUID_HEX = "cafebabe01" + ("2" * 22)
_SECOND_CHILD_UUID_HEX = "feedface02" + ("3" * 22)


def _force_clip_job_uuid_sequence(monkeypatch, values):
    generated = iter(values)
    monkeypatch.setattr(
        "clip_jobs.uuid.uuid4",
        lambda: _DeterministicUUID(next(generated)),
    )


def test_atomic_fanout_retries_job_id_collisions_without_overwriting_incumbent(
    monkeypatch,
):
    manager = ClipJobManager(max_workers=2)
    parent = _seed_running_produce_parent(manager)
    incumbent = ClipJob(
        id="deadbeef00",
        kind="export",
        status=ClipStatus.DONE,
        result={"incumbent": True},
    )
    with manager._lock:
        manager._jobs[incumbent.id] = incumbent
    _force_clip_job_uuid_sequence(
        monkeypatch,
        [
            _COLLIDING_UUID_HEX,
            _FIRST_CHILD_UUID_HEX,
            _FIRST_CHILD_UUID_HEX,
            _SECOND_CHILD_UUID_HEX,
        ],
    )
    ran = []

    try:
        child_ids = manager.submit_children_if_current(parent, parent._attempt, [
            {"kind": "cut", "target": lambda _child: ran.append("first")},
            {"kind": "cut", "target": lambda _child: ran.append("second")},
        ])

        assert child_ids == ["cafebabe01", "feedface02"]
        assert _wait_clip_pending(manager, 0)
        assert sorted(ran) == ["first", "second"]
        assert manager.get(incumbent.id) is incumbent
        assert [manager.get(child_id).id for child_id in child_ids] == child_ids
        assert parent.result["clip_jobs"] == child_ids
    finally:
        manager.shutdown(wait=True)


class _MixedFanoutFuture:
    def __init__(self, cancel_result):
        self.cancel_result = cancel_result
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        return self.cancel_result


class _MixedFanoutExecutor:
    def __init__(self):
        self.calls = 0
        self.futures = []
        self.threads = []

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("third submit failed")
        future = _MixedFanoutFuture(cancel_result=self.calls == 1)
        self.futures.append(future)
        if self.calls == 2:
            thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
            self.threads.append(thread)
            thread.start()
        return future

    def shutdown(self, wait=False, **_kwargs):
        if wait:
            for thread in self.threads:
                thread.join(5)


class _FailSecondFanoutExecutor:
    def __init__(self):
        self.calls = 0

    def submit(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second submit failed")
        return Future()

    def shutdown(self, wait=False, **_kwargs):
        return None


def test_atomic_fanout_collision_mid_submit_restores_incumbent_without_hidden_child(
    tmp_path, monkeypatch,
):
    store = tmp_path / "clip.json"
    manager = ClipJobManager(max_workers=1, store_path=store)
    parent = _seed_running_produce_parent(manager)
    original_result = parent.result
    incumbent = ClipJob(
        id="deadbeef00",
        kind="export",
        status=ClipStatus.DONE,
        result={"incumbent": True},
    )
    with manager._lock:
        manager._jobs[incumbent.id] = incumbent
    manager._persist()
    manager._executor.shutdown(wait=True)
    manager._executor = _FailSecondFanoutExecutor()
    _force_clip_job_uuid_sequence(
        monkeypatch,
        [
            _COLLIDING_UUID_HEX,
            _FIRST_CHILD_UUID_HEX,
            _FIRST_CHILD_UUID_HEX,
            _SECOND_CHILD_UUID_HEX,
        ],
    )

    with pytest.raises(RuntimeError, match="second submit failed"):
        manager.submit_children_if_current(parent, parent._attempt, [
            {"kind": "cut", "target": lambda _child: None},
            {"kind": "cut", "target": lambda _child: None},
        ])

    assert manager.pending_count == 0
    assert manager.get(incumbent.id) is incumbent
    assert [job.id for job in manager.snapshot_jobs()] == [parent.id, incumbent.id]
    assert parent.result is original_result
    stored = json.loads(store.read_text())["jobs"]
    assert set(stored) == {parent.id, incumbent.id}
    assert stored[incumbent.id]["result"] == {"incumbent": True}
    attempts = tmp_path / ".attempts" / "clip"
    assert not attempts.exists() or list(attempts.iterdir()) == []
    manager.shutdown(wait=True)


def test_atomic_fanout_midbatch_failure_releases_each_reservation_exactly_once(tmp_path):
    store = tmp_path / "clip.json"
    manager = ClipJobManager(max_workers=1, store_path=store)
    parent = _seed_running_produce_parent(manager)
    original_result = parent.result
    manager._persist()
    manager._executor.shutdown(wait=True)
    mixed = _MixedFanoutExecutor()
    manager._executor = mixed

    with pytest.raises(RuntimeError, match="third submit failed"):
        manager.submit_children_if_current(parent, parent._attempt, [
            {"kind": "cut", "clip_id": "a", "target": lambda child: None},
            {"kind": "cut", "clip_id": "b", "target": lambda child: None},
            {"kind": "cut", "clip_id": "c", "target": lambda child: None},
        ])

    assert _wait_clip_pending(manager, 0)
    assert [future.cancel_calls for future in mixed.futures] == [1, 1]
    assert [job.id for job in manager.snapshot_jobs()] == [parent.id]
    assert parent.result is original_result
    assert set(json.loads(store.read_text())["jobs"]) == {parent.id}
    attempts = tmp_path / ".attempts" / "clip"
    assert not attempts.exists() or list(attempts.iterdir()) == []
    manager.shutdown(wait=True)
