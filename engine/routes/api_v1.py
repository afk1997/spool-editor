"""Blueprint: stable JSON v1 API for the CLI + MCP server.

Wraps the JobManager / TranscribeJobManager / models_store so external
clients (the ``trove`` CLI and the ``trove-mcp`` MCP server) don't have
to scrape HTML or reach into the on-disk JSON. Every route is JSON-in
/ JSON-out and gated by the same ``token_required`` decorator as the
rest of the API surface.

Complex actions (enqueue download / resume / start transcribe)
delegate to closures stashed on ``app.extensions['trove.actions']`` by
``create_app`` — that's the same indirection pattern the transcript
editor blueprint already uses for the JobManager refs, and it lets us
expose new endpoints without re-implementing the work-thunk logic that
``_enqueue_download`` and ``api_job_resume`` already encapsulate.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from threading import Lock, Thread

from flask import (
    Blueprint, Response, current_app, jsonify, request, send_file,
    stream_with_context,
)

import models_store
import transcribe_jobs
import transcript_io
from jobs import JobStatus
from safety import (
    token_or_sig_required, token_required,
    SCOPE_MEDIA, SCOPE_TRANSCRIPT_EXPORT,
)
from util import sanitize_filename

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


# ----- idempotency store ---------------------------------------------
#
# Clients (CLI, MCP, scripts) often retry POST /jobs after a network
# blip. Without an idempotency key they'd silently double-submit and
# the same URL would download twice. Spec mirrors Stripe's
# Idempotency-Key header: caller supplies any opaque string (UUID
# recommended), server returns the *same* job for the same key inside
# the TTL window. In-memory only — self-hosted single-process server,
# so a process restart wipes the cache, which is fine.
_IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_IDEMPOTENCY_CAPACITY    = 512


class _IdempotencyStore:
    def __init__(self, ttl: int = _IDEMPOTENCY_TTL_SECONDS,
                 capacity: int = _IDEMPOTENCY_CAPACITY):
        self._ttl = ttl
        self._cap = capacity
        self._lock = Lock()
        # OrderedDict so we can drop the oldest insert on overflow
        # without scanning the whole map.
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def _sweep_locked(self, now: float) -> None:
        # Cheap eager TTL sweep — bounded by capacity (≤512 entries).
        dead = [k for k, (_, exp) in self._items.items() if exp <= now]
        for k in dead:
            del self._items[k]

    def get(self, key: str) -> str | None:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            entry = self._items.get(key)
            return entry[0] if entry else None

    def put(self, key: str, job_id: str) -> None:
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            self._items[key] = (job_id, now + self._ttl)
            self._items.move_to_end(key)
            while len(self._items) > self._cap:
                self._items.popitem(last=False)

    def claim(self, key: str) -> tuple[str | None, bool]:
        """Single-flight claim. Returns ``(prior_id, claimed)``.

        - If the key already maps to a real job id, returns
          ``(prior_id, False)`` — caller should replay.
        - If the key is unknown, atomically inserts a sentinel
          placeholder and returns ``(None, True)`` — caller owns the
          enqueue and must call ``finalize()`` or ``release()``.
        - If another request is mid-enqueue (placeholder present),
          returns ``(None, False)`` and the caller must surface a
          ``409 in_flight`` so the client retries after the first one
          completes (rather than silently double-enqueuing).
        """
        if not key:
            return None, True  # no idempotency requested → always proceed
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            entry = self._items.get(key)
            if entry is not None:
                jid = entry[0]
                if jid == _IN_FLIGHT:
                    return None, False  # racing peer is still enqueuing
                return jid, False
            # Reserve the slot atomically so concurrent retries see it.
            self._items[key] = (_IN_FLIGHT, now + self._ttl)
            self._items.move_to_end(key)
            while len(self._items) > self._cap:
                self._items.popitem(last=False)
            return None, True

    def release(self, key: str) -> None:
        """Drop a placeholder reservation (failed enqueue path)."""
        if not key:
            return
        with self._lock:
            entry = self._items.get(key)
            if entry is not None and entry[0] == _IN_FLIGHT:
                del self._items[key]

    def delete(self, key: str) -> None:
        """Unconditionally drop ``key`` (even a finalized job-id entry).

        Used to recover the stale-key path: a prior idempotent POST
        succeeded, but the job has since been TTL-swept / dismissed
        from the JobManager. The mapping is dead; let the next caller
        with the same key submit a fresh job."""
        if not key:
            return
        with self._lock:
            self._items.pop(key, None)


_IN_FLIGHT = "__inflight__"


_idempotency_store = _IdempotencyStore()


# Single-flight model install (mirrors the setup-page flag). Lives at
# module scope because it's a per-process singleton, not per-app.
_install_state: dict = {
    "downloading": False, "name": None,
    "received": 0, "total": 0,
    "error": None, "done": False,
}
_install_lock = Lock()


# ----- view helpers ---------------------------------------------------
# These shape the JSON payload returned to the CLI / MCP server. We
# include both raw machine-friendly fields (bytes, seconds, ratios) and
# a ``human`` block with pre-formatted strings ("12.4 MB", "2:31",
# "5.2 MB/s") so a coding agent can surface progress directly to the
# user without re-implementing formatting on every client.

def _human_bytes(n: int | float | None) -> str:
    if not n or n <= 0:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _human_duration(seconds: float | int | None) -> str:
    """Format seconds as ``H:MM:SS`` (or ``M:SS`` under an hour)."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _human_speed(bps: float | int | None) -> str:
    if not bps or bps <= 0:
        return "—"
    return f"{_human_bytes(bps)}/s"


def _download_pct(job) -> int:
    """Best-effort percent complete for a download. Prefers byte ratio,
    falls back to fragment ratio (HLS / DASH), 100 when terminal."""
    try:
        from jobs import JobStatus
        if job.status == JobStatus.DONE:
            return 100
    except Exception:
        pass
    if job.total_bytes:
        return min(100, int(job.downloaded_bytes / job.total_bytes * 100))
    if job.fragment_count:
        return min(100, int(job.fragment_index / job.fragment_count * 100))
    return 0


def _job_view(job) -> dict:
    elapsed = max(0.0, time.monotonic() - job.created_at)
    pct = _download_pct(job)
    out = {
        "id": job.id,
        "url": job.url,
        "title": job.title,
        "status": job.status.value,
        "filename": job.filename,
        "thumbnail": job.thumbnail or None,
        "format_choice": job.format_choice,
        # Raw machine-readable progress
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "speed_bps": job.speed,
        "eta_seconds": job.eta,
        "fragment_index": job.fragment_index,
        "fragment_count": job.fragment_count,
        "progress_pct": pct,
        "elapsed_seconds": round(elapsed, 1),
        "auto_transcribe": job.auto_transcribe,
        "error_category": job.error_category,
        "error_message": job.error_message,
    }
    # Pre-formatted strings for direct display by the agent / CLI.
    out["human"] = {
        "progress": f"{pct}%",
        "downloaded": _human_bytes(job.downloaded_bytes),
        "size": _human_bytes(job.total_bytes),
        "speed": _human_speed(job.speed),
        "eta": _human_duration(job.eta) if job.eta else "—",
        "elapsed": _human_duration(elapsed),
        # One-liner you can drop straight into a chat: e.g.
        # "downloading · 42% · 12.4 MB / 29.7 MB · 5.2 MB/s · ETA 0:03"
        "summary": _summarize_job(job, pct, elapsed),
    }
    return out


def _summarize_job(job, pct: int, elapsed: float) -> str:
    bits = [job.status.value]
    if job.status.value in ("downloading", "queued"):
        bits.append(f"{pct}%")
        if job.total_bytes:
            bits.append(f"{_human_bytes(job.downloaded_bytes)} / "
                        f"{_human_bytes(job.total_bytes)}")
        elif job.downloaded_bytes:
            bits.append(_human_bytes(job.downloaded_bytes))
        if job.speed:
            bits.append(_human_speed(job.speed))
        if job.eta:
            bits.append(f"ETA {_human_duration(job.eta)}")
    elif job.status.value == "done":
        if job.total_bytes:
            bits.append(_human_bytes(job.total_bytes))
        bits.append(f"in {_human_duration(elapsed)}")
    elif job.status.value == "error" and job.error_message:
        bits.append(f"— {job.error_message}")
    return " · ".join(bits)


def _tj_view(tj) -> dict:
    elapsed = max(0.0, time.time() - tj.started_at)
    out = {
        "id": tj.id,
        "parent_job_id": tj.parent_job_id,
        "status": tj.status.value,
        "model_used": tj.model_used,
        "progress_pct": tj.progress_pct,
        "duration_seconds": tj.duration_seconds,
        "language_detected": tj.language_detected,
        "elapsed_seconds": round(elapsed, 1),
        "error_category": tj.error_category,
        "error_message": tj.error_message,
        "diarization_status": tj.diarization_status,
        "diarization_error": tj.diarization_error,
        "speaker_count": tj.speaker_count,
    }
    out["human"] = {
        "progress": f"{tj.progress_pct}%",
        "elapsed": _human_duration(elapsed),
        "audio_duration": _human_duration(tj.duration_seconds)
            if tj.duration_seconds else "—",
        "summary": _summarize_tj(tj, elapsed),
    }
    return out


def _summarize_tj(tj, elapsed: float) -> str:
    bits = [tj.status.value]
    if tj.status.value == "running":
        bits.append(f"{tj.progress_pct}%")
        if tj.duration_seconds:
            bits.append(f"of {_human_duration(tj.duration_seconds)} audio")
        bits.append(f"elapsed {_human_duration(elapsed)}")
        if tj.model_used:
            bits.append(f"model={tj.model_used}")
    elif tj.status.value == "done":
        bits.append(f"in {_human_duration(elapsed)}")
        if tj.language_detected:
            bits.append(f"lang={tj.language_detected}")
    elif tj.status.value == "error" and tj.error_message:
        bits.append(f"— {tj.error_message}")
    return " · ".join(bits)


def _clip_job_view(cj) -> dict:
    elapsed = max(0.0, time.time() - cj.started_at)
    out = {
        "id": cj.id,
        "kind": cj.kind,
        "source_id": cj.source_id,
        "clip_id": cj.clip_id,
        "status": cj.status.value,
        "progress_pct": cj.progress_pct,
        "stage": cj.stage or None,
        "elapsed_seconds": round(elapsed, 1),
        "params": cj.params,
        "result": cj.result,
        "error_category": cj.error_category,
        "error_message": cj.error_message,
    }
    out["human"] = {
        "progress": f"{cj.progress_pct}%",
        "elapsed": _human_duration(elapsed),
        "summary": _summarize_clip(cj, elapsed),
    }
    return out


def _summarize_clip(cj, elapsed: float) -> str:
    bits = [cj.kind, cj.status.value]
    if cj.status.value == "running":
        if cj.stage:
            bits.append(cj.stage)
        bits.append(f"{cj.progress_pct}%")
        bits.append(f"elapsed {_human_duration(elapsed)}")
    elif cj.status.value == "done":
        bits.append(f"in {_human_duration(elapsed)}")
        if cj.kind == "moments":
            bits.append(f"{cj.result.get('count', 0)} candidates")
    elif cj.status.value == "error" and cj.error_message:
        bits.append(f"— {cj.error_message}")
    return " · ".join(bits)


def _jm():
    return current_app.extensions["trove.jobs"]


def _tm():
    return current_app.extensions["trove.transcribe"]


def _cm():
    return current_app.extensions["trove.clips"]


def _cr():
    return current_app.extensions["trove.clip_runner"]


def _bk():
    return current_app.extensions["trove.brand_kits"]


def _rc():
    return current_app.extensions["trove.recipes"]


def _ws():
    return current_app.extensions["trove.watches"]


def _settings():
    return current_app.extensions["trove.settings"]


def _txidx():
    return current_app.extensions.get("trove.transcript_index")


