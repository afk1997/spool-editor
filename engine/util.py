"""Tiny shared helpers used by app.py and the route blueprints.

Keeping these here (rather than in app.py) avoids the circular-import
trap when blueprint modules need them — blueprint modules can't safely
import from app.py because app.py imports the blueprint registrations.
"""
from __future__ import annotations
import os
import re
import shutil
import unicodedata


_URL_SPLIT_RE = re.compile(r"[\s,]+")


def split_urls(raw: str) -> list[str]:
    """Split a paste-blob into individual URL candidates.

    Splits on commas and any whitespace (including newlines/tabs). Trims
    each candidate, drops empties, deduplicates while preserving the
    first-seen order. The caller is responsible for `is_safe_url` /
    yt-dlp validation — this only normalizes the input shape.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in _URL_SPLIT_RE.split(raw):
        t = token.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def link_or_copy(src: str, dst: str) -> None:
    """Materialize ``src`` at ``dst`` without duplicating bytes when possible.

    Importing a local file into the download dir (the watch-folder ingest) used to
    ``shutil.copy2`` — doubling a multi-GB video on disk. Prefer a **hardlink**: a
    second directory entry for the same inode, zero extra disk, reads identically,
    and the engine only ever reads the source (never mutates it in place), so the
    user's original is safe. Hardlinks are same-filesystem only, so fall back to a
    full copy on any failure — a different volume (``EXDEV``), an existing ``dst``
    (``EEXIST``), too many links, or a filesystem that can't hardlink.
    """
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def sanitize_filename(title: str, ext: str) -> str:
    """Produce a safe download_name. Falls back to a placeholder when empty.

    NFC-normalize, drop control chars and bad filename chars (matches the
    Win/Mac/Linux intersection of disallowed bytes), trim to 150 chars.
    """
    if not title:
        return f"trove-download{ext}"
    s = unicodedata.normalize("NFC", title)
    s = "".join(ch for ch in s if ch.isprintable())
    s = re.sub(r'[\\/:*?"<>|]+', "", s)
    s = s.strip().strip(".")
    s = s[:150].strip()
    return f"{s}{ext}" if s else f"trove-download{ext}"
