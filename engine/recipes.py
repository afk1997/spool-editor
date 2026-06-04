"""Recipe store — a saved end-to-end pipeline (spec §5 Phase 3 / §3 Recipe).

A recipe captures the REUSABLE decisions of the clip pipeline — the discovery content mode +
how many moments to find, optional glass-box ranking ``weights`` (compose with `moments.rank`),
and the render settings (aspect / reframe mode / caption preset / brand kit / platform preset /
fast) — everything EXCEPT the per-moment start/end. It drives ``render.pipeline``:
find_moments(mode, count) → rank(weights) → top moments → cut→reframe→caption→export with these
settings. The same store powers watch-folder automation (drop a video → auto clips per a recipe
→ a review queue).

JSON-backed under the download dir, atomic writes; the API exposes CRUD (mirrors brand_kits.py).
"""
from __future__ import annotations

import json
import os
import uuid

# Whitelisted recipe fields (anything else in a request body is ignored).
_FIELDS = ("name", "content_mode", "count", "aspect", "reframe_mode", "caption_preset",
           "brand_kit_id", "platform", "fast", "weights")


def _clean(data: dict) -> dict:
    return {k: data[k] for k in _FIELDS if k in data}


class RecipeStore:
    """A tiny JSON-backed CRUD store for recipes, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        self._recipes = self._load()

    def _load(self) -> list:
        try:
            with open(self.path) as f:
                doc = json.load(f)
            items = doc.get("recipes") if isinstance(doc, dict) else None
            return list(items) if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"recipes": self._recipes}, f)
        os.replace(tmp, self.path)

    def list(self) -> list:
        return [dict(r) for r in self._recipes]

    def get(self, recipe_id: str):
        return next((dict(r) for r in self._recipes if r.get("id") == recipe_id), None)

    def create(self, data: dict) -> dict:
        rec = {"id": uuid.uuid4().hex[:10], **_clean(data)}
        self._recipes.append(rec)
        self._save()
        return dict(rec)

    def update(self, recipe_id: str, data: dict):
        for r in self._recipes:
            if r.get("id") == recipe_id:
                r.update(_clean(data))
                self._save()
                return dict(r)
        return None

    def delete(self, recipe_id: str) -> bool:
        before = len(self._recipes)
        self._recipes = [r for r in self._recipes if r.get("id") != recipe_id]
        if len(self._recipes) != before:
            self._save()
            return True
        return False