def _actions():
    return current_app.extensions["trove.actions"]


# Mirrors clip.reframe._ASPECTS/_MODES, clip.exporter._PRESETS, clip.captioner._VALID_STYLES.
# Duplicated here for fast request validation — the engine re-validates and would also
# error, but a 400 up front beats a job that fails asynchronously.
_CLIP_ASPECTS = ("9:16", "16:9", "1:1", "4:5")
_CLIP_MODES = ("pan", "split", "center")
_CLIP_PRESETS = ("tiktok", "reels", "shorts", "youtube", "linkedin", "x")
_CAPTION_STYLES = ("opus", "karaoke", "minimal")
_CONTENT_MODES = ("funny", "insightful", "hot-take", "story", "how-to", "q&a")  # clip.moments._MODE_GUIDES
_WATCH_KINDS = ("folder", "channel", "playlist")
_CLIP_ARTIFACTS = {"clip": "clip.mp4", "reframed": "reframed.mp4", "captioned": "captioned.mp4",
                   "preview": "preview.mp4"}


def _is_hex_color(v) -> bool:
    if not isinstance(v, str):
        return False
    h = v.lstrip("#")
    return len(h) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in h)


def _validate_caption_overrides(ov):
    """Clamp/validate S8 fine-styling overrides; return the clean dict, or None → 400."""
    if not isinstance(ov, dict):
        return None
    clean: dict = {}
    for key, lo, hi in (("size", 20, 200), ("outline", 0, 20), ("position", 0, 100),
                        ("words", 1, 12), ("weight", 100, 900)):
        if ov.get(key) is not None:
            try:
                clean[key] = max(lo, min(hi, int(float(ov[key]))))
            except (TypeError, ValueError):
                return None
    for key in ("fill", "highlight"):
        if key in ov:
            v = ov[key]
            if v is None:
                clean[key] = None
            elif _is_hex_color(v):
                clean[key] = v
            else:
                return None
    if "allcaps" in ov:
        clean["allcaps"] = bool(ov["allcaps"])
    if ov.get("font"):
        # The font name lands raw in the ASS Style line (comma-separated fields inside
        # an override-capable format): strip separators, braces, escapes, and controls.
        font = re.sub(r"[{}\\,\x00-\x1f]", "", str(ov["font"]))[:60].strip()
        if font:
            clean["font"] = font
    return clean


