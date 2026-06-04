"""Tests for the Watch store — folder / channel / playlist automations (spec §5 Phase 3).
JSON-backed, atomic (mirrors recipes.py). User fields are CRUD'd; the per-watch automation
state (seen / pending / produced) is advanced by the reconciler via set_state."""
from __future__ import annotations

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


def test_update_repoint_resets_reconciler_state(tmp_path):
    # Changing kind OR target to a DIFFERENT value must reset seen/pending/produced/producing —
    # stale seen keys (esp. folder filenames) would otherwise suppress brand-new items at the new
    # target. A no-op / name-only PATCH must NOT wipe the history.
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "F", "kind": "folder", "target": "/in/a", "recipe_id": "r1"})
    s.set_state(w["id"], seen=["old.mp4"], pending={"old.mp4": "src1"},
                produced=["src1"], producing={"src1": {"job": "j1", "attempts": 1}})

    # name-only PATCH keeps the history
    same = s.update(w["id"], {"name": "Renamed"})
    assert same["seen"] == ["old.mp4"] and same["pending"] == {"old.mp4": "src1"}
    assert same["produced"] == ["src1"] and same["producing"] == {"src1": {"job": "j1", "attempts": 1}}

    # repointing the target to a new value resets all reconciler state
    moved = s.update(w["id"], {"target": "/in/b"})
    assert moved["target"] == "/in/b"
    assert moved["seen"] == [] and moved["pending"] == {} and moved["produced"] == [] and moved["producing"] == {}


def test_update_unknown_fields_dropped_and_unknown_id(tmp_path):
    s = WatchStore(str(tmp_path / "watches.json"))
    w = s.create({"name": "W", "kind": "folder", "target": "/x", "bogus": 1})
    assert "bogus" not in w
    assert s.update("nope", {"name": "x"}) is None
    assert s.delete("nope") is False
