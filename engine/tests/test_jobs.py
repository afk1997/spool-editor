import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from job_capacity import QueueFullError
from jobs import JobManager, Job, JobStatus


def test_submit_returns_job_id_and_marks_queued():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: None, title="hi", url="https://x")
    assert isinstance(jid, str) and len(jid) == 10
    j = jm.get(jid)
    assert j.title == "hi"
    assert j.status in {JobStatus.QUEUED, JobStatus.DOWNLOADING}
    jm.shutdown()


def test_submit_runs_target_and_marks_done(tmp_path):
    jm = JobManager(max_workers=1, ttl_seconds=60)
    flag = tmp_path / "done.txt"

    def work(job: Job):
        flag.write_text("ok")

    jid = jm.submit(target=work, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    assert flag.read_text() == "ok"
    assert jm.get(jid).status == JobStatus.DONE
    jm.shutdown()


def test_submit_marks_error_when_target_raises():
    jm = JobManager(max_workers=1, ttl_seconds=60)

    def boom(job: Job):
        raise RuntimeError("nope")

    jid = jm.submit(target=boom, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.ERROR:
            break
        time.sleep(0.05)
    assert jm.get(jid).status == JobStatus.ERROR
    jm.shutdown()


@pytest.mark.parametrize(
    "status", [JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED],
)
def test_cancel_terminal_is_noop_and_preserves_published_file(status, tmp_path):
    jm = JobManager(max_workers=1, ttl_seconds=60)

    published = tmp_path / "published.mp4"
    published.write_bytes(b"published-download")
    job = Job(
        id=f"terminal-{status.value}", url="https://x", title="t",
        status=status, file_path=str(published), filename=published.name,
    )
    with jm._lock:
        jm._jobs[job.id] = job

    assert jm.cancel(job.id) is False
    assert jm.get(job.id).status is status
    assert published.read_bytes() == b"published-download"
    jm.shutdown()


@pytest.mark.parametrize(
    ("workers", "expected"),
    [(1, 4), (2, 8), (4, 16), (8, 32), (99, 32)],
)
def test_pending_capacity_formula(workers, expected):
    try:
        from job_capacity import QueueFullError, pending_capacity
    except ImportError as exc:
        pytest.fail(f"bounded-admission contract is missing: {exc}")

    assert issubclass(QueueFullError, RuntimeError)
    assert pending_capacity(workers) == expected


def test_ttl_sweep_marks_old_done_jobs_and_preserves_file(tmp_path):
    from attempt_staging import AttemptOutcome, Promotion

    jm = JobManager(max_workers=1, ttl_seconds=0)  # zero = sweep immediately

    def work(job: Job):
        f = tmp_path / "out.bin"
        staged = Path(job.out_template.replace("%(ext)s", "bin"))
        staged.write_bytes(b"x")
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": f.name},
            promotions=(Promotion(staged, f),),
        )

    jid = jm.submit(target=work, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    assert jm.sweep() == 1
    swept = jm.get(jid)
    assert swept is not None
    assert swept.dismissed_at is not None
    assert (tmp_path / "out.bin").read_bytes() == b"x"
    assert jm.sweep() == 0
    jm.shutdown()


def test_ttl_sweep_marks_each_expired_terminal_job_once(tmp_path):
    from attempt_staging import AttemptOutcome, Promotion

    jm = JobManager(max_workers=1, ttl_seconds=0)

    def work(job: Job):
        f = tmp_path / f"{job.id}.bin"
        staged = Path(job.out_template.replace("%(ext)s", "bin"))
        staged.write_bytes(b"x")
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": f.name},
            promotions=(Promotion(staged, f),),
        )

    jid_keep = jm.submit(target=work, title="keep", url="https://k")
    jid_drop = jm.submit(target=work, title="drop", url="https://d")
    for _ in range(50):
        if (jm.get(jid_keep).status == JobStatus.DONE
                and jm.get(jid_drop).status == JobStatus.DONE):
            break
        time.sleep(0.05)

    assert jm.sweep() == 2
    assert jm.get(jid_keep).dismissed_at is not None
    assert jm.get(jid_drop).dismissed_at is not None
    assert (tmp_path / f"{jid_keep}.bin").read_bytes() == b"x"
    assert (tmp_path / f"{jid_drop}.bin").read_bytes() == b"x"
    assert jm.sweep() == 0
    jm.shutdown()


def test_ttl_sweep_does_not_mark_active_job(tmp_path):
    jm = JobManager(max_workers=1, ttl_seconds=0)
    job = Job(id="active", url="https://x", title="active", status=JobStatus.PAUSED)
    with jm._lock:
        jm._jobs[job.id] = job
    assert jm.sweep() == 0
    assert jm.get(job.id).dismissed_at is None
    jm.shutdown()


def test_job_status_includes_paused():
    assert JobStatus.PAUSED.value == "paused"


def test_job_dataclass_has_resume_fields():
    j = Job(id="x", url="https://e.com", title="t")
    assert hasattr(j, "format_choice")
    assert hasattr(j, "format_id")
    assert hasattr(j, "out_template")
    assert j.format_choice == "video"
    assert j.format_id is None
    assert j.out_template == ""
    assert j._was_paused is False
    assert j.dismissed_at is None


def test_jobmanager_persists_on_state_change(tmp_path):
    from jobs_store import load_jobs
    store_path = tmp_path / "jobs.json"
    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=store_path)

    jid = jm.submit(target=lambda j: None, title="hi", url="https://x")
    # Wait for the worker to finish
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)

    # The store file should exist and contain the job
    assert store_path.exists()
    loaded = load_jobs(store_path)
    assert jid in loaded
    assert loaded[jid].status == JobStatus.DONE
    jm.shutdown()


