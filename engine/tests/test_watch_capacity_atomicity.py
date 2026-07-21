from pathlib import Path

import pytest

import app as app_module
from job_capacity import QueueFullError
import watcher


@pytest.fixture
def watch_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setenv("SPOOL_WATCH_INTERVAL", "0")
    application = app_module.create_app()
    yield application
    application.extensions["trove.jobs"].shutdown(wait=True)
    application.extensions["trove.transcribe"].shutdown(wait=True)
    application.extensions["trove.clips"].shutdown(wait=True)


def _assert_queue_full(response):
    assert response.status_code == 429
    assert response.get_json() == {"error": "queue_full", "retry_after": 1}
    assert response.headers["Retry-After"] == "1"


def test_multi_item_ingest_persists_first_admission_before_queue_full(
    watch_app, monkeypatch,
):
    store = watch_app.extensions["trove.watches"]
    watch = store.create({
        "name": "Inbox",
        "kind": "folder",
        "target": "/incoming",
    })
    monkeypatch.setattr(
        watcher,
        "list_folder_items",
        lambda *_args, **_kwargs: ["a.mp4", "b.mp4"],
    )
    manager = watch_app.extensions["trove.jobs"]
    attempted = []
    accepted = []

    def capacity_one(**kwargs):
        attempted.append(kwargs["title"])
        if len(attempted) == 2:
            raise QueueFullError("download queue full")
        accepted.append("source-a")
        return "source-a"

    monkeypatch.setattr(manager, "submit", capacity_one)
    response = watch_app.test_client().post(f"/api/v1/watches/{watch['id']}/scan")

    _assert_queue_full(response)
    assert attempted == ["a.mp4", "b.mp4"]
    assert accepted == ["source-a"]
    persisted = store.get(watch["id"])
    assert persisted["seen"] == ["a.mp4"]
    assert persisted["pending"] == {"a.mp4": "source-a"}

    retried = []

    def retry_submit(**kwargs):
        retried.append(kwargs["title"])
        return "source-b"

    monkeypatch.setattr(manager, "submit", retry_submit)
    retry = watch_app.test_client().post(f"/api/v1/watches/{watch['id']}/scan")

    assert retry.status_code == 200
    assert retried == ["b.mp4"]
    persisted = store.get(watch["id"])
    assert persisted["seen"] == ["a.mp4", "b.mp4"]
    assert persisted["pending"] == {
        "a.mp4": "source-a",
        "b.mp4": "source-b",
    }


def test_multi_item_produce_persists_first_admission_before_queue_full(
    watch_app, monkeypatch, tmp_path,
):
    store = watch_app.extensions["trove.watches"]
    watch = store.create({
        "name": "Ready",
        "kind": "folder",
        "target": "/incoming",
    })
    store.set_state(
        watch["id"],
        seen=["a.mp4", "b.mp4"],
        pending={"a.mp4": "source-a", "b.mp4": "source-b"},
    )
    monkeypatch.setattr(watcher, "list_folder_items", lambda *_args, **_kwargs: [])
    runner = watch_app.extensions["trove.clip_runner"]
    words = {}
    for source_id in ("source-a", "source-b"):
        path = tmp_path / f"{source_id}.words.json"
        path.write_text("{}")
        words[source_id] = path
    monkeypatch.setattr(
        runner,
        "source_paths",
        lambda source_id: (Path("unused"), words[source_id]),
    )
    monkeypatch.setattr(
        runner,
        "produce_target",
        lambda **_kwargs: (lambda _job: None),
    )
    manager = watch_app.extensions["trove.clips"]
    attempted = []
    accepted = []

    def capacity_one(**kwargs):
        source_id = kwargs["source_id"]
        attempted.append(source_id)
        if len(attempted) == 2:
            raise QueueFullError("media queue full")
        accepted.append("produce-a")
        return "produce-a"

    monkeypatch.setattr(manager, "submit", capacity_one)
    response = watch_app.test_client().post(f"/api/v1/watches/{watch['id']}/scan")

    _assert_queue_full(response)
    assert attempted == ["source-a", "source-b"]
    assert accepted == ["produce-a"]
    persisted = store.get(watch["id"])
    assert persisted["pending"] == {"b.mp4": "source-b"}
    assert persisted["producing"] == {
        "source-a": {"job": "produce-a", "attempts": 1},
    }

    retried = []

    def retry_submit(**kwargs):
        retried.append(kwargs["source_id"])
        return "produce-b"

    monkeypatch.setattr(manager, "submit", retry_submit)
    retry = watch_app.test_client().post(f"/api/v1/watches/{watch['id']}/scan")

    assert retry.status_code == 200
    assert retried == ["source-b"]
    persisted = store.get(watch["id"])
    assert persisted["pending"] == {}
    assert persisted["producing"] == {
        "source-a": {"job": "produce-a", "attempts": 1},
        "source-b": {"job": "produce-b", "attempts": 1},
    }
