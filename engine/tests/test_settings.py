"""Tests for the settings store — persisted, writable engine config (spec §5 Phase 2 /
demo 07 Settings). JSON-backed singleton under the download dir, atomic, survives reload.

Distinct from brand_kits (a list of records): settings is one merged dict. ``get()`` always
returns every key (defaults merged with the user's overrides); ``overrides()`` returns only
what was explicitly written (so ``create_app`` can prefer a UI-set value over the env default
without confusing "user set 2" with "default 2")."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from settings import SettingsStore, DEFAULTS


def test_defaults_when_empty(tmp_path):
    s = SettingsStore(str(tmp_path / "settings.json"))
    assert s.get() == DEFAULTS
    assert s.get()["reasoning_provider"] == "none"
    assert s.get()["reasoning_egress_consent"] is False
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


@pytest.mark.parametrize(
    ("method", "changes", "message"),
    [
        ("update", {"reasoning_provider": "codex"}, "invalid reasoning_provider"),
        ("staged_update", {"reasoning_provider": "codex"}, "invalid reasoning_provider"),
        (
            "update",
            {"reasoning_egress_consent": True},
            "invalid reasoning_egress_consent",
        ),
        (
            "staged_update",
            {"reasoning_egress_consent": True},
            "invalid reasoning_egress_consent",
        ),
    ],
)
def test_store_rejects_remote_reasoning_atomically(tmp_path, method, changes, message):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update({"fast_default": False})
    before_values = store.get()
    before_overrides = store.overrides()
    before_bytes = path.read_bytes()
    yielded = False

    with pytest.raises(ValueError) as raised:
        if method == "update":
            store.update(changes)
        else:
            with store.staged_update(changes):
                yielded = True

    assert str(raised.value) == message
    assert yielded is False
    assert store.get() == before_values
    assert store.overrides() == before_overrides
    assert path.read_bytes() == before_bytes
    assert SettingsStore(path).get() == before_values


def test_failed_atomic_replace_leaves_memory_and_persisted_settings_unchanged(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.json")
    s = SettingsStore(path)
    s.update({
        "fast_default": False,
        "offline": False,
    })
    before = s.get()

    def fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr("settings.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        s.update({
            "offline": True,
            "clip_workers": 3,
        })

    assert s.get() == before
    assert SettingsStore(path).get() == before


def test_store_rejects_invalid_reasoning_values(tmp_path):
    s = SettingsStore(str(tmp_path / "settings.json"))

    with pytest.raises(ValueError, match="reasoning_provider"):
        s.update({"reasoning_provider": "mystery-cloud"})
    with pytest.raises(ValueError, match="reasoning_egress_consent"):
        s.update({"reasoning_egress_consent": "yes"})

    assert s.get()["reasoning_provider"] == "none"
    assert s.get()["reasoning_egress_consent"] is False


@pytest.mark.parametrize("legacy_provider", ["codex", "CoDeX", "CODEX"])
def test_load_durably_canonicalizes_legacy_reasoning_without_losing_settings(
    tmp_path, legacy_provider,
):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "reasoning_provider": legacy_provider,
        "reasoning_egress_consent": True,
        "fast_default": False,
        "clip_workers": 7,
        "mcp_transport": "streamable-http",
    }))

    loaded = SettingsStore(path)

    assert loaded.get()["reasoning_provider"] == "none"
    assert loaded.get()["reasoning_egress_consent"] is False
    assert loaded.get()["fast_default"] is False
    assert loaded.get()["clip_workers"] == 7
    assert loaded.get()["mcp_transport"] == "streamable-http"
    assert json.loads(path.read_text()) == {
        "reasoning_provider": "none",
        "reasoning_egress_consent": False,
        "fast_default": False,
        "clip_workers": 7,
        "mcp_transport": "streamable-http",
    }


def test_unrelated_update_keeps_legacy_reasoning_canonical_on_disk(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "reasoning_provider": "cOdEx",
        "reasoning_egress_consent": True,
        "default_preset": "shorts",
    }))
    store = SettingsStore(path)

    values = store.update({"fast_default": False})

    assert values["reasoning_provider"] == "none"
    assert values["reasoning_egress_consent"] is False
    assert values["default_preset"] == "shorts"
    assert values["fast_default"] is False
    assert json.loads(path.read_text()) == {
        "reasoning_provider": "none",
        "reasoning_egress_consent": False,
        "default_preset": "shorts",
        "fast_default": False,
    }


def test_failed_runtime_apply_rolls_back_store_environment_and_policy(
    tmp_path, monkeypatch,
):
    import app as app_module

    class FailOfflineOnce(dict):
        def __init__(self, values):
            super().__init__(values)
            self._failed = False

        def __setitem__(self, key, value):
            if key == "SPOOL_OFFLINE" and not self._failed:
                self._failed = True
                raise OSError("injected environment write failure")
            super().__setitem__(key, value)

    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    application = app_module.create_app()
    try:
        store = application.extensions["trove.settings"]
        store.update({"fast_default": False})
        path = tmp_path / "settings.json"
        before_values = store.get()
        before_overrides = store.overrides()
        before_bytes = path.read_bytes()
        policy = application.extensions["trove.network_policy"]

        live_env = FailOfflineOnce({
            "SPOOL_LLM_PROVIDER": "none",
            "UNRELATED": "preserved",
        })
        before_env = dict(live_env)
        monkeypatch.setattr(app_module, "os", SimpleNamespace(environ=live_env))

        with pytest.raises(OSError, match="injected environment write failure"):
            application.extensions["trove.commit_settings"]({
                "offline": True,
            })

        assert policy.offline is False
        assert store.get() == before_values
        assert store.overrides() == before_overrides
        assert path.read_bytes() == before_bytes
        assert SettingsStore(path).get() == before_values
        assert live_env == before_env
    finally:
        # This test creates an app inside the shared pytest process. Shutting the
        # process-wide service registry would permanently close subprocess
        # admission for later tests, so only drain this app's worker managers.
        application.extensions["trove.jobs"].shutdown(wait=True)
        application.extensions["trove.transcribe"].shutdown(wait=True)
        application.extensions["trove.clips"].shutdown(wait=True)


def test_failed_publish_after_runtime_apply_rolls_back_every_state(
    tmp_path, monkeypatch,
):
    import app as app_module
    import settings as settings_module

    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    application = app_module.create_app()
    try:
        store = application.extensions["trove.settings"]
        store.update({"fast_default": False})
        path = tmp_path / "settings.json"
        before_values = store.get()
        before_overrides = store.overrides()
        before_bytes = path.read_bytes()
        before_env = {
            key: os.environ.get(key)
            for key in (
                "SPOOL_OFFLINE",
                "SPOOL_LLM_PROVIDER",
                "SPOOL_LLM_EGRESS_CONSENT",
            )
        }
        policy = application.extensions["trove.network_policy"]
        observed_at_publish = {}

        def fail_replace(_source, _destination):
            observed_at_publish.update(
                offline=os.environ.get("SPOOL_OFFLINE"),
                provider=os.environ.get("SPOOL_LLM_PROVIDER"),
                consent=os.environ.get("SPOOL_LLM_EGRESS_CONSENT"),
                policy_offline=policy.offline,
            )
            raise OSError("injected atomic publish failure")

        monkeypatch.setattr(settings_module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected atomic publish failure"):
            application.extensions["trove.commit_settings"]({
                "offline": True,
                "default_preset": "shorts",
            })

        assert observed_at_publish == {
            "offline": "1",
            "provider": "none",
            "consent": None,
            "policy_offline": True,
        }
        assert policy.offline is False
        assert store.get() == before_values
        assert store.overrides() == before_overrides
        assert path.read_bytes() == before_bytes
        assert SettingsStore(path).get() == before_values
        assert {
            key: os.environ.get(key)
            for key in before_env
        } == before_env
    finally:
        # Keep the process-wide service registry open for the rest of the test
        # session; production closes it only when the engine process exits.
        application.extensions["trove.jobs"].shutdown(wait=True)
        application.extensions["trove.transcribe"].shutdown(wait=True)
        application.extensions["trove.clips"].shutdown(wait=True)