def test_jobmanager_load_downgrades_downloading_to_paused(tmp_path):
    """After a crash/restart, jobs left in DOWNLOADING are reset to PAUSED."""
    from jobs_store import persist_atomic
    store_path = tmp_path / "jobs.json"
    job = Job(
        id="abc", url="https://e.com/v", title="t",
        status=JobStatus.DOWNLOADING,
        out_template=str(tmp_path / "abc.%(ext)s"),
    )
    persist_atomic({"abc": job}, store_path)

    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=store_path)
    j = jm.get("abc")
    assert j is not None
    assert j.status == JobStatus.PAUSED  # downgraded from DOWNLOADING
    jm.shutdown()


def test_jobmanager_load_downgrades_queued_to_paused(tmp_path):
    """QUEUED jobs at startup also become PAUSED — their work thunk is gone."""
    from jobs_store import persist_atomic
    store_path = tmp_path / "jobs.json"
    job = Job(
        id="abc", url="https://e.com/v", title="t",
        status=JobStatus.QUEUED,
        out_template=str(tmp_path / "abc.%(ext)s"),
    )
    persist_atomic({"abc": job}, store_path)

    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=store_path)
    assert jm.get("abc").status == JobStatus.PAUSED
    jm.shutdown()


def test_jobmanager_load_keeps_cancelled(tmp_path):
    from jobs_store import persist_atomic
    store_path = tmp_path / "jobs.json"
    persist_atomic(
        {"x": Job(id="x", url="https://e.com", title="t", status=JobStatus.CANCELLED)},
        store_path,
    )
    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=store_path)
    assert jm.get("x").status == JobStatus.CANCELLED
    jm.shutdown()


def test_jobmanager_load_keeps_done_and_error(tmp_path):
    from jobs_store import persist_atomic
    store_path = tmp_path / "jobs.json"
    persist_atomic(
        {
            "d": Job(id="d", url="https://e.com/1", title="d", status=JobStatus.DONE),
            "e": Job(id="e", url="https://e.com/2", title="e", status=JobStatus.ERROR),
        },
        store_path,
    )
    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=store_path)
    assert jm.get("d").status == JobStatus.DONE
    assert jm.get("e").status == JobStatus.ERROR
    jm.shutdown()


def test_pause_marks_paused_and_kills_process():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: time.sleep(2), title="t", url="https://x")
    # Inject a fake process so we can verify it gets killed
    fake = type("P", (), {"killed": False, "kill": lambda self: setattr(self, "killed", True)})()
    jm.get(jid).process = fake

    assert jm.pause(jid) is True
    assert jm.get(jid).status == JobStatus.PAUSED
    assert jm.get(jid)._was_paused is True
    assert fake.killed is True
    jm.shutdown()


def test_pause_idempotent_on_already_paused():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: time.sleep(2), title="t", url="https://x")
    jm.pause(jid)
    # Second call returns True and stays PAUSED, doesn't crash
    assert jm.pause(jid) is True
    assert jm.get(jid).status == JobStatus.PAUSED
    jm.shutdown()


def test_pause_returns_false_for_unknown_id():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    assert jm.pause("nonexistent") is False
    jm.shutdown()


def test_pause_noop_on_terminal_states():
    """Pausing a DONE/ERROR/CANCELLED job is a no-op (returns False)."""
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: None, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    assert jm.pause(jid) is False
    assert jm.get(jid).status == JobStatus.DONE
    jm.shutdown()


def test_resume_re_runs_target_and_clears_paused_flag():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    runs = []

    def work(job: Job):
        runs.append(job.id)

    jid = jm.submit(target=work, title="t", url="https://x")
    # Wait for first run
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    # Force into PAUSED for the test
    with jm._lock:
        jm._jobs[jid].status = JobStatus.PAUSED
        jm._jobs[jid]._was_paused = True

    assert jm.resume(jid, target=work) is True
    # Wait for second run
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    assert len(runs) == 2
    assert jm.get(jid)._was_paused is False
    jm.shutdown()


def test_resume_returns_false_for_unknown_id():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    assert jm.resume("nope", target=lambda j: None) is False
    jm.shutdown()


def test_resume_no_op_on_already_downloading():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: time.sleep(2), title="t", url="https://x")
    # While DOWNLOADING, resume should return True and not double-submit
    runs = []
    # Give the job time to reach DOWNLOADING
    time.sleep(0.2)
    assert jm.resume(jid, target=lambda j: runs.append(1)) is True
    time.sleep(0.1)
    assert len(runs) == 0  # the resume target should not have run
    jm.shutdown()


