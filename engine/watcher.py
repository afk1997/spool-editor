"""Watch reconciler — advances one watch a single tick (spec §5 Phase 3 watch-folder/channel
automation): detect NEW videos → ingest (download + auto-transcribe, or local import) → once the
transcript is done, run the recipe (produce) → ranked clips land in the review queue.

``reconcile_watch`` is dependency-injected (list_items / ingest / transcript_done / produce) so the
state machine is pure + testable; ``app.py`` wires the real ingest/transcript/produce. The poller
calls this on an interval per enabled watch. NOTHING is auto-published (Phase 4) — an honest gate.
"""
from __future__ import annotations

import os
import subprocess
import time

from network_policy import NetworkPolicy, NetworkPolicyError

_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")

_SETTLE_SECONDS = 30.0


def list_folder_items(folder: str, *, settle_seconds: float = _SETTLE_SECONDS,
                      now: float | None = None) -> list[str]:
    """Sorted video filenames in a local folder (a missing/unreadable dir → []).

    A file whose mtime is still moving is mid-copy: ingesting it produced a truncated
    source that was then permanently marked seen. Defer anything modified within
    ``settle_seconds`` (it shows up whole on a later tick). Downloader partials
    (.part/.crdownload) never match _VIDEO_EXTS, so the extension filter already
    excludes them."""
    t = time.time() if now is None else now
    out: list[str] = []
    try:
        for f in os.listdir(folder):
            if not f.lower().endswith(_VIDEO_EXTS):
                continue
            try:
                if os.path.getmtime(os.path.join(folder, f)) > t - settle_seconds:
                    continue   # still settling — re-check next tick
            except OSError:
                continue       # vanished between listdir and stat
            out.append(f)
    except OSError:
        return []
    return sorted(out)


# A playlist listing spawns a yt-dlp subprocess that can block ~90s; per-tick spawning
# with no cache or backoff hammered remote hosts and re-probed dead URLs forever.
_LISTING_TTL = 300.0            # a fresh listing is reused for this long
_LISTING_BACKOFF_MAX = 3600.0   # consecutive empty/failed listings back off up to this
_listing_cache: dict[tuple[str, int], dict] = {}


def clear_listing_cache() -> None:
    """Drop every cached listing (tests + full resets)."""
    _listing_cache.clear()


def invalidate_listing(target: str | None) -> None:
    """Drop cached listings for one target — a user's manual "Scan now" means
    "look again NOW", not "serve me the 5-minute cache"."""
    t = (target or "").strip()
    for k in [k for k in _listing_cache if k[0] == t]:
        _listing_cache.pop(k, None)


def list_playlist_items(url: str, *, network_policy: NetworkPolicy,
                        limit: int = 30, ytdlp: str = "yt-dlp",
                        now: float | None = None) -> list[str]:
    """Canonical video URLs on a channel/playlist via a yt-dlp FLAT listing (metadata only, no
    download), newest-first and capped. We print ``url`` (the per-entry webpage URL), NOT the bare
    ``id`` — the reconciler hands each item straight to ``enqueue_download(url=…)`` and a bare id is
    not a reliable download target across extractors. Ordinary yt-dlp failures degrade to [];
    Offline raises the structured policy denial so callers cannot mistake it for an empty feed.

    The target is user-controlled config: reject option-shaped values and pass it after ``--``
    so it can never be parsed as a yt-dlp flag (mirrors runner.build_*_argv — the original
    download path's argv-injection guard, which this late-added path previously skipped).

    Listings are cached for ``_LISTING_TTL`` per (target, limit); empty/failed listings
    back off exponentially (x2 per consecutive failure, capped at ``_LISTING_BACKOFF_MAX``)
    so a dead URL doesn't spawn a ~90s subprocess every tick. A manual scan calls
    ``invalidate_listing`` first (app.py), so "Scan now" always re-fetches."""
    target = (url or "").strip()
    if not target or target.startswith("-"):
        return []
    # The lease starts before the cache lookup: Offline is literal even with a warm
    # listing, and switching Offline cannot race a listing already in progress. Keep
    # it through result parsing + cache publication so every observable remote-listing
    # side effect belongs to the admitted operation.
    with network_policy.egress("watch_listing"):
        t = time.time() if now is None else now
        key = (target, int(limit))
        hit = _listing_cache.get(key)
        if hit is not None and t < hit["expires"]:
            return list(hit["items"])
        try:
            out = subprocess.run(
                [ytdlp, "--flat-playlist", "--print", "url",
                 "--playlist-end", str(int(limit)), "--", target],
                capture_output=True, text=True, timeout=90,
            ).stdout
            items = [ln.strip() for ln in out.splitlines() if ln.strip()]
        except NetworkPolicyError:
            raise
        except Exception:
            items = []
        if items:
            _listing_cache[key] = {"items": items, "expires": t + _LISTING_TTL, "fails": 0}
        else:
            fails = (hit or {}).get("fails", 0) + 1
            backoff = min(_LISTING_BACKOFF_MAX, _LISTING_TTL * (2 ** (fails - 1)))
            _listing_cache[key] = {"items": [], "expires": t + backoff, "fails": fails}
        return list(items)


# A produce job that ERRORs is retried (codex/network hiccups are transient), but bounded so a
# permanently-bad source (e.g. a transcript that yields no usable moments) can't spin codex forever.
_MAX_PRODUCE_ATTEMPTS = 3
# An ingest that fails transiently (a rare network/probe hiccup swallowed by app's broad except →
# ingest returns None) is retried on later ticks rather than dropped forever, but bounded the same
# way: a genuinely-bad item (e.g. a permanently-unreachable URL) is retried a few ticks then marked
# seen and abandoned, so it can't spin in an infinite retry loop.
_MAX_INGEST_ATTEMPTS = 3


