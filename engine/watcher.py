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

_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


def list_folder_items(folder: str) -> list[str]:
    """Sorted video filenames in a local folder (a missing/unreadable dir → [])."""
    try:
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(_VIDEO_EXTS))
    except OSError:
        return []


def list_playlist_items(url: str, *, limit: int = 30, ytdlp: str = "yt-dlp") -> list[str]:
    """Canonical video URLs on a channel/playlist via a yt-dlp FLAT listing (metadata only, no
    download), newest-first and capped. We print ``url`` (the per-entry webpage URL), NOT the bare
    ``id`` — the reconciler hands each item straight to ``enqueue_download(url=…)`` and a bare id is
    not a reliable download target across extractors. Failures (offline / bad URL) degrade to []."""
    try:
        out = subprocess.run(
            [ytdlp, "--flat-playlist", "--print", "url", "--playlist-end", str(int(limit)), url],
            capture_output=True, text=True, timeout=90,
        ).stdout
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


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
