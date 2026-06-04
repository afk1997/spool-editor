# app.py
from __future__ import annotations
import os
from pathlib import Path
from flask import Flask, jsonify, request

from safety import RateLimiter, attach_cors, attach_security_headers
from runner import run_download
from jobs import JobManager, Job, JobStatus
import models_store
import machine
import transcribe_jobs
import transcriber
import transcript_io
import clip_jobs
import brand_kits
import recipes
import settings as settings_store_mod
import transcript_index as transcript_index_mod
import clip_runner
import time as _time
from util import sanitize_filename


def _resolve_download_dir() -> Path:
    """Resolve the on-disk download root.

    Read at create_app() time (NOT at import time) so tests that set
    ``TROVE_DOWNLOAD_DIR`` per-fixture get isolated trees instead of
    accidentally sharing the real ``./downloads`` directory.
    """
    return Path(os.environ.get("TROVE_DOWNLOAD_DIR")
                or (Path(__file__).parent / "downloads"))


DOWNLOAD_DIR = _resolve_download_dir()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

JOB_TTL = int(os.environ.get("TROVE_JOB_TTL_SECONDS", "3600"))
MAX_WORKERS = int(os.environ.get("TROVE_MAX_WORKERS", "4"))
RATE_LIMIT_PER_MIN = int(os.environ.get("TROVE_RATE_LIMIT", "30"))
# Hard cap on URLs accepted in one /api/batch-download request. Each URL
# triggers a synchronous run_info() (~1-2s) and then a queued download,
# so very large pastes would block the request thread for minutes and
# flood the queue. 50 keeps the worst case under ~2 minutes and the
# response payload under ~1 MB. The hero form mirrors the cap so users
# get an inline error before they hit submit.
BATCH_MAX_URLS = int(os.environ.get("TROVE_BATCH_MAX_URLS", "50"))


