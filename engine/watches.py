"""Watch store — folder / channel / playlist automations (spec §5 Phase 3).

A watch points at a local folder or a channel/playlist URL + a recipe. The reconciler (watcher.py)
detects NEW videos → ingests them (download + auto-transcribe) → once transcribed, runs the recipe
(produce) → ranked clips land in the review queue. NOT auto-published (Phase 4) — an honest gate.

JSON-backed, atomic (mirrors recipes.py / brand_kits.py). User fields are CRUD'd via the API; the
per-watch automation STATE (seen / pending / produced) is advanced by the reconciler via set_state.
"""
from __future__ import annotations

import json
import os
import uuid

_FIELDS = ("name", "kind", "target", "recipe_id", "enabled")   # user-editable
_KINDS = ("folder", "channel", "playlist")


def _clean(data: dict) -> dict:
    return {k: data[k] for k in _FIELDS if k in data}


class WatchStore:
    """A tiny JSON-backed CRUD store for watches + their reconciler state, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        self._watches = self._load()

    def _load(self) -> list:
        try:
            with open(self.path) as f:
                doc = json.load(f)
            items = doc.get("watches") if isinstance(doc, dict) else None
            return list(items) if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"watches": self._watches}, f)
        os.replace(tmp, self.path)

    def list(self) -> list:
        return [dict(w) for w in self._watches]

    def get(self, watch_id: str):
        return next((dict(w) for w in self._watches if w.get("id") == watch_id), None)

    def create(self, data: dict) -> dict:
        w = {"id": uuid.uuid4().hex[:10], **_clean(data),
             "enabled": bool(data.get("enabled", True)),
             "seen": [], "pending": {}, "produced": []}
        self._watches.append(w)
        self._save()
        return dict(w)

    def update(self, watch_id: str, data: dict):
        for w in self._watches:
            if w.get("id") == watch_id:
                w.update(_clean(data))
                self._save()
                return dict(w)
        return None

    def set_state(self, watch_id: str, *, seen=None, pending=None, produced=None):
        """Advance the reconciler-managed state (never touches the user fields)."""
        for w in self._watches:
            if w.get("id") == watch_id:
                if seen is not None:
                    w["seen"] = list(seen)
                if pending is not None:
                    w["pending"] = dict(pending)
                if produced is not None:
                    w["produced"] = list(produced)
                self._save()
                return dict(w)
        return None

    def delete(self, watch_id: str) -> bool:
        before = len(self._watches)
        self._watches = [w for w in self._watches if w.get("id") != watch_id]
        if len(self._watches) != before:
            self._save()
            return True
        return False
