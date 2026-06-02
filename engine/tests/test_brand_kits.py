"""Tests for the BrandKit store — persisted, reusable looks applied across a project's
clips (spec §5 Phase 2 / §3 BrandKit). JSON-backed, atomic, survives reload."""
from __future__ import annotations

from brand_kits import BrandKitStore


def test_create_list_get_update_delete(tmp_path):
    s = BrandKitStore(str(tmp_path / "kits.json"))
    assert s.list() == []

    k = s.create({"name": "Acme", "palette": ["#45556E", "#C98A3D"], "caption_preset": "opus",
                  "caption_overrides": {"highlight": "#FFE94D", "size": 110}, "watermark": "@acme"})
    assert k["id"] and k["name"] == "Acme"
    assert [x["id"] for x in s.list()] == [k["id"]]
    assert s.get(k["id"])["watermark"] == "@acme"
    assert s.get(k["id"])["caption_overrides"]["size"] == 110

    u = s.update(k["id"], {"name": "Acme Media", "lower_third": "Ep. 42"})
    assert u["name"] == "Acme Media" and u["lower_third"] == "Ep. 42"
    assert u["watermark"] == "@acme"  # untouched fields preserved

    # persisted across a fresh load of the same file
    s2 = BrandKitStore(str(tmp_path / "kits.json"))
    assert s2.get(k["id"])["name"] == "Acme Media"

    assert s.delete(k["id"]) is True
    assert s.get(k["id"]) is None and s.list() == []


def test_update_delete_unknown_id(tmp_path):
    s = BrandKitStore(str(tmp_path / "kits.json"))
    assert s.update("nope", {"name": "x"}) is None
    assert s.delete("nope") is False
