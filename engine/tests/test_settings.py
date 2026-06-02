"""Tests for the settings store — persisted, writable engine config (spec §5 Phase 2 /
demo 07 Settings). JSON-backed singleton under the download dir, atomic, survives reload.

Distinct from brand_kits (a list of records): settings is one merged dict. ``get()`` always
returns every key (defaults merged with the user's overrides); ``overrides()`` returns only
what was explicitly written (so ``create_app`` can prefer a UI-set value over the env default
without confusing "user set 2" with "default 2")."""
from __future__ import annotations

from settings import SettingsStore, DEFAULTS


def test_defaults_when_empty(tmp_path):
    s = SettingsStore(str(tmp_path / "settings.json"))
    assert s.get() == DEFAULTS
    assert s.overrides() == {}
    # get() returns a copy — mutating it must not corrupt the store's defaults.
    s.get()["fast_default"] = "mutated"
    assert s.get()["fast_default"] == DEFAULTS["fast_default"]


def test_update_persists_and_merges(tmp_path):
    p = str(tmp_path / "settings.json")
    s = SettingsStore(p)
    out = s.update({"fast_default": False, "clip_workers": 4})
    assert out["fast_default"] is False
    assert out["clip_workers"] == 4
    # untouched keys still carry their defaults
    assert out["default_preset"] == DEFAULTS["default_preset"]
    # overrides records only what was written
    assert s.overrides() == {"fast_default": False, "clip_workers": 4}

    # a second update merges (does not replace) prior overrides
    s.update({"default_preset": "reels"})
    assert s.overrides() == {"fast_default": False, "clip_workers": 4, "default_preset": "reels"}

    # persisted across a fresh load of the same file
    s2 = SettingsStore(p)
    assert s2.get()["clip_workers"] == 4
    assert s2.get()["default_preset"] == "reels"
    assert s2.overrides() == {"fast_default": False, "clip_workers": 4, "default_preset": "reels"}


def test_unknown_keys_ignored(tmp_path):
    s = SettingsStore(str(tmp_path / "settings.json"))
    out = s.update({"bogus": 1, "fast_default": True})
    assert "bogus" not in out
    assert out["fast_default"] is True
    assert "bogus" not in s.overrides()


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ this is not json")
    s = SettingsStore(str(p))
    assert s.get() == DEFAULTS
    assert s.overrides() == {}
