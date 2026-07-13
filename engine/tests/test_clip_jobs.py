"""Tests for ClipJob + ClipJobManager — the clip/render job queue.

Mirrors test_transcribe_jobs.py: same lock + ThreadPoolExecutor + atomic-JSON
persistence + restart-downgrade machinery, extended with a ``kind`` and
``params``/``result`` dicts so one manager drives all six clip operations.
"""
import copy
import json
import threading
import time
from pathlib import Path

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

        def submit(self, _fn, child, _attempt, _target):
            self.calls += 1
            if failure_stage == "submission" and self.calls == 2:
                raise RuntimeError("submission failed")

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
