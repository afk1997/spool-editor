import os
import threading
import time
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


def test_cancel_done_is_noop_and_preserves_published_file(tmp_path):
    jm = JobManager(max_workers=1, ttl_seconds=60)

    published = tmp_path / "published.mp4"
    published.write_bytes(b"published-download")

    def work(job: Job):
        job.file_path = str(published)

    jid = jm.submit(target=work, title="t", url="https://x")
    for _ in range(50):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.05)
    cancelled = jm.cancel(jid)
    assert cancelled is False
    assert jm.get(jid).status == JobStatus.DONE
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
    jm = JobManager(max_workers=1, ttl_seconds=0)  # zero = sweep immediately

    def work(job: Job):
        f = tmp_path / "out.bin"
        f.write_bytes(b"x")
        job.file_path = str(f)

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
    jm = JobManager(max_workers=1, ttl_seconds=0)

    def work(job: Job):
        f = tmp_path / f"{job.id}.bin"
        f.write_bytes(b"x")
        job.file_path = str(f)

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


def test_runner_success_during_pause_window_promotes_to_done(tmp_path):
    """When pause() fires after target() wrote file_path but before _run
    re-acquires the lock, the post-target promotion should still mark the
    job DONE. Without this guard the job would be stuck in PAUSED.
    """
    jm = JobManager(max_workers=1, ttl_seconds=60)

    def work(job: Job):
        # Simulate yt-dlp completing successfully — file_path is set.
        out = tmp_path / "out.mp4"
        out.write_bytes(b"ok")
        job.file_path = str(out)
        # Now simulate pause() racing in: status flips to PAUSED before
        # the runner thread re-acquires the lock for the terminal status set.
        with jm._lock:
            job.status = JobStatus.PAUSED
            job._was_paused = True

    jid = jm.submit(target=work, title="t", url="https://x")
    # Wait specifically for DONE — under load _run may not have re-acquired
    # the lock yet so we'd briefly see PAUSED, but the race-promotion path
    # must converge on DONE.
    for _ in range(200):
        if jm.get(jid).status == JobStatus.DONE:
            break
        time.sleep(0.02)

    j = jm.get(jid)
    assert j.status == JobStatus.DONE, f"expected DONE, got {j.status}"
    assert j._was_paused is False
    jm.shutdown()


def test_snapshot_jobs_returns_insertion_ordered_list():
    jm = JobManager(max_workers=2, ttl_seconds=60)
    j1 = jm.submit(target=lambda j: None, title="a", url="https://1")
    j2 = jm.submit(target=lambda j: None, title="b", url="https://2")
    snap = jm.snapshot_jobs()
    assert [j.id for j in snap] == [j1, j2]
    jm.shutdown()


def test_dismiss_marks_done_job_idempotently_and_preserves_file(tmp_path, monkeypatch):
    import jobs
    monkeypatch.setattr(jobs, "_utc_now_rfc3339", lambda: "2026-07-13T12:34:56.789Z", raising=False)
    jm = JobManager(max_workers=1, ttl_seconds=60)
    f = tmp_path / "out.bin"
    f.write_bytes(b"saved")

    def work(job: Job):
        job.file_path = str(f)

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


def test_cancel_while_queued_stays_cancelled_and_target_never_runs():
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
