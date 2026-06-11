"""Tests for the Watch store — folder / channel / playlist automations (spec §5 Phase 3).
JSON-backed, atomic (mirrors recipes.py). User fields are CRUD'd; the per-watch automation
state (seen / pending / produced) is advanced by the reconciler via set_state."""
from __future__ import annotations

import threading

from watches import WatchStore


def test_create_list_get_update_delete(tmp_path):
    s = WatchStore(str(tmp_path / "watches.json"))
    assert s.list() == []

    w = s.create({"name": "My channel", "kind": "channel",
                  "target": "https://youtube.com/@x", "recipe_id": "r1"})
    assert w["id"] and w["name"] == "My channel" and w["enabled"] is True
    assert w["seen"] == [] and w["pending"] == {} and w["produced"] == []
    assert s.get(w["id"])["kind"] == "channel"

    u = s.update(w["id"], {"enabled": False, "recipe_id": "r2"})
    assert u["enabled"] is False and u["recipe_id"] == "r2"
    assert u["target"] == "https://youtube.com/@x"   # untouched fields preserved

    s2 = WatchStore(str(tmp_path / "watches.json"))   # persisted across reload
    assert s2.get(w["id"])["recipe_id"] == "r2"

    assert s.delete(w["id"]) is True
    assert s.get(w["id"]) is None and s.list() == []


def test_set_state_tracks_automation_progress(tmp_path):
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "W", "kind": "folder", "target": "/clips/in", "recipe_id": "r1"})
    s.set_state(w["id"], seen=["a.mp4"], pending={"a.mp4": "src1"})
    s.set_state(w["id"], produced=["src1"])
    got = s.get(w["id"])
    assert got["seen"] == ["a.mp4"] and got["pending"] == {"a.mp4": "src1"} and got["produced"] == ["src1"]
    # set_state does not touch user fields
    assert got["recipe_id"] == "r1" and got["kind"] == "folder"


def test_set_state_persists_producing_jobs(tmp_path):
    # The reconciler tracks in-flight produce jobs in `producing` (sid -> {job, attempts}) so a
    # pending retry survives an engine restart. create() initializes it; set_state advances it.
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "W", "kind": "folder", "target": "/in", "recipe_id": "r1"})
    assert w["producing"] == {}
    s.set_state(w["id"], producing={"src1": {"job": "j1", "attempts": 2}})
    reloaded = WatchStore(str(tmp_path / "watches.json"))   # survives a restart
    assert reloaded.get(w["id"])["producing"] == {"src1": {"job": "j1", "attempts": 2}}


def test_set_state_persists_ingesting_retries(tmp_path):
    # A transient ingest failure tracks its attempt count in `ingesting` (item key -> n) so the
    # bounded retry survives an engine restart. create() initializes it; set_state advances it.
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "W", "kind": "folder", "target": "/in", "recipe_id": "r1"})
    assert w["ingesting"] == {}
    s.set_state(w["id"], ingesting={"flaky.mp4": 2})
    reloaded = WatchStore(str(tmp_path / "watches.json"))   # survives a restart
    assert reloaded.get(w["id"])["ingesting"] == {"flaky.mp4": 2}


def test_update_repoint_resets_reconciler_state(tmp_path):
    # Changing kind OR target to a DIFFERENT value must reset seen/ingesting/pending/produced/
    # producing — stale seen keys (esp. folder filenames) would otherwise suppress brand-new items
    # at the new target. A no-op / name-only PATCH must NOT wipe the history.
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "F", "kind": "folder", "target": "/in/a", "recipe_id": "r1"})
    s.set_state(w["id"], seen=["old.mp4"], pending={"old.mp4": "src1"}, ingesting={"flaky.mp4": 1},
                produced=["src1"], producing={"src1": {"job": "j1", "attempts": 1}})

    # name-only PATCH keeps the history
    same = s.update(w["id"], {"name": "Renamed"})
    assert same["seen"] == ["old.mp4"] and same["pending"] == {"old.mp4": "src1"}
    assert same["ingesting"] == {"flaky.mp4": 1}
    assert same["produced"] == ["src1"] and same["producing"] == {"src1": {"job": "j1", "attempts": 1}}

    # repointing the target to a new value resets all reconciler state
    moved = s.update(w["id"], {"target": "/in/b"})
    assert moved["target"] == "/in/b"
    assert moved["seen"] == [] and moved["pending"] == {} and moved["produced"] == [] and moved["producing"] == {}
    assert moved["ingesting"] == {}


def test_update_unknown_fields_dropped_and_unknown_id(tmp_path):
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "W", "kind": "folder", "target": "/x", "bogus": 1})
    assert "bogus" not in w
    assert s.update("nope", {"name": "x"}) is None
    assert s.delete("nope") is False


def test_concurrent_crud_and_set_state_lose_nothing(tmp_path):
    """API CRUD races the reconciler's set_state: without a store lock, list-mutation +
    shared .tmp writes lose watches or tear watches.json (reloaded as [])."""
    store = WatchStore(tmp_path / "watches.json")
    base = store.create({"name": "base", "kind": "folder", "target": "/tmp/x"})
    errors = []
    def crud(n):
        try:
            for i in range(30):
                w = store.create({"name": f"w{n}-{i}", "kind": "folder", "target": "/t"})
                store.update(w["id"], {"name": f"w{n}-{i}b"})
        except Exception as e:
            errors.append(e)
    def state():
        try:
            for i in range(60):
                store.set_state(base["id"], seen=[f"s{i}"])
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=crud, args=(n,)) for n in range(3)]
    threads.append(threading.Thread(target=state))
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    assert len(store.list()) == 91          # base + 3×30, nothing lost in memory
    fresh = WatchStore(tmp_path / "watches.json")
    assert len(fresh.list()) == 91          # …or on disk
