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

import json
import os

# The full set of writable keys + their defaults. Defaults mirror the engine's existing
# env/arg defaults (TROVE_CLIP_WORKERS=2, TROVE_MAX_WORKERS=4, exporter fast=True,
# preset=tiktok, reframe aspect=9:16, MCP stdio) so an unconfigured store is a no-op.
DEFAULTS = {
    "fast_default": True,        # export fast vs quality when a render omits `fast` (hot)
    "default_preset": "tiktok",  # platform preset when a render omits `preset` (hot)
    "offline": False,            # block LLM egress (drives SPOOL_OFFLINE; hot)
    "clip_workers": 2,           # render-queue concurrency (applies on restart)
    "max_workers": 4,            # download-queue concurrency (applies on restart)
    "mcp_transport": "stdio",    # MCP server transport (applies on restart)
}

# Anything outside this set in an update body is ignored (defense in depth — the route
# validator is the first gate, the store is the second).
_FIELDS = tuple(DEFAULTS.keys())


class SettingsStore:
    """A tiny JSON-backed singleton-dict settings store, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        self._overrides = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict):
            return {}
        return {k: doc[k] for k in _FIELDS if k in doc}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._overrides, f)
        os.replace(tmp, self.path)

    def get(self) -> dict:
        """Every key: defaults merged with the user's overrides (a fresh copy)."""
        return {**DEFAULTS, **self._overrides}

    def overrides(self) -> dict:
        """Only the keys the user explicitly wrote (a fresh copy)."""
        return dict(self._overrides)

    def update(self, data: dict) -> dict:
        """Merge the whitelisted keys of ``data`` into the overrides, persist, return ``get()``."""
        clean = {k: data[k] for k in _FIELDS if k in (data or {})}
        if clean:
            self._overrides.update(clean)
            self._save()
        return self.get()
