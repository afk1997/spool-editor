"""Watch store — folder / channel / playlist automations (spec §5 Phase 3).

A watch points at a local folder or a channel/playlist URL + a recipe. The reconciler (watcher.py)
detects NEW videos → ingests them (download + auto-transcribe) → once transcribed, runs the recipe
(produce) → ranked clips land in the review queue. NOT auto-published (Phase 4) — an honest gate.

JSON-backed, atomic (mirrors recipes.py / brand_kits.py). User fields are CRUD'd via the API; the
per-watch automation STATE (seen / ingesting / pending / produced / producing) is advanced by the
reconciler via set_state.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

_FIELDS = ("name", "kind", "target", "recipe_id", "enabled")   # user-editable


def _clean(data: dict) -> dict:
    return {k: data[k] for k in _FIELDS if k in data}


class WatchStore:
    """A tiny JSON-backed CRUD store for watches + their reconciler state, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        # One store-wide reentrant lock: API CRUD handlers and the reconciler's
        # set_state all mutate self._watches + write the same file. The per-watch
        # lock in app.py only serializes same-watch reconcile ticks, not CRUD.
        self._lock = threading.RLock()
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
        with self._lock:
            return [dict(w) for w in self._watches]

    def get(self, watch_id: str):
        with self._lock:
            return next((dict(w) for w in self._watches if w.get("id") == watch_id), None)

    def create(self, data: dict) -> dict:
        with self._lock:
            w = {"id": uuid.uuid4().hex[:10], **_clean(data),
                 "enabled": bool(data.get("enabled", True)),
                 "seen": [], "ingesting": {}, "pending": {}, "produced": [], "producing": {}}
            self._watches.append(w)
            self._save()
            return dict(w)

    def update(self, watch_id: str, data: dict):
        with self._lock:
            patch = _clean(data)
            for w in self._watches:
                if w.get("id") == watch_id:
                    # Repointing a watch (changing kind or target to a DIFFERENT value) must reset the
                    # reconciler state: stale seen keys — esp. folder filenames — would otherwise
                    # suppress brand-new items at the new target. Guard on an actual change so a no-op
                    # or name-only PATCH keeps the history. CLI/MCP go through here too, so all clients
                    # benefit (the API doesn't have to special-case it).
                    repointed = (("kind" in patch and patch["kind"] != w.get("kind"))
                                 or ("target" in patch and patch["target"] != w.get("target")))
                    w.update(patch)
                    if repointed:
                        w["seen"], w["ingesting"], w["pending"], w["produced"], w["producing"] = [], {}, {}, [], {}
                    self._save()
                    return dict(w)
            return None

    def set_state(self, watch_id: str, *, seen=None, pending=None, produced=None, producing=None,
                  ingesting=None):
        """Advance the reconciler-managed state (never touches the user fields)."""
        with self._lock:
            for w in self._watches:
                if w.get("id") == watch_id:
                    if seen is not None:
                        w["seen"] = list(seen)
                    if pending is not None:
                        w["pending"] = dict(pending)
                    if produced is not None:
                        w["produced"] = list(produced)
                    if producing is not None:
                        w["producing"] = dict(producing)
                    if ingesting is not None:
                        w["ingesting"] = dict(ingesting)
                    self._save()
                    return dict(w)
            return None

    def delete(self, watch_id: str) -> bool:
        with self._lock:
            before = len(self._watches)
            self._watches = [w for w in self._watches if w.get("id") != watch_id]
            if len(self._watches) != before:
                self._save()
                return True
            return False
