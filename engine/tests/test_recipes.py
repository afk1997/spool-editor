"""Tests for the Recipe store — a saved end-to-end pipeline (content mode/count + optional
ranking weights + aspect/reframe/caption/brand-kit/platform/fast) that drives render.pipeline
(spec §5 Phase 3 / §3 Recipe). JSON-backed, atomic, survives reload — mirrors brand_kits.py."""
from __future__ import annotations

from recipes import RecipeStore


def test_create_list_get_update_delete(tmp_path):
    s = RecipeStore(str(tmp_path / "recipes.json"))
    assert s.list() == []

    r = s.create({"name": "Punchy Shorts", "content_mode": "funny", "count": 8, "aspect": "9:16",
                  "reframe_mode": "pan", "caption_preset": "karaoke", "platform": "tiktok",
                  "fast": True, "weights": {"energy": 4, "hook": 5}, "brand_kit_id": "kit123abc"})
    assert r["id"] and r["name"] == "Punchy Shorts"
    assert [x["id"] for x in s.list()] == [r["id"]]
    got = s.get(r["id"])
    assert got["content_mode"] == "funny" and got["count"] == 8
    assert got["caption_preset"] == "karaoke" and got["platform"] == "tiktok"
    assert got["weights"] == {"energy": 4, "hook": 5} and got["brand_kit_id"] == "kit123abc"

    u = s.update(r["id"], {"count": 5, "platform": "reels"})
    assert u["count"] == 5 and u["platform"] == "reels"
    assert u["content_mode"] == "funny"   # untouched fields preserved

    s2 = RecipeStore(str(tmp_path / "recipes.json"))   # persisted across a fresh load
    assert s2.get(r["id"])["platform"] == "reels"

    assert s.delete(r["id"]) is True
    assert s.get(r["id"]) is None and s.list() == []


def test_unknown_fields_dropped_and_unknown_id(tmp_path):
    s = RecipeStore(str(tmp_path / "recipes.json"))
    r = s.create({"name": "X", "content_mode": "story", "bogus": "nope"})
    assert "bogus" not in r and r["name"] == "X"
    assert s.update("nope", {"name": "x"}) is None
    assert s.delete("nope") is False