def test_cancel_from_paused_removes_partial_files_but_preserves_published_file(tmp_path):
    """Cancel on a non-terminal job (e.g. PAUSED) must remove .part and other
    output-template artifacts left behind by the killed yt-dlp process.
    """
    jm = JobManager(max_workers=1, ttl_seconds=60)
    # Two artifacts a paused yt-dlp could leave behind
    part_file = tmp_path / "abc.mp4.part"
    part_file.write_bytes(b"partial")
    webm_file = tmp_path / "abc.webm"
    webm_file.write_bytes(b"alt")
    published_file = tmp_path / "abc.mp4"
    published_file.write_bytes(b"published")
    transcript_artifacts = {
        tmp_path / "abc.words.json": b"words",
        tmp_path / "abc.srt": b"srt",
        tmp_path / "abc.vtt": b"vtt",
        tmp_path / "abc.txt": b"txt",
    }
    for path, contents in transcript_artifacts.items():
        path.write_bytes(contents)
    out_template = str(tmp_path / "abc.%(ext)s")

    # Insert a paused job directly rather than submitting a live worker: a real
    # target() races the worker (which flips status to DOWNLOADING) against this
    # PAUSED setup, making the test order/timing-dependent. cancel()'s artifact
    # cleanup needs only the job's recorded out_template — no running worker.
    job = Job(id="pausedjob", url="https://x", title="t", status=JobStatus.PAUSED)
    job._was_paused = True
    job.out_template = out_template
    job.file_path = str(published_file)
    with jm._lock:
        jm._jobs[job.id] = job

    assert jm.cancel(job.id) is True
    assert jm.get(job.id).status == JobStatus.CANCELLED
    assert not part_file.exists(), "cancel should delete .part files"
    assert not webm_file.exists(), "cancel should delete the alt-format leftover"
    assert published_file.read_bytes() == b"published"
    for path, contents in transcript_artifacts.items():
        assert path.read_bytes() == contents
    jm.shutdown()


def test_cancel_paused_persists_before_best_effort_cleanup(tmp_path, monkeypatch):
    """A cleanup failure cannot resurrect a durably cancelled paused job."""
    import jobs

    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    paused = Job(
        id="paused-cleanup-failure",
        url="https://x",
        title="paused",
        status=JobStatus.PAUSED,
    )
    paused._was_paused = True
    with manager._lock:
        manager._ensure_download_staging_locked(paused)
        manager._jobs[paused.id] = paused
    partial = Path(paused.out_template.replace("%(ext)s", "mp4.part"))
    partial.write_bytes(b"resume-bytes")
    assert manager._persist() is True

    cleanup_calls = []

    def fail_cleanup(root):
        cleanup_calls.append(root)
        raise OSError("forced cleanup failure")

    monkeypatch.setattr(jobs, "cleanup_attempt", fail_cleanup)

    assert manager.cancel(paused.id) is True
    assert manager.get(paused.id).status is JobStatus.CANCELLED
    assert cleanup_calls == [paused._staging_root]
    assert partial.read_bytes() == b"resume-bytes"
    manager.shutdown(wait=True)

    restarted = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    try:
        assert restarted.get(paused.id).status is JobStatus.CANCELLED
    finally:
        restarted.shutdown(wait=True)


def test_cancel_paused_persist_failure_raises_without_cleanup(tmp_path, monkeypatch):
    """An undurable cancellation rolls back and preserves a retryable pause."""
    import jobs
    import jobs_store

    store = tmp_path / "jobs.json"
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    manager = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    paused = Job(
        id="paused-persist-failure",
        url="https://x",
        title="paused",
        status=JobStatus.PAUSED,
        out_template=str(legacy_root / "paused.%(ext)s"),
    )
    paused._attempt = 7
    paused._was_paused = True
    with manager._lock:
        manager._jobs[paused.id] = paused
    partial = Path(paused.out_template.replace("%(ext)s", "mp4.part"))
    partial.write_bytes(b"resume-bytes")
    assert manager._persist() is True
    before = {
        name: getattr(paused, name)
        for name in (
            "status",
            "_attempt",
            "_was_paused",
            "_staging_root",
            "out_template",
        )
    }

    cleanup_calls = []

    with monkeypatch.context() as patch:
        patch.setattr(
            jobs_store,
            "persist_atomic",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced persist failure")
            ),
        )
        patch.setattr(jobs, "cleanup_attempt", cleanup_calls.append)

        with pytest.raises(RuntimeError, match="persist download cancellation"):
            manager.cancel(paused.id)

    assert cleanup_calls == []
    assert {
        name: getattr(manager.get(paused.id), name)
        for name in before
    } == before
    assert partial.read_bytes() == b"resume-bytes"

    before_retry = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    try:
        assert before_retry.get(paused.id).status is JobStatus.PAUSED
    finally:
        before_retry.shutdown(wait=True)

    assert manager.cancel(paused.id) is True
    assert manager.get(paused.id).status is JobStatus.CANCELLED
    assert not partial.exists()
    manager.shutdown(wait=True)

    after_retry = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    try:
        assert after_retry.get(paused.id).status is JobStatus.CANCELLED
    finally:
        after_retry.shutdown(wait=True)