def create_app() -> Flask:
    app = Flask(__name__)
    attach_security_headers(app)
    attach_cors(app)

    rate_limiter = RateLimiter(rate=RATE_LIMIT_PER_MIN, per_seconds=60)
    # Prefer the module-level DOWNLOAD_DIR so existing tests can use
    # ``monkeypatch.setattr(app, "DOWNLOAD_DIR", ...)``. Fall back to
    # the env-var-aware resolver only if the module global was cleared.
    download_dir = DOWNLOAD_DIR if DOWNLOAD_DIR is not None else _resolve_download_dir()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Settings store (demo 07 / spec §5 P2 "config from the UI"). Built before the job pools
    # so a UI-written concurrency takes effect at boot: a persisted override wins over the
    # env-var default, which still applies when the user never set one ("applies on restart").
    # The fast/preset/aspect defaults are read hot per-render by the ClipRunner.
    settings_store = settings_store_mod.SettingsStore(download_dir / "settings.json")
    _settings_ov = settings_store.overrides()
    effective_max_workers = int(_settings_ov.get("max_workers", MAX_WORKERS))
    effective_clip_workers = int(_settings_ov.get("clip_workers", os.environ.get("TROVE_CLIP_WORKERS", "2")))
    app.extensions["trove.settings"] = settings_store

    # FTS5 (trigram) transcript index — an additive accelerator for /transcripts/search (spec
    # §7.2). The in-memory word-scan stays the source of truth; this only narrows which
    # transcripts it must open. Inert (full-scan fallback) if FTS5/trigram isn't available.
    transcript_index = transcript_index_mod.TranscriptIndex(download_dir / "transcript_index.sqlite3")
    app.extensions["trove.transcript_index"] = transcript_index

    job_manager = JobManager(
        max_workers=effective_max_workers,
        ttl_seconds=JOB_TTL,
        store_path=download_dir / "jobs.json",
    )
    app.extensions["trove.jobs"] = job_manager
    app.extensions["trove.download_dir"] = download_dir
    app.extensions["trove.rate_limiter"] = rate_limiter
    # Batch cap is exposed as an extension (in addition to the module
    # global) so /api/v1/capabilities can read the live value rather
    # than a stale import-time copy. ``app.py`` still owns the env-
    # var default; tests can set ``app.extensions["trove.batch_max"]``
    # to assert the registry reflects per-process tweaks.
    app.extensions["trove.batch_max"] = BATCH_MAX_URLS

    # One-shot migration sweep: persist any v1 .words.json files as v2
    # at startup so individual GET handlers never have to mutate disk
    # mid-request (which races concurrent writers and bumps mtime
    # unexpectedly). transcript_io.load() is now pure — this sweep is
    # the only place a read can trigger a write, and it runs exactly
    # once per process boot. Errors per-file are swallowed inside
    # migrate_all so one corrupt artifact can't block startup.
    import transcript_io as _tio
    _migrated, _skipped = _tio.migrate_all(str(download_dir))
    if _migrated:
        app.logger.info(
            "transcript_io: migrated %d v1 transcript file(s) to v2 at startup",
            _migrated,
        )
    for _name, _reason in _skipped:
        # Surface skipped artifacts through the app logger (not stderr)
        # so operators see them in normal log streams. Boot continues.
        app.logger.warning(
            "transcript_io: skipped %s during startup migration sweep: %s",
            _name, _reason,
        )

    transcribe_manager = transcribe_jobs.TranscribeJobManager(
        max_workers=1,
        store_path=download_dir / "transcribe_jobs.json",
    )
    app.extensions["trove.transcribe"] = transcribe_manager

    # Clip/render queue — trove's job machinery with new kinds (spec §1.1). The runner
    # holds the orchestration; the manager runs it. Both reachable from the v1 blueprint
    # and (later) the MCP server, so manual + agent mode drive the same engine + queue.
    clip_manager = clip_jobs.ClipJobManager(
        max_workers=effective_clip_workers,
        store_path=download_dir / "clip_jobs.json",
    )
    app.extensions["trove.clips"] = clip_manager
    app.extensions["trove.clip_runner"] = clip_runner.ClipRunner(
        download_dir=download_dir,
        job_manager=job_manager,
        clip_manager=clip_manager,
        settings_store=settings_store,
    )
    # Brand kits — persisted reusable looks applied across a project's clips (spec §5 P2).
    app.extensions["trove.brand_kits"] = brand_kits.BrandKitStore(download_dir / "brand_kits.json")
    # Recipes — saved end-to-end pipelines that drive render.pipeline + watch-folder (spec §5 P3).
    app.extensions["trove.recipes"] = recipes.RecipeStore(download_dir / "recipes.json")

    # Register the JSON v1 API blueprint — the headless surface for the studio + MCP.
    from routes.api_v1 import api_v1_bp
    app.register_blueprint(api_v1_bp)

    # Sweeper has to start AFTER both managers exist because the
    # keep_predicate references transcribe_manager — without it the
    # TTL sweep would unlink the source media for every completed
    # transcript after one idle hour, silently 404-ing the transcript
    # page and dropping the download.
    #
    # Important: walk ALL children, not just the most recent one. A
    # parent may have an older DONE transcribe AND a newer ERROR
    # transcribe (e.g. user re-ran the transcribe with a bigger model
    # and it failed). The older DONE result is still valid and must
    # keep the parent alive. Using ``get_by_parent`` (which returns
    # only the latest) would silently drop those.
    _KEEP_STATUSES = {
        transcribe_jobs.TranscribeStatus.QUEUED,
        transcribe_jobs.TranscribeStatus.RUNNING,
        transcribe_jobs.TranscribeStatus.DONE,
    }

    def _has_active_or_done_transcribe(parent_job) -> bool:
        for tj in transcribe_manager.snapshot_jobs():
            if tj.parent_job_id == parent_job.id and tj.status in _KEEP_STATUSES:
                return True
        return False

    def _keep_source(parent_job) -> bool:
        # Also pin a source whose clips/renders still exist — sweeping its media out
        # from under a clip would 404 re-cuts/reframes and orphan the clip tree.
        return _has_active_or_done_transcribe(parent_job) or bool(
            clip_manager.get_by_source(parent_job.id))

    job_manager.start_sweeper(
        interval_seconds=300,
        keep_predicate=_keep_source,
    )

    # Idempotent HTMX/JS status polls — exempted from the per-IP rate
    # limit because they fire every 1-2s while a page is open and would
    # otherwise blow the budget within ~30s, 429-ing every subsequent
    # user action (e.g. clicking "pick this model" on the setup page).
    # All exempt paths are read-only GETs that return HTML/JSON status.
    _POLL_EXEMPT_PREFIXES = (
        "/api/status/",
        "/api/status-card/",
        "/api/transcribe/setup-progress",
    )

    def _is_poll_exempt() -> bool:
        if request.method != "GET":
            return False
        path = request.path
        if any(path.startswith(p) for p in _POLL_EXEMPT_PREFIXES):
            return True
        # /api/transcribe/<id>/status — match on suffix to avoid pinning
        # to a specific id format.
        if path.startswith("/api/transcribe/") and path.endswith("/status"):
            return True
        # Cheap v1 status/poll GETs that the CLI + MCP server hammer
        # while waiting on a job. We deliberately do NOT exempt the
        # whole /api/v1/* prefix — file streams (`/jobs/<id>/file`)
        # and export downloads (`/transcripts/<id>/export.*`) are
        # bandwidth-heavy and stay rate-limited so a token-less
        # deployment isn't a free egress vector.
        if path == "/api/v1/health":
            return True
        if path == "/api/v1/jobs" or path == "/api/v1/transcripts":
            return True
        if path == "/api/v1/models" or path == "/api/v1/models/install-progress":
            return True
        # New v1 read-only endpoints used by the CLI/MCP. All cheap +
        # idempotent so they're safe to exempt from per-IP rate
        # limiting (same reasoning as /jobs and /transcripts above).
        if path in (
            "/api/v1/storage",
            "/api/v1/openapi.json",
            "/api/v1/events",
        ) or path.startswith("/api/v1/transcripts/search"):
            return True
        # /api/v1/jobs/<id> and /api/v1/transcripts/<id> — single-resource
        # status reads. Match by prefix-and-no-slash-after-id so we don't
        # accidentally exempt /jobs/<id>/file or /transcripts/<id>/export.*.
        for prefix in ("/api/v1/jobs/", "/api/v1/transcripts/"):
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                return True
        return False

    @app.before_request
    def _rate_limit():
        if not request.path.startswith("/api/"):
            return None
        # Stash IP on g so the after-request hook can attach
        # X-RateLimit-* headers without recomputing.
        from flask import g as _g
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        _g.trove_rl_ip = ip
        if _is_poll_exempt():
            return None
        if not rate_limiter.allow(ip):
            remaining, retry_after = rate_limiter.remaining(ip)
            resp = jsonify({"error": "rate_limited", "retry_after": round(retry_after, 1)})
            resp.status_code = 429
            resp.headers["X-RateLimit-Limit"] = str(rate_limiter.rate)
            resp.headers["X-RateLimit-Remaining"] = "0"
            resp.headers["X-RateLimit-Window"] = "60"
            resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
            return resp
        return None

    @app.after_request
    def _rate_limit_headers(resp):
        # Only attach to /api/* responses, and only when the
        # before-request hook actually computed an IP for this request
        # (i.e. skip static, WS, etc.).
        from flask import g as _g
        if not request.path.startswith("/api/"):
            return resp
        ip = getattr(_g, "trove_rl_ip", None)
        if not ip:
            return resp
        try:
            remaining, retry_after = rate_limiter.remaining(ip)
        except Exception:
            return resp
        resp.headers["X-RateLimit-Limit"] = str(rate_limiter.rate)
        resp.headers["X-RateLimit-Remaining"] = str(min(remaining, rate_limiter.rate))
        resp.headers["X-RateLimit-Window"] = "60"
        if retry_after > 0 and "Retry-After" not in resp.headers:
            resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        return resp

    # --- helpers -----------------------------------------------------------

    def _build_transcribe_target(media_path: str, base_no_ext: str, wav_path: str):
        """Return the per-transcribe ``_work(tj, *, model_path)`` closure.

        Extracted from api_transcribe_start so the auto-transcribe path
        (triggered from inside the download worker on success) can reuse
        the exact same body — extract → transcribe → diarize → artifacts
        with consistent cancel semantics and WAV cleanup.
        """
        def _work(tj, *, model_path):
            def _register_ffmpeg(proc):
                # Stash the live ffmpeg Popen on the TranscribeJob so the
                # /cancel endpoint (which calls TranscribeJobManager.cancel)
                # can kill it mid-extract instead of waiting for it to
                # complete. Cleared (set to None) when extract returns.
                tj.process_handle = proc

            try:
                # 1. Extract audio
                try:
                    transcriber.extract_audio(
                        media_path, wav_path,
                        cancel_check=lambda: tj._cancel_flag,
                        register_proc=_register_ffmpeg,
                    )
                except RuntimeError as e:
                    # extract_audio raises RuntimeError("cancelled") when the
                    # user hit cancel during ffmpeg — treat as a clean abort.
                    if str(e) == "cancelled" or tj._cancel_flag:
                        return
                    raise
                if tj._cancel_flag: return
                transcribe_manager.update_progress(tj.id, 5)

                # 2. Transcribe
                result = transcriber.run_transcribe(
                    audio_path=wav_path,
                    model_path=model_path,
                    progress_cb=lambda pct: transcribe_manager.update_progress(tj.id, pct),
                    cancel_check=lambda: tj._cancel_flag,
                )
                if result.error == "cancelled" or tj._cancel_flag:
                    return
                if result.error:
                    tj.status = transcribe_jobs.TranscribeStatus.ERROR
                    tj.error_category = "transcribe_error"
                    tj.error_message = result.error
                    return

                # 2.5 VAD word-realignment + speaker diarization. Two
                # INDEPENDENT best-effort steps (neither ever kills the
                # transcribe):
                #
                # (a) Word-realignment — silero-vad's speech regions fix
                #     whisper.cpp's drift after silences (without DTW, whisper
                #     places words ~0.3-0.5s too early after each pause and the
                #     error compounds across the clip). This is a CAPTION-TIMING
                #     fix and must NOT depend on the speaker-label feature flag,
                #     so it runs whenever silero-vad is installed
                #     (``vad_available()`` — ignores TROVE_DIARIZATION).
                #
                # (b) Speaker diarization (labelling) — stays gated behind the
                #     TROVE_DIARIZATION flag + its heavier deps (``available()``).
                #     It re-derives its own VAD internally (deliberate — it
                #     operates on speech regions in the original timeline), so
                #     the two steps don't share state.
                #
                # Diarization outcome on the TranscribeJob:
                #   None     → not attempted (feature off / deps missing)
                #   complete → chunks applied; speaker_count set
                #   empty    → ran but no speech detected
                #   failed   → exception during diarize; reason in diarization_error
                try:
                    import diarizer
                    if diarizer.vad_available():
                        try:
                            vad_regions = diarizer._vad_speech_chunks(wav_path)
                            if vad_regions:
                                transcriber.realign_words_to_vad(result, vad_regions)
                        except Exception as e:
                            # Realignment is a pure timing refinement — a failure
                            # leaves whisper's original timestamps and never
                            # touches the diarization outcome below.
                            app.logger.warning("VAD word-realignment failed: %s", e)
                    if diarizer.available():
                        try:
                            chunks = diarizer.diarize(audio_path=wav_path)
                            if chunks:
                                transcriber.apply_speakers(result, chunks)
                                tj.diarization_status = "complete"
                                tj.speaker_count = len({c.speaker for c in chunks})
                            else:
                                tj.diarization_status = "empty"
                                tj.speaker_count = 0
                        except Exception as e:
                            tj.diarization_status = "failed"
                            tj.diarization_error = str(e) or type(e).__name__
                            app.logger.warning("diarization failed: %s", e)
                except Exception as e:
                    # diarizer module itself didn't import — treat as not attempted.
                    app.logger.warning("diarizer unavailable: %s", e)

                # 3. Write artifacts
                transcriber.write_artifacts(result, base_no_ext)
                tj.duration_seconds = result.duration
                tj.language_detected = result.language
                # Index the finished transcript for the FTS5 search accelerator (best-effort).
                transcript_index_mod.index_words_file(
                    transcript_index, tj.id, base_no_ext + ".words.json")
            finally:
                # Always remove the temp WAV — even on cancel/error/exception.
                # The success path used to clean it up, but cancel/error early-
                # returned and leaked a multi-MB file per aborted transcribe.
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except OSError:
                    pass

        return _work

    def _try_auto_transcribe(parent: Job) -> None:
        """Submit a transcribe for a just-completed download, if requested.

        Called from inside the download worker AFTER ``job.file_path`` is
        set and BEFORE the worker returns (i.e. before JobManager flips
        status to DONE). We deliberately do not check ``parent.status`` —
        the caller has just confirmed a successful download and any
        cancel/error path returns earlier without invoking us.

        Degrades gracefully:
        - No active model installed → set ``_auto_transcribe_hint`` so
          the DONE card can render a "set up a model" link, then return.
        - A transcribe for this parent is already queued/running/done →
          no-op (idempotent; protects against double-fires).
        """
        if not parent.auto_transcribe or not parent.file_path:
            return
        # Cancel race: /api/job/<id>/cancel can flip status to CANCELLED
        # while we're inside _work. The download still wrote file_path
        # before the kill landed, but the user clearly doesn't want the
        # follow-up transcribe. Same for ERROR (a late error category set
        # by the runner). PAUSED is benign — pause-then-success races
        # legitimately become DONE in JobManager._run, which is the
        # behavior we want.
        if parent.status in (JobStatus.CANCELLED, JobStatus.ERROR):
            return
        model_path = models_store.get_active_path()
        if model_path is None:
            parent._auto_transcribe_hint = "no_active_model"
            return
        existing = transcribe_manager.get_by_parent(parent.id)
        if existing and existing.status in (
            transcribe_jobs.TranscribeStatus.QUEUED,
            transcribe_jobs.TranscribeStatus.RUNNING,
            transcribe_jobs.TranscribeStatus.DONE,
        ):
            return
        base_no_ext = os.path.splitext(parent.file_path)[0]
        wav_path = base_no_ext + ".wav"
        try:
            transcribe_manager.submit(
                parent_job_id=parent.id,
                model_path=str(model_path),
                target=_build_transcribe_target(parent.file_path, base_no_ext, wav_path),
            )
        except Exception as e:
            # Never let a transcribe-submit failure poison the download
            # job — it's an opportunistic add-on, not the core contract.
            app.logger.warning("auto-transcribe submit failed for %s: %s", parent.id, e)

    def _enqueue_download(
        url: str, format_choice: str, format_id, title: str,
        thumbnail: str = "", *, auto_transcribe: bool = False,
        subtitles: bool = False, chapters: bool = False, embed: bool = False,
    ) -> str:
        def _work(job: Job):
            job.thumbnail = thumbnail
            job.format_choice = format_choice
            job.format_id = format_id
            out_template = str(DOWNLOAD_DIR / f"{job.id}.%(ext)s")
            job.out_template = out_template

            def _on_progress(downloaded, total, speed, eta, frag_idx, frag_count):
                job.downloaded_bytes = downloaded
                job.total_bytes = total
                job.speed = speed
                job.eta = eta
                job.fragment_index = frag_idx
                job.fragment_count = frag_count

            def _register_proc(popen):
                job.process = popen

            result = run_download(
                url=url,
                out_template=out_template,
                format_choice=format_choice,
                format_id=format_id,
                progress_cb=_on_progress,
                register_process=_register_proc,
                was_paused_check=lambda: job._was_paused,
                subtitles=subtitles,
                chapters=chapters,
                embed=embed,
            )
            if result.error_category:
                if not job._was_paused:
                    job.status = JobStatus.ERROR
                    job.error_category = result.error_category
                    job.error_message = result.error_raw
                return
            ext = os.path.splitext(result.file_path)[1] if result.file_path else ""
            job.file_path = result.file_path
            job.filename = sanitize_filename(title, ext)
            _try_auto_transcribe(job)

        return job_manager.submit(
            target=_work, title=title, url=url, auto_transcribe=auto_transcribe,
        )

    # --- v1 action helpers -----------------------------------------------
    # The /api/v1 blueprint reaches into these via app.extensions so the
    # CLI + MCP server don't need to duplicate the work-thunk construction
    # that lives inside the legacy HTML endpoints. Same in-process state,
    # same managers, same locks — just a JSON-shaped surface on top.

    def _v1_resume_job(job_id: str) -> bool:
        job = job_manager.get(job_id)
        if job is None:
            return False
        url = job.url
        format_choice = job.format_choice
        format_id = job.format_id
        title = job.title
        thumbnail = job.thumbnail
        out_template = job.out_template or str(DOWNLOAD_DIR / f"{job.id}.%(ext)s")

        def _work(j: Job):
            j.thumbnail = thumbnail
            j.format_choice = format_choice
            j.format_id = format_id
            j.out_template = out_template

            def _on_progress(downloaded, total, speed, eta, frag_idx, frag_count):
                j.downloaded_bytes = downloaded
                j.total_bytes = total
                j.speed = speed
                j.eta = eta
                j.fragment_index = frag_idx
                j.fragment_count = frag_count

            def _register_proc(popen):
                j.process = popen

            result = run_download(
                url=url,
                out_template=out_template,
                format_choice=format_choice,
                format_id=format_id,
                progress_cb=_on_progress,
                register_process=_register_proc,
                was_paused_check=lambda: j._was_paused,
            )
            if result.error_category:
                if not j._was_paused:
                    j.status = JobStatus.ERROR
                    j.error_category = result.error_category
                    j.error_message = result.error_raw
                return
            ext = os.path.splitext(result.file_path)[1] if result.file_path else ""
            j.file_path = result.file_path
            j.filename = sanitize_filename(title, ext)
            _try_auto_transcribe(j)

        return job_manager.resume(job_id, target=_work)

    def _v1_start_transcribe(parent_job_id: str) -> str | None:
        """Submit a transcribe for an already-downloaded clip. Returns
        the transcribe id, or None if preconditions fail (the v1 route
        guards parent-state and active-model presence before calling)."""
        parent = job_manager.get(parent_job_id)
        if parent is None or parent.status != JobStatus.DONE or not parent.file_path:
            return None
        model_path = models_store.get_active_path()
        if model_path is None:
            return None
        media_path = parent.file_path
        base_no_ext = os.path.splitext(media_path)[0]
        wav_path = base_no_ext + ".wav"
        return transcribe_manager.submit(
            parent_job_id=parent_job_id,
            model_path=str(model_path),
            target=_build_transcribe_target(media_path, base_no_ext, wav_path),
        )

    app.extensions["trove.actions"] = {
        "enqueue_download": _enqueue_download,
        "resume_job": _v1_resume_job,
        "start_transcribe": _v1_start_transcribe,
    }

    return app


if __name__ == "__main__":
    from config import DEFAULT_HOST, DEFAULT_PORT, assert_safe_bind
    # Refuse to start on a public bind without auth — see config.py.
    assert_safe_bind(DEFAULT_HOST)
    app = create_app()
    app.run(host=DEFAULT_HOST, port=DEFAULT_PORT)