def reconcile_watch(watch: dict, *, list_items, ingest, transcript_done, produce, produce_status) -> dict:
    """Advance ``watch`` one tick. Returns the new ``seen``/``pending``/``produced``/``producing``
    state plus this tick's ``ingested`` + ``produced_now`` source ids (for persistence + reporting).
    Disabled watches are a no-op.

    State machine (each source flows seen → pending → producing → produced; an item whose ingest
    fails waits in ``ingesting`` first):
      - ``ingesting`` maps an item key → the count of failed ingest attempts so far. The item is
        retried on later ticks (bounded by ``_MAX_INGEST_ATTEMPTS``) and is deliberately NOT in
        ``seen`` yet, so a transient failure can't drop it forever.
      - ``pending``   maps an item key → its source id (ingested, awaiting transcription).
      - ``producing`` maps a source id → ``{"job": <produce job id>, "attempts": n}`` (the recipe
        produce was ENQUEUED; we're waiting on that async job's terminal status).
      - ``produced``  lists source ids whose produce job actually COMPLETED.

    ``produce(watch, sid)`` enqueues a produce job and returns its id; ``produce_status(job_id)``
    reports that job's status (``done`` → produced; ``error`` → retry up to ``_MAX_PRODUCE_ATTEMPTS``
    then abandon; ``cancelled`` → abandon, respecting the user's intent; ``queued``/``running``/
    unknown → still in flight, wait). Marking ``produced`` only on a confirmed ``done`` is the fix
    for the old bug where a source was marked produced the instant produce was enqueued, so any
    later failure was silently never retried.

    ``ingest(watch, key)`` returns a truthy source id on success or a falsy value on failure; only a
    SUCCESSFUL ingest moves an item to ``seen`` (the fix for the old bug where the key was marked
    seen BEFORE ingest, so a transient ingest failure dropped the item forever) — a failed ingest
    accrues in ``ingesting`` and is retried, then abandoned (marked seen) after ``_MAX_INGEST_ATTEMPTS``."""
    if not watch.get("enabled", True):
        # Intentional full no-op while paused: we deliberately do NOT advance, drain, or reconcile a
        # producing/in-flight entry here — pause means pause (a re-enable resumes it next tick).
        # Locked by test_reconcile_disabled_watch_is_a_noop.
        return {"seen": watch.get("seen", []), "pending": watch.get("pending", {}),
                "produced": watch.get("produced", []), "producing": watch.get("producing", {}),
                "ingesting": watch.get("ingesting", {}), "ingested": [], "produced_now": []}
    seen = list(watch.get("seen") or [])
    pending = dict(watch.get("pending") or {})
    produced = list(watch.get("produced") or [])
    producing = dict(watch.get("producing") or {})
    ingesting = dict(watch.get("ingesting") or {})
    ingested, produced_now = [], []

    # 1) detect + ingest new items. Only a SUCCESSFUL ingest (truthy sid) marks the item seen and
    #    moves it to ``pending``; a transient failure accrues in ``ingesting`` and is retried on
    #    later ticks instead of being dropped forever — bounded by _MAX_INGEST_ATTEMPTS so a
    #    permanently-bad item is abandoned (marked seen) after a few tries, never in an infinite loop.
    #    (Items already in ``pending`` are also in ``seen``, so the seen check covers them.)
    for key in list_items(watch):
        if key in seen:
            continue
        sid = ingest(watch, key)
        if sid:
            seen.append(key)                                  # successful ingest → never re-ingest
            pending[key] = sid
            ingested.append(sid)
            ingesting.pop(key, None)                          # clear any accrued retry count
        elif ingesting.get(key, 0) + 1 >= _MAX_INGEST_ATTEMPTS:
            seen.append(key)                                  # give up on a persistently-bad item
            ingesting.pop(key, None)                          # (warning logged by the caller)
        else:
            ingesting[key] = ingesting.get(key, 0) + 1        # transient failure → retry next tick

    # 2) pending sources whose transcript is done → ENQUEUE produce (async) and track the job.
    #    Don't mark produced here — that waits on the job's terminal status in step 3.
    for key, sid in list(pending.items()):
        if sid in produced or sid in producing:
            pending.pop(key, None)
            continue
        if transcript_done(sid):
            jid = produce(watch, sid)
            producing[sid] = {"job": jid, "attempts": 1}
            pending.pop(key, None)

    # 3) reconcile in-flight produce jobs on their terminal status.
    for sid, st in list(producing.items()):
        status = produce_status(st.get("job"))
        if status == "done":
            produced.append(sid)
            produced_now.append(sid)
            producing.pop(sid, None)
        elif status == "error":
            if int(st.get("attempts", 1)) < _MAX_PRODUCE_ATTEMPTS:
                jid = produce(watch, sid)                    # retry: re-enqueue, bump the attempt count
                producing[sid] = {"job": jid, "attempts": int(st.get("attempts", 1)) + 1}
            else:
                producing.pop(sid, None)                     # exhausted retries → give up (logged by caller)
        elif status == "cancelled":
            producing.pop(sid, None)                         # user cancelled → don't auto-retry
        # else queued / running / None (not yet visible) → still in flight; leave it for next tick.

    return {"seen": seen, "pending": pending, "produced": produced, "producing": producing,
            "ingesting": ingesting, "ingested": ingested, "produced_now": produced_now}