def _validate_brand_kit(data, *, require_name: bool):
    """Validate a brand-kit body; return an error response tuple, or None if OK."""
    bad = (jsonify({"error": "bad_kit"}), 400)
    if not isinstance(data, dict):
        return bad
    if require_name and not (isinstance(data.get("name"), str) and data["name"].strip()):
        return bad
    if "name" in data and not isinstance(data["name"], str):
        return bad
    if data.get("caption_preset") is not None and data["caption_preset"] not in _CAPTION_STYLES:
        return bad
    if data.get("caption_overrides") is not None:
        clean_ov = _validate_caption_overrides(data["caption_overrides"])
        if clean_ov is None:
            return bad
        data["caption_overrides"] = clean_ov
    pal = data.get("palette")
    if pal is not None and (not isinstance(pal, list) or not all(_is_hex_color(x) for x in pal)):
        return bad
    for key in ("watermark", "lower_third"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return bad
    return None


# MCP server transports we can actually honor at boot (mcp_server.main → FastMCP.run, whose
# transport is Literal['stdio','sse','streamable-http']). stdio = the desktop-client default;
# sse / streamable-http = headless / self-host.
_MCP_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _validate_settings(data):
    """Validate/clamp a ``PATCH /settings`` body → ``(clean_dict, None)`` on success, or
    ``(None, error_response)`` on a bad value. Unknown keys are dropped (the store also
    whitelists). Concurrency is clamped (like caption overrides); enums + booleans error
    rather than silently coerce (``bool("false")`` is ``True`` — a footgun)."""
    bad = (jsonify({"error": "bad_settings"}), 400)
    if not isinstance(data, dict):
        return None, bad
    clean: dict = {}
    for key, lo, hi in (("clip_workers", 1, 16), ("max_workers", 1, 16)):
        if data.get(key) is not None:
            try:
                clean[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                return None, bad
    for key, allowed in (("default_preset", _CLIP_PRESETS),
                         ("mcp_transport", _MCP_TRANSPORTS)):
        if key in data:
            if data[key] not in allowed:
                return None, bad
            clean[key] = data[key]
    for bkey in ("fast_default", "offline"):
        if bkey in data:
            if not isinstance(data[bkey], bool):
                return None, bad
            clean[bkey] = data[bkey]
    return clean, None


def _download_dir() -> Path:
    return current_app.extensions["trove.download_dir"]


# ----- pagination + filtering helpers --------------------------------

def _parse_page_args() -> tuple[int, int, str, str | None]:
    """Pull ``?limit=&offset=&order=&status=`` off the request, with
    defensive clamping.

    Back-compat: if the caller does NOT supply ``limit``, we return all
    matching items (legacy behavior). Pre-pagination clients that just
    called ``GET /jobs`` must keep getting the full list. A caller that
    explicitly opts in to paging (``?limit=N``) is clamped to 1-500."""
    raw_limit = request.args.get("limit")
    if raw_limit is None:
        limit = _UNLIMITED
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 100
        limit = max(1, min(500, limit))
    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        offset = 0
    offset = max(0, offset)
    order = request.args.get("order", "newest").lower()
    if order not in ("newest", "oldest"):
        order = "newest"
    status = request.args.get("status")
    return limit, offset, order, status


# Sentinel meaning "no caller-supplied limit; return everything after
# offset". Concrete int so downstream slice math doesn't need a branch.
_UNLIMITED = 10 ** 9


def _paginate(items: list, *, status: str | None, status_attr: str,
              order: str, limit: int, offset: int) -> tuple[list, int]:
    """Apply status filter + ordering + slice. Returns (page, total).

    ``status_attr`` is the attribute path on each item (e.g. ``status``
    on Job/TranscribeJob — both expose ``.status.value``).
    """
    if status:
        wanted = {s.strip().lower() for s in status.split(",") if s.strip()}
        items = [
            it for it in items
            if getattr(it, status_attr).value in wanted
        ]
    # JobManager stores in insertion order; reverse for "newest first".
    if order == "newest":
        items = list(reversed(items))
    total = len(items)
    page = items[offset : offset + limit]
    return page, total


# ----- meta -----------------------------------------------------------

@api_v1_bp.get("/health")
def health():
    """Liveness probe. Unauthenticated on purpose so the CLI can detect
    the server is up before prompting the user for a token."""
    return jsonify({"ok": True, "version": "v1"})


@api_v1_bp.get("/capabilities")
def capabilities():
    """Server feature / limit / scope registry.

    Unauthenticated by design: clients (CLI, MCP, browser) need to be
    able to probe ``auth_required`` *before* they have a token to
    decide whether to prompt the user. The body is stable JSON shape;
    new fields are added but never removed without an api-version
    bump. A self-hosting operator can rely on this to wire automation
    that adapts to the running server's actual config rather than
    hard-coding env-var defaults.
    """
    import diarizer  # local import to avoid a top-level cycle on cold start.
    import safety

    auth_required = bool(os.environ.get("TROVE_TOKEN", "").strip())
    # Read every limit off the *live* runtime objects rather than the
    # ``app`` module's import-time globals. Architect P1: module
    # globals are frozen at import time, so a per-process tweak (the
    # JobManager being constructed with a different worker count for a
    # test, or the BATCH_MAX_URLS extension being patched mid-run)
    # would otherwise be invisible to capability consumers.
    rl = current_app.extensions.get("trove.rate_limiter")
    jm = current_app.extensions.get("trove.jobs")
    batch_max = current_app.extensions.get("trove.batch_max", 0)
    return jsonify({
        "api_version":    "v1",
        "schema_version": transcript_io.SCHEMA_VERSION,
        "auth_required":  auth_required,
        "features": {
            # ``available()`` is True iff the flag is on AND the heavy
            # deps are importable — matches what the pipeline will
            # actually try to do at transcribe time.
            "diarization":       diarizer.available(),
            "transcripts":       True,
            "sse_events":        True,
            "idempotency_keys":  True,
            "signed_urls":       auth_required,
            "transcript_chunk":  True,
            "transcript_search": True,
            "clips":             True,
        },
        "formats": {
            "transcript_export": ["txt", "srt", "vtt", "json"],
            "clip_aspects":      list(_CLIP_ASPECTS),
            "reframe_modes":     list(_CLIP_MODES),
            "caption_styles":    list(_CAPTION_STYLES),
            "render_presets":    list(_CLIP_PRESETS),
        },
        "scopes": {
            "media":             safety.SCOPE_MEDIA,
            "transcript_view":   safety.SCOPE_TRANSCRIPT_VIEW,
            "transcript_export": safety.SCOPE_TRANSCRIPT_EXPORT,
        },
        "limits": {
            "rate_limit_per_minute": int(getattr(rl, "rate", 0) or 0),
            "rate_limit_window_sec": int(getattr(rl, "per_seconds", 60) or 60),
            "max_workers":           int(getattr(jm, "max_workers", 0) or 0),
            "job_ttl_seconds":       int(getattr(jm, "ttl_seconds", 0) or 0),
            "batch_max_urls":        int(batch_max or 0),
            "transcript_chunk": {
                "text_default_bytes":    _CHUNK_TEXT_DEFAULT_LIMIT,
                "text_max_bytes":        _CHUNK_TEXT_MAX_LIMIT,
                "json_default_segments": _CHUNK_JSON_DEFAULT_LIMIT,
                "json_max_segments":     _CHUNK_JSON_MAX_LIMIT,
            },
        },
        # Idempotency policy is surfaced so automation can size its
        # retry window and pick a key strategy without reading the
        # source — required for clients that want to safely retry a
        # POST /jobs submission across a network blip.
        "idempotency": {
            "header_name":  "Idempotency-Key",
            "ttl_seconds":  _IDEMPOTENCY_TTL_SECONDS,
            "capacity":     _IDEMPOTENCY_CAPACITY,
        },
        "openapi_url": "/api/v1/openapi.json",
    })


# ----- dependency doctor ---------------------------------------------

@api_v1_bp.get("/doctor")
def doctor():
    """Dependency doctor: machine probe + presence/version of the external
    tools the pipeline needs (ffmpeg, yt-dlp, whisper.cpp, Python) plus the
    ffmpeg encoders available for hardware-aware export.

    Unauthenticated by design — like ``/health`` and ``/capabilities``, the
    onboarding screen calls this before any token exists. Read-only.
    """
    import importlib.metadata as ilm
    import platform
    import shutil
    import subprocess
    import sys
    import machine

    def _entry(version: str | None) -> dict:
        return {"present": version is not None, "version": version, "ok": version is not None}

    def _pkg(dist: str) -> str | None:
        try:
            return ilm.version(dist)
        except Exception:
            return None

    def _ffmpeg_version() -> str | None:
        if shutil.which("ffmpeg") is None:
            return None
        try:
            out = subprocess.run(["ffmpeg", "-version"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        head = (out.stdout or "").splitlines()[:1]
        if not head:
            return None
        parts = head[0].split()
        # "ffmpeg version 7.1.1 Copyright ..." -> "7.1.1"
        return parts[2] if len(parts) >= 3 and parts[0] == "ffmpeg" else head[0].strip()

    def _encoders() -> list[str]:
        if shutil.which("ffmpeg") is None:
            return []
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        # The hardware/software encoders Spool's exporter picks among.
        wanted = ("h264_videotoolbox", "hevc_videotoolbox", "h264_nvenc",
                  "hevc_nvenc", "h264_qsv", "h264_vaapi", "libx264", "libx265")
        text = out.stdout or ""
        return [enc for enc in wanted if enc in text]

    py_ok = sys.version_info[:2] >= (3, 11)
    tools = {
        "python": {"present": True, "version": platform.python_version(), "ok": py_ok},
        "ffmpeg": _entry(_ffmpeg_version()),
        "yt_dlp": _entry(_pkg("yt-dlp")),
        "whisper_cpp": _entry(_pkg("pywhispercpp")),
    }
    required = ("ffmpeg", "yt_dlp", "whisper_cpp")
    ok = py_ok and all(tools[t]["present"] for t in required)
    return jsonify({
        "machine": machine.probe(),
        "tools": tools,
        "encoders": _encoders(),
        "ok": ok,
    })


# ----- jobs -----------------------------------------------------------

@api_v1_bp.get("/jobs")
@token_required
def list_jobs():
    """List download jobs.

    Query params (all optional):
      * ``status``: comma-separated filter (e.g. ``done,error``).
      * ``limit``: 1-500, default 100.
      * ``offset``: pagination cursor (0-based).
      * ``order``: ``newest`` (default) or ``oldest``.

    Returns ``{jobs, total, returned, limit, offset}`` so the caller
    can show "showing 20 of 137" and page without re-counting.
    """
    limit, offset, order, status = _parse_page_args()
    page, total = _paginate(
        _jm().snapshot_jobs(),
        status=status, status_attr="status",
        order=order, limit=limit, offset=offset,
    )
    return jsonify({
        "jobs": [_job_view(j) for j in page],
        "total": total, "returned": len(page),
        "limit": _surface_limit(limit, len(page)), "offset": offset,
    })


def _surface_limit(limit: int, returned: int) -> int:
    """Hide the internal _UNLIMITED sentinel from JSON callers — surface
    the actual page size when the caller asked for "everything"."""
    if limit >= _UNLIMITED:
        return returned
    return limit


@api_v1_bp.get("/jobs/<job_id>")
@token_required
def get_job(job_id):
    job = _jm().get(job_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_job_view(job))


def _submit_one(url: str, *, format_choice: str = "video",
                format_id: str | None = None, title: str = "",
                thumbnail: str = "", auto_transcribe: bool = False,
                subtitles: bool = False, chapters: bool = False, embed: bool = False,
                probe: bool = True,
                ) -> tuple[dict | None, dict | None]:
    """Shared download-submission core used by both the single-URL
    POST /jobs path and the bulk POST /jobs/bulk path.

    Returns ``(job_view, None)`` on success or ``(None, error_dict)`` on
    failure. The caller is responsible for HTTP status mapping (single
    posts surface 4xx; bulk posts return per-URL errors in the array).

    ``probe=True`` (default, single-URL path): call ``run_info`` synchronously
    to resolve the title/thumbnail before enqueuing — one probe is acceptable.
    ``probe=False`` (bulk path): skip the synchronous probe to avoid blocking
    the request thread on up to 50 sequential network calls; the worker
    resolves the real title just before downloading (``resolve_title=True``).
    """
    from safety import is_safe_url
    from runner import run_info

    if not url:
        return None, {"error": "missing_url"}
    if not is_safe_url(url):
        return None, {"error": "unsupported_url"}

    given_title = bool(title)
    if not title:
        if probe:
            info = run_info(url)
            if info.error_category:
                return None, {"error": info.error_category}
            title = info.title or url
            if not thumbnail:
                thumbnail = info.thumbnail or ""
        else:
            # Bulk path: up to 50 sequential seconds-long probes would block
            # the request thread.  Submit with the URL as a placeholder; the
            # worker resolves the real title/thumbnail just before downloading.
            title = url

    try:
        job_id = _actions()["enqueue_download"](
            url, format_choice, format_id, title, thumbnail,
            auto_transcribe=auto_transcribe,
            subtitles=subtitles, chapters=chapters, embed=embed,
            resolve_title=(not probe and not given_title),
        )
    except RuntimeError:
        return None, {"error": "busy"}
    return _job_view(_jm().get(job_id)), None


@api_v1_bp.post("/jobs")
@token_required
def submit_job():
    """Enqueue a new download. Body: ``{url, format?, format_id?, title?, auto_transcribe?}``.

    ``format`` defaults to ``"video"`` (mp4); pass ``"audio"`` for mp3.
    ``auto_transcribe=true`` triggers transcription on success when an
    active model is installed.

    Idempotency: if the request includes an ``Idempotency-Key`` header
    AND the same key was used in the last 24h to create a job that
    still exists, the same job is returned (HTTP 200 + ``X-Idempotent-
    Replay: true`` header) instead of creating a duplicate.
    """
    idem_key = request.headers.get("Idempotency-Key", "").strip()
    # Single-flight claim BEFORE enqueue so two concurrent retries with
    # the same key never both reach the worker.
    prior_id, claimed = _idempotency_store.claim(idem_key)
    if prior_id is not None:
        existing = _jm().get(prior_id)
        if existing is not None:
            resp = jsonify(_job_view(existing))
            resp.headers["X-Idempotent-Replay"] = "true"
            return resp, 200
        # Prior id no longer in the manager (TTL'd out / dismissed).
        # ``release()`` only drops placeholders, so use ``delete()`` to
        # evict the finalized mapping before re-claiming — otherwise
        # the second claim would observe the dead entry and 409 forever.
        _idempotency_store.delete(idem_key)
        prior_id, claimed = _idempotency_store.claim(idem_key)
    if not claimed:
        # Another request is still mid-enqueue for this key. Tell the
        # client to retry instead of silently double-enqueuing.
        return jsonify({"error": "in_flight",
                         "message": "An identical request is still being processed."}), 409

    data = request.get_json(silent=True) or {}
    try:
        view, err = _submit_one(
            (data.get("url") or "").strip(),
            format_choice=data.get("format", "video"),
            format_id=data.get("format_id"),
            title=(data.get("title") or "").strip(),
            thumbnail=(data.get("thumbnail") or "").strip(),
            auto_transcribe=bool(data.get("auto_transcribe")),
            subtitles=bool(data.get("subtitles")),
            chapters=bool(data.get("chapters")),
            embed=bool(data.get("embed")),
        )
    except BaseException:
        _idempotency_store.release(idem_key)
        raise
    if err is not None:
        _idempotency_store.release(idem_key)
        # Map error codes to HTTP status. ``busy`` is a real 503 (queue
        # full); everything else is a 400 (caller-side problem).
        code = 503 if err["error"] == "busy" else 400
        return jsonify(err), code
    if idem_key:
        _idempotency_store.put(idem_key, view["id"])
    return jsonify(view), 201


_BULK_MAX_URLS = 50


@api_v1_bp.post("/jobs/bulk")
@token_required
def submit_bulk():
    """Enqueue many downloads in one round-trip (max 50 URLs).

    Body: ``{urls: [...], format?, format_id?, auto_transcribe?}``.
    Each URL gets its own job. Per-URL errors are returned alongside
    successes — the response body is::

        {
          "submitted": 7,
          "failed": 2,
          "results": [
            {"url": "...", "id": "abc123", "title": "..."},
            {"url": "...", "error": "unsupported_url"},
            ...
          ]
        }

    HTTP 207 Multi-Status when any URL failed; 201 when all succeeded;
    400 when the body itself is malformed.

    Title/thumbnail resolution is deferred to the download worker so that
    up to 50 synchronous ``run_info`` probes do not block the request thread.
    The ``title`` field in each result row is initially the URL itself and
    is updated in the SSE stream once the worker resolves the real title.
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls")
    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "missing_urls"}), 400
    if len(urls) > _BULK_MAX_URLS:
        return jsonify({"error": "too_many_urls", "limit": _BULK_MAX_URLS}), 400

    fmt = data.get("format", "video")
    fmt_id = data.get("format_id")
    auto_t = bool(data.get("auto_transcribe"))

    results = []
    submitted = failed = 0
    for raw in urls:
        u = (raw or "").strip() if isinstance(raw, str) else ""
        view, err = _submit_one(
            u, format_choice=fmt, format_id=fmt_id,
            auto_transcribe=auto_t, probe=False,
        )
        if view is not None:
            results.append({"url": u, "id": view["id"], "title": view["title"]})
            submitted += 1
        else:
            results.append({"url": u, **err})
            failed += 1
    status_code = 201 if failed == 0 else 207
    return jsonify({
        "submitted": submitted, "failed": failed, "results": results,
    }), status_code


@api_v1_bp.post("/jobs/<job_id>/pause")
@token_required
def pause_job(job_id):
    if not _jm().pause(job_id):
        return jsonify({"error": "not_found_or_terminal"}), 404
    return jsonify(_job_view(_jm().get(job_id)))


@api_v1_bp.post("/jobs/<job_id>/resume")
@token_required
def resume_job(job_id):
    job = _jm().get(job_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404
    if not _actions()["resume_job"](job_id):
        return jsonify({"error": "not_resumable"}), 409
    return jsonify(_job_view(_jm().get(job_id)))


@api_v1_bp.post("/jobs/<job_id>/cancel")
@token_required
def cancel_job(job_id):
    if not _jm().cancel(job_id):
        return jsonify({"error": "not_found"}), 404
    job = _jm().get(job_id)
    return jsonify(_job_view(job)) if job else ("", 204)


@api_v1_bp.post("/jobs/<job_id>/dismiss")
@token_required
def dismiss_job(job_id):
    if not _jm().dismiss(job_id):
        return jsonify({"error": "not_found_or_active"}), 404
    return ("", 204)


@api_v1_bp.get("/jobs/<job_id>/file")
@token_or_sig_required(SCOPE_MEDIA, kwarg="job_id")
def get_job_file(job_id):
    job = _jm().get(job_id)
    if job is None or job.status != JobStatus.DONE or not job.file_path:
        return jsonify({"error": "not_ready"}), 404
    return send_file(
        job.file_path, as_attachment=True,
        download_name=job.filename or "download",
    )


# ----- transcripts ----------------------------------------------------

@api_v1_bp.get("/transcripts")
@token_required
def list_transcripts():
    """Same pagination + filtering surface as :func:`list_jobs`."""
    limit, offset, order, status = _parse_page_args()
    page, total = _paginate(
        _tm().snapshot_jobs(),
        status=status, status_attr="status",
        order=order, limit=limit, offset=offset,
    )
    return jsonify({
        "transcripts": [_tj_view(t) for t in page],
        "total": total, "returned": len(page),
        "limit": _surface_limit(limit, len(page)), "offset": offset,
    })


@api_v1_bp.get("/transcripts/search")
@token_required
def search_transcripts():
    """Substring search across all completed transcripts.

    Query: ``?q=<phrase>&limit=&context=``.
    Returns matches with a contextual snippet (default ±60 chars) and
    the timing range of the words that contained the hit, so the
    caller can deep-link into the editor at the right point.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing_query"}), 400
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(200, limit))
    try:
        ctx = int(request.args.get("context", "60"))
    except ValueError:
        ctx = 60
    ctx = max(0, min(400, ctx))

    needle = q.lower()
    matches = []
    # FTS5 (trigram) candidate filter — purely additive. `candidates` is a *superset* of the
    # transcripts that may contain `needle` (or None = "can't help, scan everything"). We only
    # skip a transcript that is BOTH indexed AND not a candidate (so it definitely can't match);
    # anything unindexed is always scanned, so a missing/lagging index can never drop a hit.
    idx = _txidx()
    candidates = idx.search_candidates(needle) if idx is not None else None
    indexed = idx.indexed_ids() if idx is not None else set()
    for tj in _tm().snapshot_jobs():
        if tj.status != transcribe_jobs.TranscribeStatus.DONE:
            continue
        if candidates is not None and tj.id in indexed and tj.id not in candidates:
            continue
        parent = _jm().get(tj.parent_job_id)
        if parent is None or not parent.file_path:
            continue
        words_path = os.path.splitext(parent.file_path)[0] + ".words.json"
        if not os.path.exists(words_path):
            continue
        try:
            data = transcript_io.load(words_path)
        except Exception:
            continue
        words = data.get("words") or []
        # Flat text + per-char → word-position map (shared builder), so a string-match offset
        # converts back to the word range and thence to start/end timestamps for deep-linking.
        flat, char_to_widx = transcript_io.flat_text(words, data.get("segments") or [])
        # Self-healing backfill: index any DONE transcript we had to scan because it wasn't yet
        # indexed (pre-existing library, or completed before this feature) — so the next search
        # can skip it. Best-effort; the store swallows its own errors.
        if idx is not None and tj.id not in indexed:
            idx.index(tj.id, flat)
        if not flat:
            continue
        flat_lower = flat.lower()
        start = 0
        while True:
            hit = flat_lower.find(needle, start)
            if hit == -1:
                break
            end = hit + len(needle)
            w_start = char_to_widx[hit] if hit < len(char_to_widx) else 0
            w_end = char_to_widx[end - 1] if end - 1 < len(char_to_widx) else w_start
            snippet_lo = max(0, hit - ctx)
            snippet_hi = min(len(flat), end + ctx)
            snippet = flat[snippet_lo:snippet_hi]
            matches.append({
                "transcript_id": tj.id,
                "parent_job_id": tj.parent_job_id,
                "title": parent.title,
                "snippet": ("…" if snippet_lo > 0 else "") + snippet
                           + ("…" if snippet_hi < len(flat) else ""),
                "start_seconds": float(words[w_start].get("start") or 0.0),
                "end_seconds":   float(words[w_end].get("end") or 0.0),
                "match_offset": hit,
            })
            if len(matches) >= limit:
                break
            start = end
        if len(matches) >= limit:
            break
    return jsonify({"query": q, "matches": matches, "returned": len(matches)})


@api_v1_bp.get("/transcripts/<tid>")
@token_required
def get_transcript(tid):
    tj = _tm().get(tid)
    if tj is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_tj_view(tj))


@api_v1_bp.post("/jobs/<parent_job_id>/transcribe")
@token_required
def start_transcribe(parent_job_id):
    parent = _jm().get(parent_job_id)
    if parent is None or parent.status != JobStatus.DONE or not parent.file_path:
        return jsonify({"error": "parent_not_done"}), 404
    if models_store.get_active_path() is None:
        return jsonify({"error": "no_active_model"}), 409

    # Idempotent: return the existing in-flight transcribe instead of
    # spawning a duplicate. Same guard the HTML start endpoint uses.
    existing = _tm().get_by_parent(parent_job_id)
    if existing and existing.status in (
        transcribe_jobs.TranscribeStatus.QUEUED,
        transcribe_jobs.TranscribeStatus.RUNNING,
    ):
        return jsonify(_tj_view(existing)), 200

    tjid = _actions()["start_transcribe"](parent_job_id)
    if tjid is None:
        return jsonify({"error": "submit_failed"}), 500
    return jsonify(_tj_view(_tm().get(tjid))), 201


@api_v1_bp.post("/transcripts/<tid>/cancel")
@token_required
def cancel_transcript(tid):
    if not _tm().cancel(tid):
        return jsonify({"error": "not_found_or_terminal"}), 404
    return jsonify(_tj_view(_tm().get(tid)))


@api_v1_bp.post("/transcripts/<tid>/dismiss")
@token_required
def dismiss_transcript(tid):
    if not _tm().dismiss(tid):
        return jsonify({"error": "not_found_or_active"}), 404
    return ("", 204)


# Defaults / caps for the chunked-read endpoint. Picked so a single MCP
# call comfortably fits in a small-context tool window: ~4 KB of text
# per page, or ~50 segments of words+timing JSON. Hard caps prevent a
# malicious client from forcing the server to materialize the whole
# transcript in one shot just by passing a huge ``?limit=``.
_CHUNK_TEXT_DEFAULT_LIMIT = 4000
_CHUNK_TEXT_MAX_LIMIT     = 64000
_CHUNK_JSON_DEFAULT_LIMIT = 50
_CHUNK_JSON_MAX_LIMIT     = 500


def _resolve_transcript_artifact(tid, fmt):
    """Shared lookup for the export + chunk endpoints.

    Returns ``(path, parent, error_response)``. On error one of the
    first two is None and ``error_response`` is the ready-to-return
    ``(json_body, status)`` tuple. Keeps the error-shape contract
    identical between ``export.<fmt>`` and ``chunk`` so MCP/CLI
    callers can swap one for the other without retraining their
    error parsers.
    """
    if fmt not in {"txt", "srt", "vtt", "json"}:
        return None, None, (jsonify({"error": "invalid_format"}), 404)
    tj = _tm().get(tid)
    if tj is None or tj.status != transcribe_jobs.TranscribeStatus.DONE:
        return None, None, (jsonify({"error": "transcript_not_found_or_not_done"}), 404)
    parent = _jm().get(tj.parent_job_id)
    if parent is None or not parent.file_path:
        return None, None, (jsonify({"error": "parent_job_missing"}), 404)
    base = os.path.splitext(parent.file_path)[0]
    suffix = ".words.json" if fmt == "json" else ("." + fmt)
    path = base + suffix
    if not os.path.exists(path):
        return None, None, (jsonify({"error": "artifact_not_on_disk"}), 404)
    return path, parent, None


@api_v1_bp.post("/transcripts/<tid>/words/<int:idx>")
@token_required
def edit_transcript_word(tid, idx):
    """Edit one transcript word in place — trove's transcript-editor behavior, now over the
    API. ``op`` ∈ set_text / delete / insert_after / merge_next; ``idx`` is the word's stable
    ``idx`` field. Persists words.json + regenerates .srt/.vtt/.txt so a re-burn / ripple cut
    picks up the edit. Returns the (possibly new) word dict the UI re-renders."""
    path, parent, err = _resolve_transcript_artifact(tid, "json")
    if err:
        return err
    try:
        data = transcript_io.load(path)
    except (OSError, ValueError) as e:
        return jsonify({"error": "transcript_unreadable", "detail": str(e)}), 500
    words = data.get("words") or []
    pos = next((i for i, w in enumerate(words) if w.get("idx") == idx), None)
    if pos is None:
        return jsonify({"error": "word_not_found"}), 404
    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "")
    kw = {"w": body["w"]} if isinstance(body.get("w"), str) else {}
    try:
        word = transcript_io.apply_word_op(data, pos, op, **kw)
    except transcript_io.WordOpError as e:
        return jsonify({"error": "bad_word_op", "detail": str(e)}), 400
    transcript_io.save(path, data)
    transcript_io.regenerate_artifacts(data, os.path.splitext(parent.file_path)[0])
    # Re-index the edited transcript so the FTS5 search accelerator can't go stale: an insert
    # could otherwise leave a now-matching transcript un-indexed → wrongly filtered out.
    idx = _txidx()
    if idx is not None:
        flat, _ = transcript_io.flat_text(data.get("words") or [], data.get("segments") or [])
        idx.index(tid, flat)
    return jsonify({"tid": tid, "word": word})


@api_v1_bp.get("/transcripts/<tid>/chunk")
@token_or_sig_required(SCOPE_TRANSCRIPT_EXPORT, kwarg="tid")
def chunk_transcript(tid):
    """Paginated read of a finished transcript — designed for MCP / LLM
    callers whose context window can't hold a full hour-long transcript.

    Query string:
      - ``format`` — ``txt|srt|vtt|json`` (default ``txt``)
      - ``offset`` — start position. For text formats this is a *byte*
        offset into the on-disk rendered artifact (matches the bytes
        ``/export.<fmt>`` would serve); for ``json`` it's a *segment*
        index into ``data["segments"]``.
      - ``limit``  — max units to return. Defaults / caps differ by
        format (see module-level ``_CHUNK_*`` constants). ``0`` is
        treated as "use the server default" rather than "return zero
        units" — the latter would deadlock a naive paginator.

    Always returns a JSON envelope with the same top-level keys
    (``format / offset / limit / returned / total / has_more``) plus
    one of ``content`` (text formats) or ``segments`` + ``words``
    (json). ``content`` is a *substring*, not a clean line break, so
    the caller can stitch chunks back together verbatim.
    """
    fmt = (request.args.get("format") or "txt").lower()
    path, _parent, err = _resolve_transcript_artifact(tid, fmt)
    if err is not None:
        return err

    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return jsonify({"error": "invalid_offset"}), 400
    raw_limit = request.args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else None
    except ValueError:
        return jsonify({"error": "invalid_limit"}), 400
    if limit is not None and limit < 0:
        return jsonify({"error": "invalid_limit"}), 400
    # ``limit=0`` is treated as "server default" rather than "return
    # zero units". Returning zero with ``has_more=True`` would put a
    # naive client into an infinite pagination loop (offset never
    # advances), so we collapse 0 to None and let the per-format
    # default kick in below.
    if limit == 0:
        limit = None

    if fmt == "json":
        # Segment-range slicing. Loading the whole .words.json is fine
        # — even an hour-long transcript is well under a megabyte and
        # transcript_io.load() returns the canonical in-memory v2 dict
        # regardless of what's on disk; the startup migrate_all() sweep
        # is what persists cold v1 files. This GET handler stays read-only.
        try:
            data = transcript_io.load(path)
        except (OSError, ValueError) as e:
            return jsonify({"error": "transcript_unreadable", "detail": str(e)}), 500
        segments = list(data.get("segments") or [])
        all_words = list(data.get("words") or [])
        total = len(segments)
        if limit is None:
            limit = _CHUNK_JSON_DEFAULT_LIMIT
        limit = min(limit, _CHUNK_JSON_MAX_LIMIT)
        slice_segs = segments[offset : offset + limit]
        # Filter words to only those referenced by the returned
        # segments — this is the whole point of the endpoint, otherwise
        # a 1-segment chunk would still drag every word along with it.
        wanted: set[int] = set()
        for seg in slice_segs:
            for wi in seg.get("word_idxs") or []:
                if isinstance(wi, int):
                    wanted.add(wi)
        words_subset = [w for w in all_words
                        if isinstance(w.get("idx"), int) and w["idx"] in wanted]
        return jsonify({
            "format":         "json",
            "offset":         offset,
            "limit":          limit,
            "returned":       len(slice_segs),
            "total":          total,
            "has_more":       (offset + len(slice_segs)) < total,
            "schema_version": data.get("schema_version"),
            "language":       data.get("language"),
            "duration":       data.get("duration"),
            "segments":       slice_segs,
            "words":          words_subset,
            "total_words":    len(all_words),
        })

    # txt / srt / vtt — *byte*-range slicing of the on-disk rendered
    # body. Read in binary so the wire bytes match what the
    # /export.<fmt> endpoint serves verbatim (no CRLF→LF translation,
    # no BOM eating). offset / total / returned are all byte counts so
    # a client looping on ``offset += returned`` until ``has_more`` is
    # false reproduces the export bytes exactly. The trailing chunk's
    # bytes are decoded as utf-8 for JSON transport with
    # ``errors="replace"`` so a split mid-codepoint never raises —
    # callers that need true byte-fidelity should hit /export.<fmt>.
    with open(path, "rb") as f:
        body = f.read()
    total = len(body)
    if limit is None:
        limit = _CHUNK_TEXT_DEFAULT_LIMIT
    limit = min(limit, _CHUNK_TEXT_MAX_LIMIT)
    end = min(offset + limit, total)
    raw_chunk = body[offset:end] if offset < total else b""
    chunk = raw_chunk.decode("utf-8", errors="replace")
    return jsonify({
        "format":   fmt,
        "offset":   offset,
        "limit":    limit,
        "returned": len(raw_chunk),  # bytes, matches offset semantics
        "total":    total,           # bytes
        "has_more": end < total,
        "content":  chunk,
    })


@api_v1_bp.get("/transcripts/<tid>/export.<fmt>")
@token_or_sig_required(SCOPE_TRANSCRIPT_EXPORT, kwarg="tid")
def export_transcript(tid, fmt):
    """Stream the saved export artifact for a finished transcript.

    ``json`` returns the raw v2 ``.words.json`` (the editor's source
    of truth — useful for programmatic post-processing). ``txt|srt|
    vtt`` return the rendered artifacts.
    """
    # Single-sourced lookup so /chunk and /export.<fmt> can never
    # drift on auth scope, error codes, or artifact path resolution.
    path, parent, err = _resolve_transcript_artifact(tid, fmt)
    if err is not None:
        return err
    mime = {
        "txt": "text/plain; charset=utf-8",
        "srt": "application/x-subrip",
        "vtt": "text/vtt; charset=utf-8",
        "json": "application/json",
    }[fmt]
    name = sanitize_filename(parent.title or "transcript", "." + fmt)
    return send_file(path, mimetype=mime, as_attachment=True, download_name=name)


# ----- models ---------------------------------------------------------

@api_v1_bp.get("/models")
@token_required
def list_models():
    active = models_store.get_active()
    installed = set(models_store.list_installed())
    out = []
    for name, meta in models_store.KNOWN_MODELS.items():
        out.append({
            "name": name,
            "label": meta["label"],
            "size_bytes": meta["size_bytes"],
            "stars": meta["stars"],
            "multilingual": meta["multilingual"],
            "is_active": name == active,
            "is_installed": name in installed,
        })
    with _install_lock:
        progress = dict(_install_state)
    return jsonify({"active": active, "models": out, "install_progress": progress})


@api_v1_bp.post("/models/<name>/use")
@token_required
def use_model(name):
    if name not in models_store.KNOWN_MODELS:
        return jsonify({"error": "unknown_model"}), 400
    try:
        models_store.set_active(name)
    except FileNotFoundError:
        return jsonify({"error": "not_installed"}), 409
    return jsonify({"active": name})


@api_v1_bp.post("/models/<name>/remove")
@token_required
def remove_model(name):
    if name not in models_store.KNOWN_MODELS:
        return jsonify({"error": "unknown_model"}), 400
    models_store.remove(name)
    return ("", 204)


@api_v1_bp.post("/models/<name>/install")
@token_required
def install_model(name):
    if name not in models_store.KNOWN_MODELS:
        return jsonify({"error": "unknown_model"}), 400
    with _install_lock:
        if _install_state["downloading"]:
            return jsonify({"error": "busy", "name": _install_state["name"]}), 409
        _install_state.update({
            "downloading": True, "name": name,
            "received": 0, "total": models_store.KNOWN_MODELS[name]["size_bytes"],
            "error": None, "done": False,
        })

    def _progress(rec, total):
        with _install_lock:
            _install_state["received"] = rec
            _install_state["total"] = total

    def _worker():
        try:
            models_store.download(name, progress_cb=_progress, verify=True)
            models_store.set_active(name)
            with _install_lock:
                _install_state["downloading"] = False
                _install_state["done"] = True
        except Exception as e:
            with _install_lock:
                _install_state["downloading"] = False
                _install_state["error"] = type(e).__name__ + ": " + str(e)

    Thread(target=_worker, daemon=True, name="trove-v1-model-install").start()
    return jsonify({"name": name, "downloading": True}), 202


@api_v1_bp.get("/models/install-progress")
@token_required
def install_progress():
    with _install_lock:
        return jsonify(dict(_install_state))


# ----- storage / disk usage ------------------------------------------

def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@api_v1_bp.get("/storage")
@token_required
def storage_info():
    """Disk-usage report for the download directory.

    Walks the on-disk tree (NOT the JobManager) so orphan files left
    behind by crashes are still counted — this is the same number the
    user would get from ``du -sb downloads/``. Per-job breakdown is
    derived by matching files to job IDs (file basename starts with
    the job id) so the user can see which downloads are taking space.

    Response::

        {
          "download_dir": "/abs/path/downloads",
          "total_bytes": 12345,
          "file_count": 7,
          "by_job": [
            {"id": "abc", "title": "...", "bytes": 1234,
             "files": [{"path": "...", "bytes": 1234}]},
            ...
          ],
          "orphan_bytes": 0,
          "orphan_files": []
        }
    """
    root = _download_dir()
    by_job: dict[str, dict] = {}
    orphan_files: list[dict] = []
    orphan_bytes = 0
    total = 0
    file_count = 0

    # Index known job ids once so file-to-job attribution is O(N+M).
    jobs_by_id = {j.id: j for j in _jm().snapshot_jobs()}

    if root.exists():
        for entry in os.scandir(root):
            if not entry.is_file():
                continue
            # Internal bookkeeping files we never want to surface (job stores, the brand-kit
            # and settings stores, and the FTS5 search index + its sqlite -wal/-shm sidecars).
            if entry.name in ("jobs.json", "transcribe_jobs.json", "clip_jobs.json",
                              "brand_kits.json", "settings.json", "recipes.json", "watches.json") \
                    or entry.name.startswith("transcript_index.sqlite3"):
                continue
            size = _file_size(entry.path)
            total += size
            file_count += 1
            # Job ids are the prefix of the filename up to the first
            # `.` (e.g. ``abc123.mp4`` or ``abc123.words.json``).
            stem = entry.name.split(".", 1)[0]
            if stem in jobs_by_id:
                slot = by_job.setdefault(stem, {
                    "id": stem,
                    "title": jobs_by_id[stem].title,
                    "bytes": 0,
                    "files": [],
                })
                slot["bytes"] += size
                slot["files"].append({"name": entry.name, "bytes": size})
            else:
                orphan_files.append({"name": entry.name, "bytes": size})
                orphan_bytes += size

    # Sort biggest first so the report is immediately useful.
    by_job_list = sorted(by_job.values(), key=lambda d: d["bytes"], reverse=True)
    orphan_files.sort(key=lambda d: d["bytes"], reverse=True)

    return jsonify({
        "download_dir": str(root),
        "total_bytes": total,
        "file_count": file_count,
        "by_job": by_job_list,
        "orphan_bytes": orphan_bytes,
        "orphan_files": orphan_files,
    })


# ----- clips: the render queue + clip operations ----------------------
#
# trove's job machinery with clip kinds (spec §4). ``/sources/<id>/*`` create
# clips/renders; ``/clips/<clip_id>/*`` operate on a produced clip; ``/clip-jobs/*``
# is the render queue (list/get/cancel/dismiss). Each POST submits a ClipJob and
# returns its view immediately — poll ``/clip-jobs/<id>`` (or the SSE stream) for
# progress + result. Manual mode (UI) and agent mode (MCP) hit the same endpoints →
# same engine → same queue (the golden rule).

def _clamp_count(raw, default=5):
    try:
        return max(1, min(25, int(raw)))
    except (TypeError, ValueError):
        return default


def _parse_range(data):
    """``(start, end)`` floats from a body, or ``None`` if absent/invalid."""
    try:
        start, end = float(data["start"]), float(data["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _source_or_error(source_id):
    """``(source_job, words_path, None)`` when the source is downloaded, else
    ``(None, None, (response, code))``."""
    src = _jm().get(source_id)
    if src is None or src.status != JobStatus.DONE or not src.file_path:
        return None, None, (jsonify({"error": "source_not_ready"}), 404)
    _, words_path = _cr().source_paths(source_id)
    return src, words_path, None


@api_v1_bp.post("/sources/<source_id>/moments")
@token_required
def find_clip_moments(source_id):
    """Find clip-worthy moments over the source transcript (discover.find_moments)."""
    _, words_path, err = _source_or_error(source_id)
    if err:
        return err
    if not words_path or not os.path.exists(words_path):
        return jsonify({"error": "no_transcript"}), 409
    data = request.get_json(silent=True) or {}
    params = {"mode": str(data.get("mode") or "funny"), "count": _clamp_count(data.get("count"))}
    win = data.get("window")
    if isinstance(win, (list, tuple)) and len(win) == 2:
        try:
            params["window"] = [float(win[0]), float(win[1])]
        except (TypeError, ValueError):
            return jsonify({"error": "bad_window"}), 400
    jid = _cm().submit(kind="moments", source_id=source_id, params=params,
                       target=_cr().find_moments_target(source_id=source_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


def _opt_window(req):
    """Parse optional ``?start=&end=`` floats from a request; ``(None, None)`` if absent/invalid."""
    s, e = req.args.get("start"), req.args.get("end")
    if s is None or e is None:
        return None, None
    try:
        return float(s), float(e)
    except (TypeError, ValueError):
        return None, None


@api_v1_bp.get("/sources/<source_id>/energy")
@token_required
def source_energy(source_id):
    """Normalized 0..1 loudness envelope across the source — the audio-energy waveform (peaks ≈
    louder / higher-energy moments). One ffmpeg pass, cached next to the media so repeats are
    instant. Optional ``?start=&end=`` windows it to a clip (the editor's Energy lane).
    ``{"bars": [...], "buckets": n}``; ``bars`` is empty when the source has no audio."""
    from clip import signals
    src, _, err = _source_or_error(source_id)
    if err:
        return err
    try:
        buckets = max(8, min(480, int(request.args.get("buckets", 96))))
    except (TypeError, ValueError):
        buckets = 96
    start, end = _opt_window(request)
    bars = signals.energy_envelope(src.file_path, buckets=buckets, start=start, end=end)
    return jsonify({"bars": bars or [], "buckets": buckets})


@api_v1_bp.get("/sources/<source_id>/scenes")
@token_required
def source_scenes(source_id):
    """Scene-cut timestamps (source seconds) for the editor timeline's Scenes lane. Requires
    ``?start=&end=`` (a clip window — whole-source detection is too slow). ``{"cuts": [...]}``."""
    from clip import signals
    src, _, err = _source_or_error(source_id)
    if err:
        return err
    start, end = _opt_window(request)
    if start is None or end is None or end <= start:
        return jsonify({"cuts": []})
    return jsonify({"cuts": signals.scene_cuts(src.file_path, start, end) or []})


@api_v1_bp.get("/sources/<source_id>/filmstrip")
@token_required
def source_filmstrip(source_id):
    """A horizontal filmstrip (evenly-spaced thumbnails) across ``?start=&end=`` as one
    ``data:image/jpeg`` URI — the editor timeline's Video lane. ``{"strip": <uri|null>, "frames": n}``."""
    from clip import signals
    src, _, err = _source_or_error(source_id)
    if err:
        return err
    start, end = _opt_window(request)
    if start is None or end is None or end <= start:
        return jsonify({"strip": None, "frames": 0})
    try:
        frames = max(2, min(40, int(request.args.get("frames", 12))))
    except (TypeError, ValueError):
        frames = 12
    return jsonify({"strip": signals.filmstrip(src.file_path, start, end, frames=frames), "frames": frames})


@api_v1_bp.post("/sources/<source_id>/rank")
@token_required
def rank_clip_candidates(source_id):
    """Re-rank candidates with the glass-box opportunity score (discover.rank).

    Stateless: the client posts the candidates it holds (each carrying the ``features``
    that ``signals.annotate`` attached) + optional factor ``weights``; the engine re-scores
    **on** those signals and returns them sorted best-first. The score is a transparent
    weighted sum of named factors (hook / self-contained / arc / energy / length-fit), so
    the studio mirrors it client-side for instant slider feedback while MCP/CLI/agent reach
    the same path here (spec §4 discover.rank, §6 glass-box rule)."""
    src, _, err = _source_or_error(source_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cands = data.get("candidates")
    if not isinstance(cands, list) or not cands:
        return jsonify({"error": "no_candidates"}), 400
    # Each element must be a dict — rank() reaches into ``features``/``signals`` and a non-dict
    # element would raise an AttributeError (an unhandled 500). Reject up front with a clean 400.
    if not all(isinstance(c, dict) for c in cands):
        return jsonify({"error": "bad_candidates"}), 400
    from clip import moments as clip_moments
    ranked = clip_moments.rank(cands, weights=_sanitize_rank_weights(data.get("weights")))
    for cand in ranked:
        cand.setdefault("source_id", source_id)
    return jsonify({"candidates": ranked, "count": len(ranked),
                    "weights": ranked[0]["weights"]}), 200


def _sanitize_rank_weights(raw):
    """Coerce a client ``weights`` payload to ``{factor: float}`` (drop unknown/non-numeric
    keys). ``None`` → the ranker uses its default weights. Guards against a bad value 500ing."""
    if not isinstance(raw, dict):
        return None
    from clip import moments as clip_moments
    out = {}
    for k in clip_moments.RANK_FACTORS:
        if k in raw:
            try:
                out[k] = float(raw[k])
            except (TypeError, ValueError):
                continue
    return out or None


@api_v1_bp.post("/sources/<source_id>/produce")
@token_required
def produce_clips(source_id):
    """Apply a recipe to a source end-to-end → the review queue (automated discover.* +
    render.pipeline). find_moments(recipe.content_mode, count) → glass-box rank(recipe.weights)
    → the top ``count`` moments each render with the recipe's aspect/reframe/caption/platform,
    tagged auto + recipe_id. The watch-folder runs this per new video. The clips are NOT
    published (Phase 4) — they land for review (the honest gate)."""
    _, words_path, err = _source_or_error(source_id)
    if err:
        return err
    if not words_path or not os.path.exists(words_path):
        return jsonify({"error": "no_transcript"}), 409
    data = request.get_json(silent=True) or {}
    rid = data.get("recipe_id")
    if rid is not None:
        recipe = _rc().get(str(rid))
        if recipe is None:
            return jsonify({"error": "recipe_not_found"}), 404
    else:   # an inline recipe (the watch passes a stored recipe_id; ad-hoc callers can inline one)
        recipe = {k: data[k] for k in ("content_mode", "count", "aspect", "reframe_mode",
                                       "caption_preset", "platform", "fast", "weights", "brand_kit_id")
                  if k in data}
        # Validate up front like /render and /recipes — a bad enum (aspect/reframe/caption/platform)
        # would otherwise return 201 then fail ASYNC in the render. Stored recipes are already
        # validated at create/update, so only the inline branch needs this.
        verr = _validate_recipe(recipe, require_name=False)
        if verr:
            return verr
    jid = _cm().submit(kind="produce", source_id=source_id, params={"recipe_id": rid},
                       target=_cr().produce_target(source_id=source_id, recipe=recipe))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.post("/sources/<source_id>/cut")
@token_required
def cut_clip(source_id):
    """Cut a clip [start, end] from the source (clip.cut). The result carries the
    new ``clip_id`` to drive subsequent reframe/caption/render calls."""
    src, _, err = _source_or_error(source_id)
    if err:
        return err
    rng = _parse_range(request.get_json(silent=True) or {})
    if rng is None:
        return jsonify({"error": "bad_range"}), 400
    start, end = rng
    clip_id = uuid.uuid4().hex[:10]
    params = {"start": start, "end": end}
    jid = _cm().submit(kind="cut", source_id=source_id, clip_id=clip_id, params=params,
                       target=_cr().cut_target(source_id=source_id, clip_id=clip_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.post("/clips/<clip_id>/reframe")
@token_required
def reframe_clip(clip_id):
    """Reframe a clip to a target aspect via the diar⊕ROI speaker pan (reframe.render)."""
    if _cr().load_clip_meta(clip_id) is None:
        return jsonify({"error": "clip_not_found"}), 404
    data = request.get_json(silent=True) or {}
    aspect = str(data.get("aspect") or "9:16")
    mode = str(data.get("mode") or "pan")
    if aspect not in _CLIP_ASPECTS or mode not in _CLIP_MODES:
        return jsonify({"error": "bad_params"}), 400
    params = {"aspect": aspect, "mode": mode}
    if data.get("preview") is not None:
        params["preview"] = bool(data["preview"])  # editor what-you-see preview: fast low-res to preview.mp4
    if isinstance(data.get("rois"), dict):
        params["rois"] = data["rois"]
    # Phase-2 S7 tuning knobs: numeric, clamped to safe ranges.
    for key, lo, hi in (("min_dwell", 0.0, 10.0), ("smoothing", 1.0, 121.0), ("crop_margin", 0.0, 0.5)):
        if data.get(key) is not None:
            try:
                params[key] = max(lo, min(hi, float(data[key])))
            except (TypeError, ValueError):
                return jsonify({"error": "bad_params"}), 400
    # Edited speaker track (drag/flip the segments in S7) → render verbatim.
    if data.get("segments") is not None:
        segs = data["segments"]
        if not isinstance(segs, list):
            return jsonify({"error": "bad_params"}), 400
        clean = []
        for s in segs:
            if not isinstance(s, dict) or s.get("speaker") not in ("left", "right"):
                return jsonify({"error": "bad_params"}), 400
            try:
                clean.append({"start": float(s["start"]), "end": float(s["end"]), "speaker": s["speaker"]})
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "bad_params"}), 400
        params["segments"] = clean
    jid = _cm().submit(kind="reframe", clip_id=clip_id, params=params,
                       target=_cr().reframe_target(clip_id=clip_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.post("/clips/<clip_id>/captions")
@token_required
def caption_clip(clip_id):
    """Generate + burn styled captions sliced to the clip window (caption.generate/burn)."""
    meta = _cr().load_clip_meta(clip_id)
    if meta is None:
        return jsonify({"error": "clip_not_found"}), 404
    _, words_path = _cr().source_paths(meta.get("source_id", ""))
    if not words_path or not os.path.exists(words_path):
        return jsonify({"error": "no_transcript"}), 409
    data = request.get_json(silent=True) or {}
    style = str(data.get("style") or "opus")
    if style not in _CAPTION_STYLES:
        return jsonify({"error": "bad_style"}), 400
    params = {"style": style}
    if data.get("overrides") is not None:
        clean = _validate_caption_overrides(data["overrides"])
        if clean is None:
            return jsonify({"error": "bad_overrides"}), 400
        params["overrides"] = clean
    # Brand-kit overlays (S9): a watermark + lower-third burned with the captions.
    for key in ("watermark", "lower_third"):
        v = data.get(key)
        if v is not None:
            if not isinstance(v, str):
                return jsonify({"error": "bad_overrides"}), 400
            params[key] = v[:60]
    # Caption-craft toggles (item D, all additive — captions are byte-identical when off):
    # speaker color, balanced line-breaking, keyword emphasis.
    for key in ("color_speakers", "emphasis", "balance_lines"):
        if data.get(key) is not None:
            params[key] = bool(data[key])
    jid = _cm().submit(kind="caption", clip_id=clip_id, params=params,
                       target=_cr().caption_target(clip_id=clip_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.post("/clips/<clip_id>/renders")
@token_required
def render_clip(clip_id):
    """Export the clip to a platform preset (render.export). The result carries the
    ``render_id`` + output path; download via /clips/<clip_id>/renders/<render_id>/file."""
    if _cr().load_clip_meta(clip_id) is None:
        return jsonify({"error": "clip_not_found"}), 404
    data = request.get_json(silent=True) or {}
    preset = str(data.get("preset") or "tiktok")
    if preset not in _CLIP_PRESETS:
        return jsonify({"error": "bad_preset"}), 400
    render_id = uuid.uuid4().hex[:10]
    params = {"preset": preset, "fast": bool(data.get("fast", True))}
    jid = _cm().submit(kind="export", clip_id=clip_id, params=params,
                       target=_cr().export_target(clip_id=clip_id, render_id=render_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.post("/sources/<source_id>/render")
@token_required
def render_pipeline(source_id):
    """One-shot ingest→cut→reframe→caption→export (render.pipeline). The API caller
    supplies every decision up front; the MCP layer adds elicitation pauses on top."""
    _, words_path, err = _source_or_error(source_id)
    if err:
        return err
    if not words_path or not os.path.exists(words_path):
        return jsonify({"error": "no_transcript"}), 409
    data = request.get_json(silent=True) or {}
    rng = _parse_range(data)
    if rng is None:
        return jsonify({"error": "bad_range"}), 400
    rec = {}
    if data.get("recipe_id") is not None:
        rec = _rc().get(str(data["recipe_id"]))
        if rec is None:
            return jsonify({"error": "recipe_not_found"}), 404
    # a recipe supplies defaults for the reusable settings; an explicit body value always wins.
    aspect = str(data.get("aspect") or rec.get("aspect") or "9:16")
    mode = str(data.get("mode") or rec.get("reframe_mode") or "pan")
    preset = str(data.get("preset") or rec.get("platform") or "tiktok")
    style = str(data.get("style") or rec.get("caption_preset") or "opus")
    # A brand kit (explicit, else the recipe's) burns its look in at the caption step.
    brand_kit_id = data.get("brand_kit_id") or rec.get("brand_kit_id")
    # stop_after='reframe' = the "Make clips" path: cut + auto-reframe to review, no burn/export.
    stop_after = data.get("stop_after")
    if (aspect not in _CLIP_ASPECTS or mode not in _CLIP_MODES
            or preset not in _CLIP_PRESETS or style not in _CAPTION_STYLES
            or (data.get("brand_kit_id") is not None and not isinstance(data["brand_kit_id"], str))
            or (stop_after is not None and stop_after != "reframe")):
        return jsonify({"error": "bad_params"}), 400
    start, end = rng
    clip_id, render_id = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    params = {"start": start, "end": end, "aspect": aspect, "mode": mode,
              "style": style, "preset": preset}
    if brand_kit_id:
        params["brand_kit_id"] = str(brand_kit_id)
    fast = data.get("fast") if data.get("fast") is not None else rec.get("fast")
    if fast is not None:
        params["fast"] = bool(fast)
    if data.get("recipe_id"):
        params["recipe_id"] = str(data["recipe_id"])   # provenance: which recipe drove this render
    if stop_after:
        params["stop_after"] = stop_after
    jid = _cm().submit(kind="pipeline", source_id=source_id, clip_id=clip_id, params=params,
                       target=_cr().pipeline_target(source_id=source_id, clip_id=clip_id,
                                                    render_id=render_id, params=params))
    return jsonify(_clip_job_view(_cm().get(jid))), 201


@api_v1_bp.get("/clip-jobs")
@token_required
def list_clip_jobs():
    """List clip/render jobs (the render queue). ``?status=`` + ``?kind=`` filter,
    ``?limit/offset/order`` paginate — same contract as /jobs."""
    limit, offset, order, status = _parse_page_args()
    items = _cm().snapshot_jobs()
    kind = request.args.get("kind")
    if kind:
        wanted = {k.strip() for k in kind.split(",") if k.strip()}
        items = [j for j in items if j.kind in wanted]
    page, total = _paginate(items, status=status, status_attr="status",
                            order=order, limit=limit, offset=offset)
    return jsonify({
        "clip_jobs": [_clip_job_view(j) for j in page],
        "total": total, "returned": len(page),
        "limit": _surface_limit(limit, len(page)), "offset": offset,
    })


@api_v1_bp.get("/clip-jobs/<job_id>")
@token_required
def get_clip_job(job_id):
    cj = _cm().get(job_id)
    if cj is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_clip_job_view(cj))


@api_v1_bp.post("/clip-jobs/<job_id>/cancel")
@token_required
def cancel_clip_job(job_id):
    if not _cm().cancel(job_id):
        return jsonify({"error": "not_found_or_terminal"}), 404
    cj = _cm().get(job_id)
    return jsonify(_clip_job_view(cj)) if cj else ("", 204)


@api_v1_bp.post("/clip-jobs/<job_id>/dismiss")
@token_required
def dismiss_clip_job(job_id):
    if not _cm().dismiss(job_id):
        return jsonify({"error": "not_found_or_active"}), 404
    return ("", 204)


@api_v1_bp.get("/clips/<clip_id>/renders/<render_id>/file")
@token_or_sig_required(SCOPE_MEDIA, kwarg="clip_id")
def get_render_file(clip_id, render_id):
    """Stream a produced render .mp4 (same auth as /jobs/<id>/file: token or media sig)."""
    path = _cr().clip_dir(clip_id) / "renders" / f"{render_id}.mp4"
    if not path.exists():
        return jsonify({"error": "not_found"}), 404
    return send_file(str(path), as_attachment=True, download_name=f"{clip_id}-{render_id}.mp4")


@api_v1_bp.get("/clips/<clip_id>/artifacts/<name>")
@token_or_sig_required(SCOPE_MEDIA, kwarg="clip_id")
def get_clip_artifact(clip_id, name):
    """Stream a clip's intermediate artifact (cut/reframed/captioned mp4) inline for the
    editor previews (S6/S7/S8). Final platform renders use /clips/<id>/renders/<rid>/file."""
    fname = _CLIP_ARTIFACTS.get(name)
    if not fname:
        return jsonify({"error": "bad_artifact"}), 400
    path = _cr().clip_dir(clip_id) / fname
    if not path.exists():
        return jsonify({"error": "not_found"}), 404
    return send_file(str(path), as_attachment=False, download_name=f"{clip_id}-{name}.mp4")


# ---- brand kits (S9): persisted reusable looks applied across a project's clips ----

@api_v1_bp.get("/brand-kits")
@token_required
def list_brand_kits():
    return jsonify({"brand_kits": _bk().list()})


@api_v1_bp.post("/brand-kits")
@token_required
def create_brand_kit():
    data = request.get_json(silent=True) or {}
    err = _validate_brand_kit(data, require_name=True)
    if err:
        return err
    return jsonify(_bk().create(data)), 201


@api_v1_bp.patch("/brand-kits/<kit_id>")
@token_required
def update_brand_kit(kit_id):
    data = request.get_json(silent=True) or {}
    err = _validate_brand_kit(data, require_name=False)
    if err:
        return err
    kit = _bk().update(kit_id, data)
    if kit is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(kit)


@api_v1_bp.delete("/brand-kits/<kit_id>")
@token_required
def delete_brand_kit(kit_id):
    if not _bk().delete(kit_id):
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


# ---- recipes (Phase 3): saved end-to-end pipelines that drive render.pipeline + watch-folder ----

def _validate_recipe(data, *, require_name: bool):
    """Validate a recipe body; return an error response tuple, or None if OK. Enums error rather
    than silently coerce (a recipe drives real renders, so a bad value would fail asynchronously)."""
    bad = (jsonify({"error": "bad_recipe"}), 400)
    if not isinstance(data, dict):
        return bad
    if require_name and not (isinstance(data.get("name"), str) and data["name"].strip()):
        return bad
    if "name" in data and not isinstance(data["name"], str):
        return bad
    enums = (("content_mode", _CONTENT_MODES), ("aspect", _CLIP_ASPECTS),
             ("reframe_mode", _CLIP_MODES), ("caption_preset", _CAPTION_STYLES), ("platform", _CLIP_PRESETS))
    for key, allowed in enums:
        if data.get(key) is not None and data[key] not in allowed:
            return bad
    if data.get("count") is not None and not (isinstance(data["count"], int) and 1 <= data["count"] <= 50):
        return bad
    if data.get("fast") is not None and not isinstance(data["fast"], bool):
        return bad
    if data.get("brand_kit_id") is not None and not isinstance(data["brand_kit_id"], str):
        return bad
    w = data.get("weights")
    if w is not None and (not isinstance(w, dict)
                          or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in w.values())):
        return bad
    return None


@api_v1_bp.get("/recipes")
@token_required
def list_recipes():
    return jsonify({"recipes": _rc().list()})


@api_v1_bp.post("/recipes")
@token_required
def create_recipe():
    data = request.get_json(silent=True) or {}
    err = _validate_recipe(data, require_name=True)
    if err:
        return err
    return jsonify(_rc().create(data)), 201


@api_v1_bp.get("/recipes/<recipe_id>")
@token_required
def get_recipe(recipe_id):
    rec = _rc().get(recipe_id)
    if rec is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


@api_v1_bp.patch("/recipes/<recipe_id>")
@token_required
def update_recipe(recipe_id):
    data = request.get_json(silent=True) or {}
    err = _validate_recipe(data, require_name=False)
    if err:
        return err
    rec = _rc().update(recipe_id, data)
    if rec is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


@api_v1_bp.delete("/recipes/<recipe_id>")
@token_required
def delete_recipe(recipe_id):
    if not _rc().delete(recipe_id):
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


# ---- watches (Phase 3): folder / channel / playlist automations → the review queue ----

def _validate_watch(data, *, require_name: bool, current: dict | None = None):
    """Validate a watch body; return an error response tuple, or None if OK."""
    bad = (jsonify({"error": "bad_watch"}), 400)
    if not isinstance(data, dict):
        return bad
    if require_name:
        if not (isinstance(data.get("name"), str) and data["name"].strip()):
            return bad
        if data.get("kind") not in _WATCH_KINDS:
            return bad
        if not (isinstance(data.get("target"), str) and data["target"].strip()):
            return bad
    if "name" in data and not isinstance(data["name"], str):
        return bad
    if data.get("kind") is not None and data["kind"] not in _WATCH_KINDS:
        return bad
    if "target" in data and not isinstance(data["target"], str):
        return bad
    if data.get("enabled") is not None and not isinstance(data["enabled"], bool):
        return bad
    if data.get("recipe_id") is not None and not isinstance(data["recipe_id"], str):
        return bad
    # Watch targets feed a yt-dlp subprocess (channel/playlist) — enforce the same
    # URL-shape + SSRF guard the download path applies (safety.is_safe_url), and refuse
    # option-shaped targets for every kind.
    if "target" in data or "kind" in data:
        kind = data.get("kind") or (current or {}).get("kind")
        target = data.get("target") if "target" in data else (current or {}).get("target")
        tgt = target.strip() if isinstance(target, str) else ""
        if tgt.startswith("-"):
            return (jsonify({"error": "unsafe_target"}), 400)
        if kind in ("channel", "playlist"):
            from safety import is_safe_url
            if not tgt or not is_safe_url(tgt):
                return (jsonify({"error": "unsafe_target"}), 400)
    return None


@api_v1_bp.get("/watches")
@token_required
def list_watches():
    return jsonify({"watches": _ws().list()})


@api_v1_bp.post("/watches")
@token_required
def create_watch():
    data = request.get_json(silent=True) or {}
    err = _validate_watch(data, require_name=True)
    if err:
        return err
    return jsonify(_ws().create(data)), 201


@api_v1_bp.get("/watches/<watch_id>")
@token_required
def get_watch(watch_id):
    w = _ws().get(watch_id)
    if w is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(w)


@api_v1_bp.patch("/watches/<watch_id>")
@token_required
def update_watch(watch_id):
    data = request.get_json(silent=True) or {}
    err = _validate_watch(data, require_name=False, current=_ws().get(watch_id))
    if err:
        return err
    w = _ws().update(watch_id, data)
    if w is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(w)


@api_v1_bp.delete("/watches/<watch_id>")
@token_required
def delete_watch(watch_id):
    if not _ws().delete(watch_id):
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


@api_v1_bp.post("/watches/<watch_id>/scan")
@token_required
def scan_watch(watch_id):
    """Reconcile a watch once now: detect new videos → ingest (download+auto-transcribe / local
    import) → produce the recipe for any whose transcript is done. Returns this tick's source ids.
    The opt-in background poller runs the same reconcile on an interval."""
    result = current_app.extensions["trove.watch_reconcile"](watch_id)
    if result is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ingested": result["ingested"], "produced": result["produced_now"],
                    "pending": result["pending"], "producing": result["producing"],
                    "ingesting": result["ingesting"]})


# ---- settings (S14): writable engine config surfaced by the demo's Settings screen (07) ----

@api_v1_bp.get("/settings")
@token_required
def get_settings():
    """Every writable key, defaults merged with the user's overrides (demo 07)."""
    return jsonify(_settings().get())


@api_v1_bp.patch("/settings")
@token_required
def patch_settings():
    """Persist a partial settings change. fast/preset/aspect + offline apply immediately
    (offline drives SPOOL_OFFLINE in-process); concurrency + MCP transport apply on restart."""
    data = request.get_json(silent=True) or {}
    clean, err = _validate_settings(data)
    if err:
        return err
    out = _settings().update(clean)
    apply_cb = current_app.extensions.get("trove.apply_settings")
    if apply_cb:
        apply_cb(out)
    return jsonify(out)


@api_v1_bp.post("/agent")
@token_required
def agent_message():
    """The studio Agent panel's turn: a NL message (+ optional source context) driven through a
    bounded ReAct TOOL-LOOP (clip.agent.run_agent) over the SAME /api/v1 tool catalog the UI, MCP,
    and CLI use (the golden rule) — so the in-app agent can do everything the app can: inspect the
    render queue, download, transcribe, discover, produce, manage recipes/watches/brand-kits, etc.

    Body: ``{message, source_id?, confirm_tool?}``. Returns ``{reply, action, jobs[], tools[],
    question?, options?, kind?}`` — ``tools`` is the real per-step tool trace, ``jobs`` any jobs
    started this turn, and a ``clarify`` action carries the question/options/kind for the studio's
    elicitation card. An export/destructive tool returns ``action="confirm"`` with a ``pending``
    ``{tool,args}`` until the next turn echoes it back as ``confirm_tool`` (a one-shot human gate).
    Blocks while the loop runs (each step is an LLM call), so the client shows a thinking state."""
    from clip import agent as clip_agent, llm as clip_llm, moments as clip_moments
    from trove_client import TroveClient

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "missing_message"}), 400
    source_id = (data.get("source_id") or "").strip() or None
    confirmed = (data.get("confirm_tool") or "").strip() or None

    # Source context: feed the transcript to the loop when we have one (so the agent can quote
    # timestamps for moments/clips without an extra fetch).
    lines = None
    if source_id:
        _, words_path = _cr().source_paths(source_id)
        if words_path and os.path.exists(words_path):
            try:
                lines = clip_moments._transcript_lines(transcript_io.load(words_path), None)
            except (OSError, ValueError):
                lines = None
        if lines is None:
            message = f"{message}\n\n(Context: source_id={source_id})"

    # A TroveClient pointed at THIS engine (env TROVE_URL/TROVE_TOKEN, else the local default) — the
    # agent's tools drive the same HTTP surface as every other client, so nothing can diverge.
    client = TroveClient()
    try:
        result = clip_agent.run_agent(message, client=client, transcript_lines=lines,
                                      confirmed_tool=confirmed)
    except (clip_llm.OfflineError, clip_llm.ProviderUnavailableError) as e:
        return jsonify({"error": "llm_unavailable", "message": str(e)}), 503
    except RuntimeError as e:
        # The bridge died before the loop ran any tool (post-retry): a 503 with the
        # message beats an opaque 500 stack trace.
        return jsonify({"error": "llm_failed", "message": str(e)[:300]}), 503

    resp = {"reply": result.get("reply", ""), "action": result.get("action", "reply"),
            "jobs": result.get("jobs", []), "tools": result.get("tools", [])}
    if result.get("action") in ("clarify", "confirm"):
        resp["question"] = result.get("question", "")
        resp["options"] = result.get("options", [])
        resp["kind"] = result.get("kind", "enum")
    if result.get("action") == "confirm":
        resp["pending"] = result.get("pending") or {}
    return jsonify(resp)


# ----- OpenAPI schema -------------------------------------------------

# Hand-rolled because pulling in flask-openapi3 / apispec for ~25
# routes is overkill, and we want the doc to read like prose, not
# auto-generated noise. Keep this in sync with the actual handlers
# above — there's a contract test (test_api_v1.py) that asserts every
# registered ``/api/v1/*`` rule appears here.

_OPENAPI_DOC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Trove API",
        "version": "1.0",
        "description": (
            "JSON control surface for the Trove media downloader / "
            "transcript editor. Stable subset shared with the `trove` "
            "CLI and the `trove-mcp` MCP server."
        ),
    },
    "servers": [{"url": "/api/v1"}],
    "paths": {
        "/health":             {"get":  {"summary": "Liveness probe"}},
        "/capabilities":       {"get":  {"summary": "Server feature / limit / scope registry (unauthenticated)"}},
        "/doctor":             {"get":  {"summary": "Dependency doctor: machine probe + tool presence/versions + encoders (unauthenticated)"}},
        "/jobs":               {
            "get":  {"summary": "List download jobs (paginated, filterable)",
                      "parameters": [
                          {"name": "status", "in": "query",
                           "schema": {"type": "string"},
                           "description": "Comma-separated status filter"},
                          {"name": "limit",  "in": "query",
                           "schema": {"type": "integer", "default": 100, "maximum": 500}},
                          {"name": "offset", "in": "query",
                           "schema": {"type": "integer", "default": 0}},
                          {"name": "order",  "in": "query",
                           "schema": {"type": "string", "enum": ["newest", "oldest"]}},
                      ]},
            "post": {"summary": "Submit a download",
                      "parameters": [
                          {"name": "Idempotency-Key", "in": "header",
                           "schema": {"type": "string"},
                           "description": "Opaque key; same key returns same job for 24h."},
                      ]},
        },
        "/jobs/bulk":          {"post": {"summary": "Submit many downloads"}},
        "/jobs/{job_id}":          {"get":  {"summary": "Get one job"}},
        "/jobs/{job_id}/pause":    {"post": {"summary": "Pause a running job"}},
        "/jobs/{job_id}/resume":   {"post": {"summary": "Resume a paused job"}},
        "/jobs/{job_id}/cancel":   {"post": {"summary": "Cancel a job"}},
        "/jobs/{job_id}/dismiss":  {"post": {"summary": "Drop a finished job"}},
        "/jobs/{job_id}/file":     {"get":  {"summary": "Download the produced file"}},
        "/jobs/{parent_job_id}/transcribe": {"post": {"summary": "Start transcription for a downloaded job"}},
        "/transcripts":        {"get":  {"summary": "List transcripts (paginated, filterable)"}},
        "/transcripts/search": {"get":  {"summary": "Substring search across completed transcripts",
                                          "parameters": [
                                              {"name": "q",       "in": "query", "required": True,
                                               "schema": {"type": "string"}},
                                              {"name": "limit",   "in": "query",
                                               "schema": {"type": "integer", "default": 50, "maximum": 200}},
                                              {"name": "context", "in": "query",
                                               "schema": {"type": "integer", "default": 60}},
                                          ]}},
        "/transcripts/{tid}":           {"get":  {"summary": "Get one transcript"}},
        "/transcripts/{tid}/cancel":    {"post": {"summary": "Cancel a transcribe"}},
        "/transcripts/{tid}/dismiss":   {"post": {"summary": "Drop a finished transcribe"}},
        "/transcripts/{tid}/words/{idx}": {"post": {"summary": "Edit a transcript word (set_text/delete/insert_after/merge_next)"}},
        "/transcripts/{tid}/export.{fmt}": {"get": {"summary": "Export txt/srt/vtt/json"}},
        "/transcripts/{tid}/chunk": {"get": {
            "summary": "Paginated read of a transcript (for MCP / context-bounded clients)",
            "parameters": [
                {"name": "format", "in": "query",
                 "schema": {"type": "string", "enum": ["txt", "srt", "vtt", "json"], "default": "txt"}},
                {"name": "offset", "in": "query",
                 "schema": {"type": "integer", "default": 0},
                 "description": "Char offset for text formats; segment index for json."},
                {"name": "limit",  "in": "query",
                 "schema": {"type": "integer"},
                 "description": "Defaults: 4000 chars (txt/srt/vtt) or 50 segments (json). Capped at 64000 / 500."},
            ]}},
        "/sources/{source_id}/moments": {"post": {"summary": "Find clip-worthy moments over the source transcript (LLM)"}},
        "/sources/{source_id}/energy":  {"get": {"summary": "Normalized 0..1 loudness envelope across the source (the audio-energy waveform)"}},
        "/sources/{source_id}/scenes":  {"get": {"summary": "Scene-cut timestamps within ?start=&end= (the editor timeline's Scenes lane)"}},
        "/sources/{source_id}/filmstrip": {"get": {"summary": "Horizontal thumbnail filmstrip across ?start=&end= as a data URI (the editor timeline's Video lane)"}},
        "/sources/{source_id}/rank":    {"post": {"summary": "Re-rank candidates with the glass-box opportunity score (named, reweightable factors)"}},
        "/sources/{source_id}/produce": {"post": {"summary": "Apply a recipe end-to-end (find→rank→top-N→pipeline per moment) → the review queue"}},
        "/sources/{source_id}/cut":     {"post": {"summary": "Cut a clip [start,end] from the source"}},
        "/sources/{source_id}/render":  {"post": {"summary": "One-shot pipeline: cut→reframe→caption→export"}},
        "/clips/{clip_id}/reframe":     {"post": {"summary": "Reframe a clip (diar⊕ROI speaker pan; aspect/mode)"}},
        "/clips/{clip_id}/captions":    {"post": {"summary": "Generate + burn captions (opus/karaoke/minimal)"}},
        "/clips/{clip_id}/renders":     {"post": {"summary": "Export the clip to a platform preset"}},
        "/clips/{clip_id}/renders/{render_id}/file": {"get": {"summary": "Download a produced render .mp4"}},
        "/clips/{clip_id}/artifacts/{name}": {"get": {"summary": "Stream a clip's intermediate artifact (cut/reframed/captioned mp4)"}},
        "/brand-kits":          {"get": {"summary": "List brand kits"}, "post": {"summary": "Create a brand kit"}},
        "/brand-kits/{kit_id}": {"patch": {"summary": "Update a brand kit"}, "delete": {"summary": "Delete a brand kit"}},
        "/recipes":             {"get": {"summary": "List recipes (saved end-to-end pipelines)"}, "post": {"summary": "Create a recipe"}},
        "/recipes/{recipe_id}": {"get": {"summary": "Get one recipe"}, "patch": {"summary": "Update a recipe"}, "delete": {"summary": "Delete a recipe"}},
        "/watches":             {"get": {"summary": "List watches (folder/channel/playlist automations)"}, "post": {"summary": "Create a watch"}},
        "/watches/{watch_id}":  {"get": {"summary": "Get one watch"}, "patch": {"summary": "Update a watch"}, "delete": {"summary": "Delete a watch"}},
        "/watches/{watch_id}/scan": {"post": {"summary": "Reconcile a watch now: ingest new videos → produce ranked clips per its recipe"}},
        "/settings":            {"get":   {"summary": "Read writable engine config (fast/preset/aspect defaults, offline, concurrency, MCP transport)"},
                                 "patch": {"summary": "Update engine config (fast/preset/aspect + offline apply immediately; concurrency + MCP transport apply on restart)"}},
        "/clip-jobs":          {"get":  {"summary": "List clip/render jobs (the render queue)",
                                          "parameters": [
                                              {"name": "kind", "in": "query",
                                               "schema": {"type": "string"},
                                               "description": "Comma-separated kind filter: moments,cut,reframe,caption,export,pipeline"},
                                              {"name": "status", "in": "query", "schema": {"type": "string"}},
                                              {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 500}},
                                              {"name": "offset", "in": "query", "schema": {"type": "integer"}},
                                              {"name": "order", "in": "query", "schema": {"type": "string", "enum": ["newest", "oldest"]}},
                                          ]}},
        "/clip-jobs/{job_id}":          {"get":  {"summary": "Get one clip job"}},
        "/clip-jobs/{job_id}/cancel":   {"post": {"summary": "Cancel a clip job"}},
        "/clip-jobs/{job_id}/dismiss":  {"post": {"summary": "Drop a finished clip job"}},
        "/agent":              {"post": {"summary": "Agent turn: NL message → a clip-tool action (find_moments / make_clip / clarify / reply)"}},
        "/storage":            {"get":  {"summary": "Disk-usage report"}},
        "/openapi.json":       {"get":  {"summary": "This document"}},
        "/events":             {"get":  {"summary": "Server-Sent Events stream of jobs+transcripts",
                                          "parameters": [
                                              {"name": "max_events", "in": "query",
                                               "schema": {"type": "integer"},
                                               "description": "Test-only termination cap."},
                                              {"name": "interval", "in": "query",
                                               "schema": {"type": "number", "default": 1.0},
                                               "description": "Poll interval in seconds (0.05-10)."},
                                          ]}},
        "/models":                       {"get":  {"summary": "List installed models"}},
        "/models/install-progress":      {"get":  {"summary": "Poll model install progress"}},
        "/models/{name}/use":            {"post": {"summary": "Mark a model as active"}},
        "/models/{name}/remove":         {"post": {"summary": "Uninstall a model"}},
        "/models/{name}/install":        {"post": {"summary": "Begin installing a model"}},
    },
    "headers_global": {
        "X-RateLimit-Limit":     "Requests allowed per 60s window",
        "X-RateLimit-Remaining": "Requests still available in window",
        "X-RateLimit-Window":    "Window length in seconds (always 60)",
        "Retry-After":           "Seconds to wait when rate-limited",
    },
}


@api_v1_bp.get("/openapi.json")
def openapi():
    return jsonify(_OPENAPI_DOC)


# ----- SSE event stream ----------------------------------------------

def _events_snapshot() -> dict:
    """Cheap full snapshot of jobs + transcripts. Diffing happens at
    the client; the server stays stateless across SSE messages."""
    return {
        "ts": time.time(),
        "jobs":        [_job_view(j) for j in _jm().snapshot_jobs()],
        "transcripts": [_tj_view(t) for t in _tm().snapshot_jobs()],
        "clips":       [_clip_job_view(c) for c in _cm().snapshot_jobs()],
    }


@api_v1_bp.get("/events")
@token_required
def events():
    """SSE stream of job + transcript snapshots.

    Emits one ``data:`` frame per change (poll-and-diff at 1s by
    default; tunable with ``?interval=``). A heartbeat comment is
    sent every 15s while idle so proxies don't drop the connection.

    ``?max_events=N`` is a *test hook* — the generator exits cleanly
    after N data frames so pytest doesn't have to kill a long-poll.
    """
    try:
        interval = float(request.args.get("interval", "1.0"))
    except ValueError:
        interval = 1.0
    interval = max(0.05, min(10.0, interval))
    try:
        max_events = int(request.args.get("max_events", "0"))
    except ValueError:
        max_events = 0

    def gen():
        last_payload: str | None = None
        emitted = 0
        last_heartbeat = time.monotonic()
        # Always send the initial snapshot so the client has state to
        # render on connect (otherwise it has to sit through one full
        # interval of nothing).
        first = _events_snapshot()
        last_payload = json.dumps(first, sort_keys=True)
        yield f"event: snapshot\ndata: {last_payload}\n\n"
        emitted += 1
        if max_events and emitted >= max_events:
            return
        while True:
            time.sleep(interval)
            try:
                snap = _events_snapshot()
            except Exception:
                # Server tearing down — close cleanly.
                return
            payload = json.dumps(snap, sort_keys=True)
            now = time.monotonic()
            if payload != last_payload:
                yield f"event: snapshot\ndata: {payload}\n\n"
                last_payload = payload
                last_heartbeat = now
                emitted += 1
                if max_events and emitted >= max_events:
                    return
            elif now - last_heartbeat >= 15.0:
                yield ": keepalive\n\n"
                last_heartbeat = now

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    # Defeat proxy buffering so events arrive in real time.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
