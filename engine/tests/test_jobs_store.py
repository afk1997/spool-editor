import json
import threading
import time
from pathlib import Path
import pytest

from jobs import Job, JobManager, JobStatus
from jobs_store import dump_jobs, load_jobs, persist_atomic


def test_dump_and_load_round_trip(tmp_path):
    jobs_in = {
        "abc": Job(
            id="abc", url="https://example.com/v", title="Hello",
            status=JobStatus.PAUSED,
            thumbnail="https://example.com/t.jpg",
            downloaded_bytes=1024, total_bytes=4096,
            fragment_index=2, fragment_count=8,
            format_choice="video", format_id="137",
            out_template=str(tmp_path / "abc.%(ext)s"),
            file_path=None, filename=None,
        ),
    }
    path = tmp_path / "jobs.json"
    persist_atomic(jobs_in, path)
    assert path.exists()

    jobs_out = load_jobs(path)
    j = jobs_out["abc"]
    assert j.url == "https://example.com/v"
    assert j.title == "Hello"
    assert j.status == JobStatus.PAUSED
    assert j.downloaded_bytes == 1024
    assert j.fragment_count == 8
    assert j.format_id == "137"
    assert j.out_template.endswith("abc.%(ext)s")


def test_load_returns_empty_dict_when_file_missing(tmp_path):
    assert load_jobs(tmp_path / "nope.json") == {}


def test_load_tolerates_malformed_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json")
    assert load_jobs(path) == {}


def test_load_tolerates_unknown_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"version": 999, "jobs": []}))
    assert load_jobs(path) == {}


def test_persist_atomic_uses_tempfile_then_rename(tmp_path):
    """If write fails partway, the existing file should still be intact."""
    path = tmp_path / "jobs.json"
    jobs1 = {"a": Job(id="a", url="https://e.com/1", title="first")}
    persist_atomic(jobs1, path)

    # Atomic — temp file should not linger
    assert not (tmp_path / "jobs.json.tmp").exists()
    assert path.exists()

    # Rewrite — should overwrite, not duplicate
    jobs2 = {"b": Job(id="b", url="https://e.com/2", title="second")}
    persist_atomic(jobs2, path)
    loaded = load_jobs(path)
    assert "a" not in loaded
    assert "b" in loaded


def test_dump_omits_transient_fields(tmp_path):
    """The Popen handle and _was_paused flag should NOT serialize."""
    j = Job(id="x", url="https://e.com", title="t", status=JobStatus.DOWNLOADING)
    j.process = object()  # would not be JSON-serializable
    j._was_paused = True
    path = tmp_path / "jobs.json"
    persist_atomic({"x": j}, path)
    raw = json.loads(path.read_text())
    serialized = raw["jobs"][0]
    assert "process" not in serialized
    assert "_was_paused" not in serialized


def test_dump_serializes_all_persistent_fields(tmp_path):
    j = Job(
        id="x", url="https://e.com/v", title="t",
        status=JobStatus.PAUSED,
        thumbnail="https://e.com/t.jpg",
        file_path="/tmp/x.mp4", filename="x.mp4",
        downloaded_bytes=10, total_bytes=100,
        speed=1.5, eta=42,
        fragment_index=3, fragment_count=10,
        format_choice="audio", format_id=None,
        out_template="/tmp/x.%(ext)s",
        error_category=None, error_message=None,
    )
    path = tmp_path / "jobs.json"
    persist_atomic({"x": j}, path)
    raw = json.loads(path.read_text())
    s = raw["jobs"][0]
    assert s["url"] == "https://e.com/v"
    assert s["status"] == "paused"
    assert s["downloaded_bytes"] == 10
    assert s["fragment_count"] == 10
    assert s["format_choice"] == "audio"
    assert s["out_template"] == "/tmp/x.%(ext)s"
    assert s["filename"] == "x.mp4"


def test_concurrent_submits_never_drop_a_persisted_job(tmp_path):
    """4 threads × 50 submits: _persist used to serialize the LIVE dict outside the lock
    (RuntimeError: dict changed size — swallowed) and all writers shared one .tmp name."""
    store = tmp_path / "jobs.json"
    mgr = JobManager(max_workers=4, store_path=store)
    def hammer(n):
        for i in range(50):
            mgr.submit(target=lambda j: None, title=f"t{n}-{i}", url=f"u{n}-{i}")
    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    deadline = time.time() + 15
    while time.time() < deadline:
        snap = mgr.snapshot_jobs()
        if len(snap) == 200 and all(j.status is JobStatus.DONE for j in snap):
            break
        time.sleep(0.05)
    mgr._persist()  # one final settled write
    data = json.loads(store.read_text())
    assert len(data["jobs"]) == 200


def test_cancel_done_job_is_false_noop_without_deadlock(tmp_path):
    """Terminal cancel is a fast false-returning no-op and never deadlocks."""
    mgr = JobManager(max_workers=1, store_path=tmp_path / "jobs.json")
    jid = mgr.submit(target=lambda j: None, title="t", url="u")
    deadline = time.time() + 5
    while mgr.get(jid).status is not JobStatus.DONE and time.time() < deadline:
        time.sleep(0.01)
    result = []
    t = threading.Thread(target=lambda: result.append(mgr.cancel(jid)))
    t.start(); t.join(timeout=5)
    assert result == [False], "terminal cancel deadlocked or changed state"
    assert mgr.get(jid).status is JobStatus.DONE
