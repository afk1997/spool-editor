"""Settings store — persisted, writable engine config surfaced by the demo's Settings
screen (07) and the v1 ``/settings`` routes (spec §5 Phase 2 "config from the UI").

JSON-backed under the download dir, atomic writes — mirrors ``brand_kits.BrandKitStore``,
but settings is a single merged *dict* (a singleton), not a list of records.

Two reads matter:
  * ``get()``       — every key, defaults merged with the user's overrides (what the UI shows
                      and what hot consumers like ``clip_runner._do_export`` read).
  * ``overrides()`` — only the keys the user explicitly wrote. ``create_app`` prefers a
                      UI-set value over the env-var default *only when one was actually set*,
                      so it can't confuse "user chose 2" with "default happens to be 2".

Which keys apply when:
  * ``fast_default`` / ``default_preset`` / ``default_aspect`` — **hot** (read per render).
  * ``offline`` — **hot** (the apply-hook drives ``SPOOL_OFFLINE`` in-process the moment it's
    patched; ``clip.llm.is_offline`` reads that env to refuse egress providers).
  * ``clip_workers`` / ``max_workers`` — **restart** (the thread pools size at ``create_app``).
  * ``mcp_transport`` — **restart** (read by ``mcp_server.main`` when the MCP server boots).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import tempfile
import threading
from typing import Callable, Iterator

# The full set of writable keys + their defaults. Defaults mirror the engine's existing
# env/arg defaults (TROVE_CLIP_WORKERS=2, TROVE_MAX_WORKERS=4, exporter fast=True,
# preset=tiktok, reframe aspect=9:16, MCP stdio) so an unconfigured store is a no-op.
DEFAULTS = {
    "fast_default": True,        # export fast vs quality when a render omits `fast` (hot)
    "default_preset": "tiktok",  # platform preset when a render omits `preset` (hot)
    "offline": False,            # block LLM egress (drives SPOOL_OFFLINE; hot)
    "reasoning_provider": "none",  # none | codex; remote reasoning is opt-in
    "reasoning_egress_consent": False,  # explicit consent for transcript-text egress
    "clip_workers": 2,           # render-queue concurrency (applies on restart)
    "max_workers": 4,            # download-queue concurrency (applies on restart)
    "mcp_transport": "stdio",    # MCP server transport (applies on restart)
}

# Anything outside this set in an update body is ignored (defense in depth — the route
# validator is the first gate, the store is the second).
_FIELDS = tuple(DEFAULTS.keys())
_REASONING_PROVIDERS = {"none", "codex"}


class SettingsStore:
    """A tiny JSON-backed singleton-dict settings store, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._overrides = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict):
            return {}
        loaded = {k: doc[k] for k in _FIELDS if k in doc}
        if loaded.get("reasoning_provider", "none") not in _REASONING_PROVIDERS:
            loaded.pop("reasoning_provider", None)
        if not isinstance(loaded.get("reasoning_egress_consent", False), bool):
            loaded.pop("reasoning_egress_consent", None)
        if (
            loaded.get("reasoning_provider", DEFAULTS["reasoning_provider"]) == "none"
            and loaded.get("reasoning_egress_consent") is True
        ):
            loaded["reasoning_egress_consent"] = False
        return loaded

    def _stage(self, overrides: dict) -> str:
        """Durably write ``overrides`` beside the store without publishing it."""
        parent = os.path.dirname(self.path) or "."
        prefix = os.path.basename(self.path) + "."
        fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(overrides, f)
                f.flush()
                os.fsync(f.fileno())
            return tmp
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _save(self, overrides: dict) -> str:
        """Prepare a durable candidate file for the transactional publish."""
        return self._stage(overrides)

    def _publish(self, overrides: dict) -> None:
        """Atomically publish a staged settings document."""
        tmp = self._save(overrides)
        try:
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def get(self) -> dict:
        """Every key: defaults merged with the user's overrides (a fresh copy)."""
        with self._lock:
            return {**DEFAULTS, **self._overrides}

    def overrides(self) -> dict:
        """Only the keys the user explicitly wrote (a fresh copy)."""
        with self._lock:
            return dict(self._overrides)

    @contextmanager
    def staged_update(self, data: dict) -> Iterator[dict]:
        """Yield the next values, publishing them only after the caller succeeds.

        The store lock stays held across the caller's short runtime apply.  The
        candidate JSON is fully written and fsynced first, but ``os.replace`` and
        the in-memory swap happen only when the context exits successfully.
        """
        clean = {k: data[k] for k in _FIELDS if k in (data or {})}
        if (
            "reasoning_provider" in clean
            and clean["reasoning_provider"] not in _REASONING_PROVIDERS
        ):
            raise ValueError("invalid reasoning_provider")
        if (
            "reasoning_egress_consent" in clean
            and not isinstance(clean["reasoning_egress_consent"], bool)
        ):
            raise ValueError("invalid reasoning_egress_consent")
        with self._lock:
            if not clean:
                yield {**DEFAULTS, **self._overrides}
                return

            current = {**DEFAULTS, **self._overrides}
            next_overrides = {**self._overrides, **clean}
            next_provider = next_overrides.get(
                "reasoning_provider", DEFAULTS["reasoning_provider"]
            )
            provider_changed = (
                "reasoning_provider" in clean
                and clean["reasoning_provider"] != current["reasoning_provider"]
            )
            if provider_changed and "reasoning_egress_consent" not in clean:
                next_overrides["reasoning_egress_consent"] = False
            if next_provider == "none" and (
                "reasoning_provider" in clean
                or "reasoning_egress_consent" in clean
                or current["reasoning_egress_consent"] is True
            ):
                next_overrides["reasoning_egress_consent"] = False

            tmp = self._save(next_overrides)
            try:
                yield {**DEFAULTS, **next_overrides}
                os.replace(tmp, self.path)
                self._overrides = next_overrides
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

    def update(
        self,
        data: dict,
        *,
        apply: Callable[[dict], None] | None = None,
    ) -> dict:
        """Apply runtime state, then atomically publish and return new values."""
        with self.staged_update(data) as values:
            if apply is not None:
                apply(values)
            result = values
        return result
