import pytest

import app as app_module
import watcher


REMOTE_REASONING_UNAVAILABLE = {
    "error": "remote_reasoning_unavailable",
    "message": (
        "Remote reasoning is unavailable in Phase 0 until a supported "
        "zero-tool transport ships."
    ),
}


@pytest.fixture
def watch_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setenv("SPOOL_WATCH_INTERVAL", "0")
    application = app_module.create_app()
    yield application
    application.extensions["trove.jobs"].shutdown(wait=True)
    application.extensions["trove.transcribe"].shutdown(wait=True)
    application.extensions["trove.clips"].shutdown(wait=True)


def _assert_reasoning_unavailable(response):
    assert response.status_code == 409
    assert response.get_json() == REMOTE_REASONING_UNAVAILABLE


def test_watch_scan_rejects_before_listing_or_ingest_capacity(watch_app, monkeypatch):
    store = watch_app.extensions["trove.watches"]
    watch = store.create({
        "name": "Inbox",
        "kind": "folder",
        "target": "/incoming",
    })
    before = store.get(watch["id"])

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("disabled watch scan reached listing or download admission")

    monkeypatch.setattr(watcher, "list_folder_items", unexpected_call)
    monkeypatch.setattr(
        watch_app.extensions["trove.jobs"], "submit", unexpected_call
    )

    response = watch_app.test_client().post(
        f"/api/v1/watches/{watch['id']}/scan"
    )

    _assert_reasoning_unavailable(response)
    assert store.get(watch["id"]) == before


def test_watch_scan_rejects_before_produce_capacity_or_state_change(
    watch_app, monkeypatch
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
    before = store.get(watch["id"])

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("disabled watch scan reached production admission")

    monkeypatch.setattr(watcher, "list_folder_items", unexpected_call)
    monkeypatch.setattr(
        watch_app.extensions["trove.clips"], "submit", unexpected_call
    )

    response = watch_app.test_client().post(
        f"/api/v1/watches/{watch['id']}/scan"
    )

    _assert_reasoning_unavailable(response)
    assert store.get(watch["id"]) == before
