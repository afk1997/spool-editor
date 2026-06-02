"""BrandKit store — persisted, reusable looks (caption preset + overrides + watermark /
lower-third / palette / fonts) applied across a project's clips on render (spec §5 Phase 2 /
§3 BrandKit).

JSON-backed under the download dir, atomic writes; the API exposes CRUD. Applying a kit =
captioning + rendering each clip with the kit's preset/overrides/watermark — the same engine
path the manual caption flow uses (the golden rule), so manual and kit-applied never diverge.
"""
from __future__ import annotations

import json
import os
import uuid

# Whitelisted kit fields (anything else in a request body is ignored).
_FIELDS = ("name", "palette", "caption_preset", "caption_overrides", "watermark", "lower_third", "fonts")


def _clean(data: dict) -> dict:
    return {k: data[k] for k in _FIELDS if k in data}


class BrandKitStore:
    """A tiny JSON-backed CRUD store for brand kits, persisted atomically."""

    def __init__(self, path):
        self.path = str(path)
        self._kits = self._load()

    def _load(self) -> list:
        try:
            with open(self.path) as f:
                doc = json.load(f)
            kits = doc.get("kits") if isinstance(doc, dict) else None
            return list(kits) if isinstance(kits, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"kits": self._kits}, f)
        os.replace(tmp, self.path)

    def list(self) -> list:
        return [dict(k) for k in self._kits]

    def get(self, kit_id: str):
        return next((dict(k) for k in self._kits if k.get("id") == kit_id), None)

    def create(self, data: dict) -> dict:
        kit = {"id": uuid.uuid4().hex[:10], **_clean(data)}
        self._kits.append(kit)
        self._save()
        return dict(kit)

    def update(self, kit_id: str, data: dict):
        for k in self._kits:
            if k.get("id") == kit_id:
                k.update(_clean(data))
                self._save()
                return dict(k)
        return None

    def delete(self, kit_id: str) -> bool:
        before = len(self._kits)
        self._kits = [k for k in self._kits if k.get("id") != kit_id]
        if len(self._kits) != before:
            self._save()
            return True
        return False