def test_cancel_active_persist_failure_restores_killable_state_and_retries(
    tmp_path, monkeypatch,
):
    import jobs_store

    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    killed = []

    class Process:
        def kill(self):
            killed.append(True)

    process = Process()
    active = Job(
        id="active-persist-failure",
        url="https://x",
        title="active",
        status=JobStatus.DOWNLOADING,
        process=process,
    )
    active._attempt = 11
    active._worker_active = True
    with manager._lock:
        manager._ensure_download_staging_locked(active)
        manager._jobs[active.id] = active
    assert manager._persist() is True
    before = {
        name: getattr(active, name)
        for name in ("status", "_attempt", "_was_paused", "process")
    }

    with monkeypatch.context() as patch:
        patch.setattr(
            jobs_store,
            "persist_atomic",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced persist failure")
            ),
        )

        with pytest.raises(RuntimeError, match="persist download cancellation"):
            manager.cancel(active.id)

    assert {
        name: getattr(manager.get(active.id), name)
        for name in before
    } == before
    assert killed == []

    assert manager.cancel(active.id) is True
    assert manager.get(active.id).status is JobStatus.CANCELLED
    assert killed == [True]
    manager.shutdown(wait=True)

    restarted = JobManager(max_workers=1, ttl_seconds=60, store_path=store)
    try:
        assert restarted.get(active.id).status is JobStatus.CANCELLED
    finally:
        restarted.shutdown(wait=True)


def test_stale_attempt_success_after_pause_does_not_publish(tmp_path):
    """Pause linearizes before publication, so the captured attempt stays stale.

    A completed yt-dlp result is still private until the manager revalidates and
    promotes it.  Pausing in that window must preserve staging for resume rather
    than resurrecting the old attempt as DONE.
    """
    from attempt_staging import AttemptOutcome, Promotion

    jm = JobManager(max_workers=1, ttl_seconds=60, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    final = tmp_path / "published.mp4"

    def work(job: Job):
        staged = Path(job.out_template.replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"candidate")
        started.set()
        release.wait(5)
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": "published.mp4"},
            promotions=(Promotion(staged, final),),
        )

    jid = jm.submit(target=work, title="t", url="https://x")
    assert started.wait(2)
    assert jm.pause(jid) is True
    release.set()
    assert _wait_worker_inactive(jm, jid)

    j = jm.get(jid)
    assert j.status is JobStatus.PAUSED
    assert j.file_path is None
    assert not final.exists()
    assert Path(j.out_template.replace("%(ext)s", "mp4")).read_bytes() == b"candidate"
    jm.shutdown()


def test_snapshot_jobs_returns_insertion_ordered_list():
    jm = JobManager(max_workers=2, ttl_seconds=60)
    j1 = jm.submit(target=lambda j: None, title="a", url="https://1")
    j2 = jm.submit(target=lambda j: None, title="b", url="https://2")
    snap = jm.snapshot_jobs()
    assert [j.id for j in snap] == [j1, j2]
    jm.shutdown()


def test_dismiss_marks_done_job_idempotently_and_preserves_file(tmp_path, monkeypatch):
    from attempt_staging import AttemptOutcome, Promotion

    import jobs
    monkeypatch.setattr(jobs, "_utc_now_rfc3339", lambda: "2026-07-13T12:34:56.789Z", raising=False)
    jm = JobManager(max_workers=1, ttl_seconds=60)
    f = tmp_path / "out.bin"

    def work(job: Job):
        staged = Path(job.out_template.replace("%(ext)s", "bin"))
        staged.write_bytes(b"saved")
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": f.name},
            promotions=(Promotion(staged, f),),
        )

    jid = jm.submit(target=work, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)

    assert jm.dismiss(jid) is True
    hidden = jm.get(jid)
    assert hidden is not None
    assert hidden.dismissed_at == "2026-07-13T12:34:56.789Z"
    assert f.read_bytes() == b"saved"
    assert jm.dismiss(jid) is True
    assert jm.get(jid).dismissed_at == "2026-07-13T12:34:56.789Z"
    jm.shutdown()


def test_dismiss_marks_error_job():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    j = Job(id="errjob1", url="https://x", title="t", status=JobStatus.ERROR,
            error_category="unknown", error_message="boom")
    with jm._lock:
        jm._jobs["errjob1"] = j

    assert jm.dismiss("errjob1") is True
    assert jm.get("errjob1").dismissed_at is not None
    jm.shutdown()


