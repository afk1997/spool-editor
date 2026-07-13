import os
import threading
import time
from pathlib import Path
import pytest
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


def test_pool_full_returns_overflow():
    jm = JobManager(max_workers=1, ttl_seconds=60, queue_size=0)
    started = []

    def slow(job: Job):
        started.append(job.id)
        time.sleep(0.5)

    j1 = jm.submit(target=slow, title="a", url="https://x")
    with pytest.raises(RuntimeError):
        jm.submit(target=slow, title="b", url="https://y")
    jm.shutdown(wait=True)


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