def _wait_status(mgr, jid, status, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mgr.get(jid).status is status:
            return True
        time.sleep(0.01)
    return False


def test_queued_cancel_initial_download_stays_cancelled_and_target_never_runs():
    mgr = JobManager(max_workers=1)
    gate = threading.Event()
    ran = []
    a = mgr.submit(target=lambda job: gate.wait(5), title="a", url="u-a")
    b = mgr.submit(target=lambda job: ran.append(job.id), title="b", url="u-b")
    assert mgr.get(b).status is JobStatus.QUEUED
    assert mgr.cancel(b) is True
    gate.set()
    assert _wait_status(mgr, a, JobStatus.DONE)
    time.sleep(0.2)  # let b's worker slot fire and (correctly) no-op
    assert mgr.get(b).status is JobStatus.CANCELLED
    assert ran == []


def _wait_worker_inactive(mgr, jid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not mgr.get(jid)._worker_active:
            return True
        time.sleep(0.01)
    return False


def test_download_success_is_not_visible_before_attempt_cleanup(tmp_path, monkeypatch):
    from attempt_staging import AttemptOutcome, Promotion, cleanup_attempt as real_cleanup
    import jobs as jobs_module

    manager = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    published = tmp_path / "published.mp4"

    def delayed_cleanup(root):
        cleanup_started.set()
        assert release_cleanup.wait(5), "test did not release staging cleanup"
        real_cleanup(root)

    def target(job, *, attempt):
        staged = Path(job._staging_root) / "candidate.mp4"
        staged.write_bytes(b"published")
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": published.name},
            promotions=(Promotion(staged, published),),
        )

    monkeypatch.setattr(jobs_module, "cleanup_attempt", delayed_cleanup)
    jid = manager.submit(target=target, title="cleanup ordering", url="https://x")
    captured = manager._jobs[jid]
    try:
        assert cleanup_started.wait(2), "worker never reached staging cleanup"
        assert captured.status is JobStatus.DOWNLOADING
        release_cleanup.set()
        assert _wait_status(manager, jid, JobStatus.DONE)
        assert not Path(manager.get(jid)._staging_root).exists()
        assert published.read_bytes() == b"published"
    finally:
        release_cleanup.set()
        manager.shutdown(wait=True)


def test_queued_cancel_download_resume_never_runs_target(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    blocker = threading.Event()
    ran = []
    first = mgr.submit(target=lambda job: blocker.wait(5), title="first", url="u-first")
    resumed = Job(
        id="resume-me", url="u-resume", title="resume", status=JobStatus.PAUSED,
        out_template=str(tmp_path / "legacy.%(ext)s"),
    )
    with mgr._lock:
        mgr._jobs[resumed.id] = resumed

    assert mgr.resume(resumed.id, target=lambda job: ran.append(job.id)) is True
    assert mgr.cancel(resumed.id) is True
    blocker.set()
    assert _wait_status(mgr, first, JobStatus.DONE)
    assert _wait_worker_inactive(mgr, resumed.id)
    assert ran == []
    assert mgr.get(resumed.id).status is JobStatus.CANCELLED
    mgr.shutdown(wait=True)


@pytest.mark.parametrize("raises", [False, True])
def test_running_cancel_download_rejects_result_error_and_late_progress(tmp_path, raises):
    from attempt_staging import AttemptOutcome, Promotion

    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    final = tmp_path / "published.mp4"

    def target(job):
        attempt = job._attempt
        staged = Path(job.out_template.replace("%(ext)s", "mp4"))
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"candidate")
        started.set()
        release.wait(5)
        mgr.update_progress(
            job.id, job, attempt, downloaded=99, total=100, speed=1.0,
            eta=1, fragment_index=1, fragment_count=1,
        )
        if raises:
            raise RuntimeError("late failure")
        return AttemptOutcome(
            updates={"file_path": str(staged), "filename": "published.mp4"},
            promotions=(Promotion(staged, final),),
        )

    jid = mgr.submit(target=target, title="download", url="u")
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    job = mgr.get(jid)
    assert job.status is JobStatus.CANCELLED
    assert job.file_path is None and job.filename is None
    assert job.error_category is None and job.error_message is None
    assert job.downloaded_bytes == 0
    assert not final.exists()
    mgr.shutdown(wait=True)


def test_stale_attempt_late_process_registration_is_killed(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
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

    jid = mgr.submit(target=target, title="download", url="u")
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert killed == [True]
    assert mgr.get(jid).process is None
    mgr.shutdown(wait=True)


def test_stale_attempt_target_receives_dispatch_token_not_later_cancel_token(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def target(job, *, attempt=None):
        entered.set()
        release.wait(5)
        observed.append(attempt)

    jid = mgr.submit(target=target, title="download", url="u")
    assert entered.wait(2)
    dispatched = mgr.get(jid)._attempt
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert observed == [dispatched]
    mgr.shutdown(wait=True)


def test_legacy_download_target_cannot_mutate_canonical_job_after_cancel(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    killed = []

    class Proc:
        def kill(self):
            killed.append(True)

    def legacy_target(job):
        started.set()
        release.wait(5)
        job.status = JobStatus.DONE
        job.file_path = str(tmp_path / "late.mp4")
        job.filename = "late.mp4"
        job.process = Proc()

    jid = mgr.submit(target=legacy_target, title="download", url="u")
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)

    canonical = mgr.get(jid)
    assert canonical.status is JobStatus.CANCELLED
    assert canonical.file_path is None and canonical.filename is None
    assert canonical.process is None
    assert killed == [True]
    mgr.shutdown(wait=True)


def test_download_positional_only_attempt_is_not_passed_as_keyword(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    observed = []

    def legacy_target(job, attempt=None, /):
        observed.append((attempt, job is mgr.get(job.id)))

    jid = mgr.submit(target=legacy_target, title="download", url="u")
    assert _wait_status(mgr, jid, JobStatus.DONE)
    assert observed == [(None, False)]
    mgr.shutdown(wait=True)


def test_download_post_commit_entitlement_survives_concurrent_dismiss(tmp_path):
    from attempt_staging import AttemptOutcome

    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
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
                is_done = any(job.status is JobStatus.DONE for job in mgr._jobs.values())
            if is_done and not blocked:
                blocked = True
                done_persisted.set()
                assert release_persist.wait(5)
        return result

    mgr._persist = persist_with_done_barrier

    def target(job, *, attempt):
        worker_ident.append(threading.get_ident())
        return AttemptOutcome(after_commit=lambda committed: hook_calls.append(committed.id))

    jid = mgr.submit(target=target, title="download", url="u")
    assert done_persisted.wait(2)
    assert mgr.dismiss(jid) is True
    release_persist.set()
    assert _wait_worker_inactive(mgr, jid)
    assert hook_calls == [jid]
    mgr.shutdown(wait=True)


def test_stale_attempt_callback_cannot_mutate_dismissed_cancelled_job(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    callbacks = []

    def target(job):
        attempt = job._attempt
        callbacks.append(lambda: mgr.update_progress(
            job.id, job, attempt, downloaded=50, total=100, speed=2.0,
            eta=1, fragment_index=0, fragment_count=0,
        ))
        started.set()
        release.wait(5)

    jid = mgr.submit(target=target, title="download", url="u")
    assert started.wait(2)
    assert mgr.cancel(jid) is True
    assert mgr.dismiss(jid) is True
    assert callbacks[0]() is False
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    job = mgr.get(jid)
    assert job.status is JobStatus.CANCELLED and job.dismissed_at is not None
    assert job.downloaded_bytes == 0
    mgr.shutdown(wait=True)


def test_attempt_unwinding_blocks_resume_and_preserves_part_for_reuse(tmp_path):
    from jobs import AttemptUnwindingError

    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    resumed = threading.Event()
    observed = {}

    def first_target(job):
        root = Path(job.out_template).parent
        root.mkdir(parents=True, exist_ok=True)
        (root / "download.mp4.part").write_bytes(b"partial")
        observed["template"] = job.out_template
        started.set()
        release.wait(5)

    jid = mgr.submit(target=first_target, title="download", url="u")
    assert started.wait(2)
    assert mgr.pause(jid) is True
    with pytest.raises(AttemptUnwindingError):
        mgr.resume(jid, target=lambda job: None)

    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert mgr.get(jid).status is JobStatus.PAUSED

    def resumed_target(job):
        observed["resumed_template"] = job.out_template
        observed["part_exists"] = (Path(job.out_template).parent / "download.mp4.part").exists()
        resumed.set()

    assert mgr.resume(jid, target=resumed_target) is True
    assert resumed.wait(2)
    assert _wait_status(mgr, jid, JobStatus.DONE)
    assert observed["resumed_template"] == observed["template"]
    assert observed["part_exists"] is True
    mgr.shutdown(wait=True)


def test_stale_attempt_callback_cannot_touch_new_resume_attempt(tmp_path):
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    started = threading.Event()
    release = threading.Event()
    callbacks = []
    new_gate = threading.Event()

    def first_target(job):
        old_attempt = job._attempt
        callbacks.append(lambda: mgr.update_progress(
            job.id, job, old_attempt, downloaded=77, total=100, speed=1.0,
            eta=1, fragment_index=0, fragment_count=0,
        ))
        started.set()
        release.wait(5)

    jid = mgr.submit(target=first_target, title="download", url="u")
    assert started.wait(2)
    assert mgr.pause(jid) is True
    release.set()
    assert _wait_worker_inactive(mgr, jid)
    assert mgr.resume(jid, target=lambda job: new_gate.wait(5)) is True
    assert _wait_status(mgr, jid, JobStatus.DOWNLOADING)

    assert callbacks[0]() is False
    assert mgr.get(jid).downloaded_bytes == 0
    new_gate.set()
    assert _wait_status(mgr, jid, JobStatus.DONE)
    mgr.shutdown(wait=True)


def test_attempt_runtime_fields_are_not_persisted(tmp_path):
    import json

    store = tmp_path / "jobs.json"
    mgr = JobManager(max_workers=1, store_path=store)
    gate = threading.Event()
    jid = mgr.submit(target=lambda job: gate.wait(5), title="download", url="u")
    assert _wait_status(mgr, jid, JobStatus.DOWNLOADING)
    payload = json.loads(store.read_text())["jobs"][0]
    assert "_attempt" not in payload
    assert "_worker_active" not in payload
    assert "_staging_root" not in payload
    gate.set()
    mgr.shutdown(wait=True)


def test_pause_while_queued_stays_paused_until_resumed():
    mgr = JobManager(max_workers=1)
    gate = threading.Event()
    ran = []
    a = mgr.submit(target=lambda job: gate.wait(5), title="a", url="u-a")
    b = mgr.submit(target=lambda job: ran.append(job.id), title="b", url="u-b")
    assert mgr.pause(b) is True
    gate.set()
    assert _wait_status(mgr, a, JobStatus.DONE)
    time.sleep(0.2)
    assert mgr.get(b).status is JobStatus.PAUSED   # pause means pause — not auto-restarted
    assert ran == []
    assert mgr.resume(b, target=lambda job: ran.append(job.id)) is True
    assert _wait_status(mgr, b, JobStatus.DONE)
    assert ran == [b]


def test_dismiss_marks_cancelled_job():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    j = Job(id="canjob1", url="https://x", title="t", status=JobStatus.CANCELLED)
    with jm._lock:
        jm._jobs["canjob1"] = j

    assert jm.dismiss("canjob1") is True
    assert jm.get("canjob1").dismissed_at is not None
    jm.shutdown()


def test_dismiss_refuses_running_job():
    """Dismiss on DOWNLOADING returns False; job stays in _jobs."""
    jm = JobManager(max_workers=1, ttl_seconds=60)
    jid = jm.submit(target=lambda j: time.sleep(2), title="t", url="https://x")
    # Give the worker a moment to flip status to DOWNLOADING
    for _ in range(20):
        if jm.get(jid).status == JobStatus.DOWNLOADING:
            break
        time.sleep(0.02)
    assert jm.dismiss(jid) is False
    assert jm.get(jid) is not None
    jm.shutdown()


def test_dismiss_refuses_paused_job():
    """Dismiss on PAUSED returns False; user must cancel first."""
    jm = JobManager(max_workers=1, ttl_seconds=60)
    j = Job(id="pjob1", url="https://x", title="t", status=JobStatus.PAUSED)
    with jm._lock:
        jm._jobs["pjob1"] = j
    assert jm.dismiss("pjob1") is False
    assert jm.get("pjob1") is not None
    jm.shutdown()


def test_dismiss_returns_false_for_unknown_id():
    jm = JobManager(max_workers=1, ttl_seconds=60)
    assert jm.dismiss("nope") is False
    jm.shutdown()


# ---- bounded pending admission --------------------------------------


def _wait_pending_count(manager, expected, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager.pending_count == expected:
            return True
        time.sleep(0.01)
    return False


def _fill_download_capacity(manager, gate):
    return [
        manager.submit(
            target=lambda _job: gate.wait(5),
            title=f"fill-{index}",
            url=f"u-{index}",
        )
        for index in range(manager.pending_capacity)
    ]


def test_download_capacity_plus_one_creates_no_record_or_executor_work(monkeypatch):
    manager = JobManager(max_workers=1)
    gate = threading.Event()
    try:
        admitted = _fill_download_capacity(manager, gate)
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
            manager.submit(target=lambda _job: None, title="overflow", url="u-overflow")

        assert [job.id for job in manager.snapshot_jobs()] == before_ids == admitted
        assert manager.pending_count == manager.pending_capacity
        assert executor_calls == []
    finally:
        gate.set()
        manager.shutdown(wait=True)
    assert manager.pending_count == 0


@pytest.mark.parametrize("raises", [False, True])
def test_download_reservation_recovers_after_target_completion(raises):
    manager = JobManager(max_workers=1)

    def target(_job):
        if raises:
            raise RuntimeError("target failed")

    jid = manager.submit(target=target, title="one", url="u-one")
    assert _wait_worker_inactive(manager, jid)
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_download_completion_cleanup_oserror_still_releases_reservation(monkeypatch):
    import jobs

    cleanup_calls = []

    def fail_cleanup(root):
        cleanup_calls.append(root)
        raise OSError("cleanup failed")

    monkeypatch.setattr(jobs, "cleanup_attempt", fail_cleanup)
    manager = JobManager(max_workers=1)
    jid = manager.submit(target=lambda _job: None, title="one", url="u-one")

    assert _wait_worker_inactive(manager, jid)
    assert manager.get(jid).status is JobStatus.DONE
    assert cleanup_calls == [manager.get(jid)._staging_root]
    assert manager.pending_count == 0
    manager.shutdown(wait=True)


def test_download_queued_cancel_keeps_reservation_until_stale_wrapper_drains(monkeypatch):
    manager = JobManager(max_workers=1)
    gate = threading.Event()
    futures = []
    cancel_calls = []
    real_submit = manager._executor.submit

    def tracked_submit(*args, **kwargs):
        future = real_submit(*args, **kwargs)
        real_cancel = future.cancel

        def tracked_cancel():
            cancel_calls.append(True)
            return real_cancel()

        monkeypatch.setattr(future, "cancel", tracked_cancel)
        futures.append(future)
        return future

    monkeypatch.setattr(manager._executor, "submit", tracked_submit)
    try:
        admitted = _fill_download_capacity(manager, gate)
        queued = admitted[-1]
        assert manager.get(queued).status is JobStatus.QUEUED
        assert manager.cancel(queued) is True
        assert cancel_calls == []
        assert manager.pending_count == manager.pending_capacity
        with pytest.raises(QueueFullError):
            manager.submit(target=lambda _job: None, title="still-full", url="u-full")
    finally:
        gate.set()
        manager.shutdown(wait=True)

    assert cancel_calls == []
    assert manager.pending_count == 0


def test_download_resume_capacity_failure_is_exact_noop(tmp_path, monkeypatch):
    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, store_path=store)
    paused = Job(
        id="paused", url="u-paused", title="paused", status=JobStatus.PAUSED,
        error_category="old-error", error_message="old message",
    )
    paused._attempt = 7
    paused._was_paused = True
    with manager._lock:
        manager._ensure_download_staging_locked(paused)
        manager._jobs[paused.id] = paused
    manager._persist()
    gate = threading.Event()
    try:
        _fill_download_capacity(manager, gate)
        manager._persist()
        before_state = copy.deepcopy(vars(paused))
        before_store = store.read_bytes()
        executor_calls = []
        real_submit = manager._executor.submit
        monkeypatch.setattr(
            manager._executor,
            "submit",
            lambda *args, **kwargs: executor_calls.append((args, kwargs))
            or real_submit(*args, **kwargs),
        )

        with pytest.raises(QueueFullError):
            manager.resume(paused.id, target=lambda _job: None)

        assert vars(paused) == before_state
        assert store.read_bytes() == before_store
        assert executor_calls == []
        assert manager.pending_count == manager.pending_capacity
    finally:
        gate.set()
        manager.shutdown(wait=True)


class _RejectingExecutor:
    def __init__(self):
        self.submit_calls = 0
        self.shutdown_calls = []

    def submit(self, *_args, **_kwargs):
        self.submit_calls += 1
        raise RuntimeError("executor rejected")

    def shutdown(self, wait=False, **_kwargs):
        self.shutdown_calls.append(wait)


def test_download_submit_rejection_rolls_back_reservation_record_and_store(tmp_path):
    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, store_path=store)
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingExecutor()
    manager._executor = rejecting

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.submit(target=lambda _job: None, title="rejected", url="u-rejected")

    assert manager.pending_count == 0
    assert manager.snapshot_jobs() == []
    assert json.loads(store.read_text()) == {"version": 1, "jobs": []}
    assert rejecting.submit_calls == 1
    manager.shutdown(wait=True)


def test_download_submit_rejection_cleanup_oserror_still_releases_reservation(
    tmp_path, monkeypatch,
):
    import jobs

    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, store_path=store)
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingExecutor()
    manager._executor = rejecting
    cleanup_calls = []

    def fail_cleanup(root):
        cleanup_calls.append(root)
        raise OSError("cleanup failed")

    monkeypatch.setattr(jobs, "cleanup_attempt", fail_cleanup)

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.submit(target=lambda _job: None, title="rejected", url="u-rejected")

    assert len(cleanup_calls) == 1
    assert manager.pending_count == 0
    assert manager.snapshot_jobs() == []
    assert json.loads(store.read_text()) == {"version": 1, "jobs": []}
    assert rejecting.submit_calls == 1
    manager.shutdown(wait=True)


def test_download_resume_rejection_restores_paused_record_and_store(tmp_path):
    store = tmp_path / "jobs.json"
    manager = JobManager(max_workers=1, store_path=store)
    paused = Job(
        id="paused", url="u-paused", title="paused", status=JobStatus.PAUSED,
        error_category="old-error", error_message="old message",
    )
    paused._attempt = 7
    paused._was_paused = True
    with manager._lock:
        manager._ensure_download_staging_locked(paused)
        manager._jobs[paused.id] = paused
    manager._persist()
    before_state = copy.deepcopy(vars(paused))
    before_store = store.read_bytes()
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingExecutor()
    manager._executor = rejecting

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.resume(paused.id, target=lambda _job: None)

    assert vars(paused) == before_state
    assert store.read_bytes() == before_store
    assert manager.pending_count == 0
    assert rejecting.submit_calls == 1
    manager.shutdown(wait=True)


def test_legacy_download_resume_rejection_preserves_partial_store_and_retry(tmp_path):
    from jobs_store import persist_atomic

    store = tmp_path / "jobs.json"
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_template = legacy_root / "paused.%(ext)s"
    legacy_partial = legacy_root / "paused.mp4.part"
    legacy_partial.write_bytes(b"resume-bytes")
    persist_atomic(
        {
            "paused": Job(
                id="paused",
                url="u-paused",
                title="paused",
                status=JobStatus.PAUSED,
                out_template=str(legacy_template),
            ),
        },
        store,
    )
    before_store = store.read_bytes()

    manager = JobManager(max_workers=1, store_path=store)
    manager._executor.shutdown(wait=True)
    rejecting = _RejectingExecutor()
    manager._executor = rejecting

    with pytest.raises(RuntimeError, match="executor rejected"):
        manager.resume("paused", target=lambda _job: None)

    paused = manager.get("paused")
    assert paused is not None
    assert paused.status is JobStatus.PAUSED
    assert paused.out_template == str(legacy_template)
    assert legacy_partial.read_bytes() == b"resume-bytes"
    assert store.read_bytes() == before_store
    assert manager.pending_count == 0

    observed = []
    manager._executor = ThreadPoolExecutor(max_workers=1)

    def retry_target(job):
        partial = Path(job.out_template.replace("%(ext)s", "mp4.part"))
        observed.append(partial.read_bytes())

    assert manager.resume("paused", target=retry_target) is True
    assert _wait_worker_inactive(manager, "paused")
    assert observed == [b"resume-bytes"]
    assert manager.get("paused").status is JobStatus.DONE
    manager.shutdown(wait=True)


def test_download_shutdown_wait_false_rejects_new_work_and_drains():
    manager = JobManager(max_workers=1)
    gate = threading.Event()
    jid = manager.submit(target=lambda _job: gate.wait(5), title="active", url="u-active")
    assert _wait_status(manager, jid, JobStatus.DOWNLOADING)

    manager.shutdown(wait=False)
    assert manager.pending_count == 1
    before_ids = [job.id for job in manager.snapshot_jobs()]
    with pytest.raises(RuntimeError, match="shut down"):
        manager.submit(target=lambda _job: None, title="late", url="u-late")
    assert [job.id for job in manager.snapshot_jobs()] == before_ids

    gate.set()
    assert _wait_pending_count(manager, 0)
    manager.shutdown(wait=True)


def test_download_shutdown_wait_true_returns_with_zero_pending():
    manager = JobManager(max_workers=1)
    gate = threading.Event()
    manager.submit(target=lambda _job: gate.wait(5), title="active", url="u-active")
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
    with pytest.raises(RuntimeError, match="shut down"):
        manager.submit(target=lambda _job: None, title="late", url="u-late")


class _BlockingSubmitExecutor:
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


def test_download_submit_is_linearized_before_shutdown():
    manager = JobManager(max_workers=1)
    manager._executor.shutdown(wait=True)
    blocking = _BlockingSubmitExecutor()
    manager._executor = blocking
    submitted = []
    submit_error = []

    def submit_work():
        try:
            submitted.append(
                manager.submit(
                    target=lambda _job: None, title="racing", url="u-racing",
                ),
            )
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
