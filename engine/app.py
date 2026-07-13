# app.py
from __future__ import annotations
import os
import threading
from pathlib import Path
from flask import Flask, jsonify, request

from safety import RateLimiter, attach_cors, attach_security_headers
from runner import run_download, run_info
from jobs import AttemptUnwindingError, JobManager, Job, JobStatus
from attempt_staging import AttemptOutcome, Promotion
import models_store
import machine
import transcribe_jobs
import transcriber
import transcript_io
import clip_jobs
import brand_kits
import recipes
import watches
import watcher
import settings as settings_store_mod
import transcript_index as transcript_index_mod
import clip_runner
import time as _time
from util import link_or_copy, sanitize_filename


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

    @app.errorhandler(AttemptUnwindingError)
    def _attempt_unwinding(_error):
        return jsonify({"error": "attempt_unwinding"}), 409

    rate_limiter = RateLimiter(rate=RATE_LIMIT_PER_MIN, per_seconds=60)
    # Prefer the module-level DOWNLOAD_DIR so existing tests can use
    # ``monkeypatch.setattr(app, "DOWNLOAD_DIR", ...)``. Fall back to
    # the env-var-aware resolver only if the module global was cleared.
    download_dir = DOWNLOAD_DIR if DOWNLOAD_DIR is not None else _resolve_download_dir()
    download_dir.mkdir(parents=True, exist_ok=True)
    job_ttl = int(os.environ.get("TROVE_JOB_TTL_SECONDS", str(JOB_TTL)))

    # Settings store (demo 07 / spec §5 P2 "config from the UI"). Built before the job pools
    # so a UI-written concurrency takes effect at boot: a persisted override wins over the
    # env-var default, which still applies when the user never set one ("applies on restart").
    # The fast/preset/aspect defaults are read hot per-render by the ClipRunner.
    settings_store = settings_store_mod.SettingsStore(download_dir / "settings.json")
    _settings_ov = settings_store.overrides()
    effective_max_workers = int(_settings_ov.get("max_workers", MAX_WORKERS))
    effective_clip_workers = int(_settings_ov.get("clip_workers", os.environ.get("TROVE_CLIP_WORKERS", "2")))
    app.extensions["trove.settings"] = settings_store

    # The studio's Offline toggle is ENFORCED by clip.llm.is_offline (SPOOL_OFFLINE) —
    # keep that single enforcement point in sync with the persisted setting. An env var
    # set at launch seeds the setting (the badge must reflect reality); from then on the
    # UI toggle drives the env. Single-process deploy, so process-env mutation is sound.
    def _apply_offline(values: dict) -> None:
        if values.get("offline"):
            os.environ["SPOOL_OFFLINE"] = "1"
        else:
            os.environ.pop("SPOOL_OFFLINE", None)
    if (os.environ.get("SPOOL_OFFLINE") or "").strip().lower() in ("1", "true", "yes", "on"):
        settings_store.update({"offline": True})
    _apply_offline(settings_store.get())
    app.extensions["trove.apply_settings"] = _apply_offline

    # FTS5 (trigram) transcript index — an additive accelerator for /transcripts/search (spec
    # §7.2). The in-memory word-scan stays the source of truth; this only narrows which
    # transcripts it must open. Inert (full-scan fallback) if FTS5/trigram isn't available.
    transcript_index = transcript_index_mod.TranscriptIndex(download_dir / "transcript_index.sqlite3")
    app.extensions["trove.transcript_index"] = transcript_index

    job_manager = JobManager(
        max_workers=effective_max_workers,
        ttl_seconds=job_ttl,
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
        ttl_seconds=job_ttl,
        store_path=download_dir / "transcribe_jobs.json",
    )
    app.extensions["trove.transcribe"] = transcribe_manager

    # Clip/render queue — trove's job machinery with new kinds (spec §1.1). The runner
    # holds the orchestration; the manager runs it. Both reachable from the v1 blueprint
    # and (later) the MCP server, so manual + agent mode drive the same engine + queue.
    clip_manager = clip_jobs.ClipJobManager(
        max_workers=effective_clip_workers,
        ttl_seconds=job_ttl,
        store_path=download_dir / "clip_jobs.json",
    )
    app.extensions["trove.clips"] = clip_manager
    # Brand kits — persisted reusable looks applied across a project's clips (spec §5 P2). Built
    # before the ClipRunner so the engine can resolve a recipe's/render's brand_kit_id into its
    # caption look (the same store the CRUD API mutates → manual + automated apply never diverge).
    brand_kit_store = brand_kits.BrandKitStore(download_dir / "brand_kits.json")
    app.extensions["trove.brand_kits"] = brand_kit_store
    clip_runner_inst = clip_runner.ClipRunner(
        download_dir=download_dir,
        job_manager=job_manager,
        clip_manager=clip_manager,
        settings_store=settings_store,
        brand_kits_store=brand_kit_store,
    )
    app.extensions["trove.clip_runner"] = clip_runner_inst
    # Recipes — saved end-to-end pipelines that drive render.pipeline + watch-folder (spec §5 P3).
    recipe_store = recipes.RecipeStore(download_dir / "recipes.json")
    app.extensions["trove.recipes"] = recipe_store
    # Watches — folder/channel/playlist automations (spec §5 P3). Reconciler wired below (needs the
    # download/import action closures); the poller (opt-in) is started after create_app finishes.
    watch_store = watches.WatchStore(download_dir / "watches.json")
    app.extensions["trove.watches"] = watch_store

    # Register the JSON v1 API blueprint — the headless surface for the studio + MCP.
    from routes.api_v1 import api_v1_bp
    app.register_blueprint(api_v1_bp)

    # TTL cleanup is history-only for every queue: it hides expired terminal
    # attempts without deleting identity or published artifacts.
    job_manager.start_sweeper(interval_seconds=300)
    transcribe_manager.start_sweeper(interval_seconds=300)
    clip_manager.start_sweeper(interval_seconds=300)

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

    def _build_transcribe_target(media_path: str, final_base_no_ext: str):
        """Return the per-transcribe ``_work(tj, *, model_path)`` closure.

        Extracted from api_transcribe_start so the auto-transcribe path
        (triggered from inside the download worker on success) can reuse
        the exact same body — extract → transcribe → diarize → artifacts
        with consistent cancel semantics and WAV cleanup.
        """
        def _work(tj, *, model_path, attempt=None):
            if attempt is None:
                attempt = tj._attempt
            staging_root = Path(tj._staging_root)
            staging_root.mkdir(parents=True, exist_ok=True)
            base_name = Path(final_base_no_ext).name
            staged_base = str(staging_root / base_name)
            wav_path = str(staging_root / f"{base_name}.wav")

            def _cancelled() -> bool:
                return transcribe_manager.attempt_cancelled(tj.id, tj, attempt)

            def _register_ffmpeg(proc):
                transcribe_manager.register_process(tj.id, tj, attempt, proc)

            try:
                # 1. Extract audio
                try:
                    transcriber.extract_audio(
                        media_path, wav_path,
                        cancel_check=_cancelled,
                        register_proc=_register_ffmpeg,
                    )
                except RuntimeError as e:
                    # extract_audio raises RuntimeError("cancelled") when the
                    # user hit cancel during ffmpeg — treat as a clean abort.
                    if str(e) == "cancelled" or _cancelled():
                        return
                    raise
                if _cancelled():
                    return
                transcribe_manager.update_progress(tj.id, tj, attempt, 5)

                # 2. Transcribe
                result = transcriber.run_transcribe(
                    audio_path=wav_path,
                    model_path=model_path,
                    progress_cb=lambda pct: transcribe_manager.update_progress(
                        tj.id, tj, attempt, pct,
                    ),
                    cancel_check=_cancelled,
                )
                if result.error == "cancelled" or _cancelled():
                    return
                if result.error:
                    error = RuntimeError(result.error)
                    error.error_category = "transcribe_error"
                    raise error

                diarization_status = None
                diarization_error = None
                speaker_count = None

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
                            _wav = diarizer._load_wav_16k(wav_path)
                            vad_regions = diarizer._vad_speech_chunks(_wav)
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
                            if _cancelled():
                                return
                            if chunks:
                                transcriber.apply_speakers(result, chunks)
                                diarization_status = "complete"
                                speaker_count = len({c.speaker for c in chunks})
                            else:
                                diarization_status = "empty"
                                speaker_count = 0
                        except Exception as e:
                            diarization_status = "failed"
                            diarization_error = str(e) or type(e).__name__
                            app.logger.warning("diarization failed: %s", e)
                except Exception as e:
                    # diarizer module itself didn't import — treat as not attempted.
                    app.logger.warning("diarizer unavailable: %s", e)

                if _cancelled():
                    return

                # 3. Write private candidates. The manager promotes these
                # exact sidecars only after it revalidates the captured attempt.
                transcriber.write_artifacts(result, staged_base)
                promotions = tuple(
                    Promotion(
                        Path(staged_base + suffix),
                        Path(final_base_no_ext + suffix),
                    )
                    for suffix in (".words.json", ".txt", ".srt", ".vtt")
                )

                def _index_committed(committed_job):
                    transcript_index_mod.index_words_file(
                        transcript_index,
                        committed_job.id,
                        final_base_no_ext + ".words.json",
                    )

                return AttemptOutcome(
                    updates={
                        "duration_seconds": result.duration,
                        "language_detected": result.language,
                        "diarization_status": diarization_status,
                        "diarization_error": diarization_error,
                        "speaker_count": speaker_count,
                    },
                    promotions=promotions,
                    after_commit=_index_committed,
                )
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

        Called only by the manager's guarded post-commit hook, after the
        staged media is published and the canonical Job is DONE.

        Degrades gracefully:
        - No active model installed → set ``_auto_transcribe_hint`` so
          the DONE card can render a "set up a model" link, then return.
        - A transcribe for this parent is already queued/running/done →
          no-op (idempotent; protects against double-fires).
        """
        if not parent.auto_transcribe or not parent.file_path:
            return
        if parent.status is not JobStatus.DONE or parent.dismissed_at is not None:
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
        try:
            transcribe_manager.submit(
                parent_job_id=parent.id,
                model_path=str(model_path),
                target=_build_transcribe_target(parent.file_path, base_no_ext),
            )
        except Exception as e:
            # Never let a transcribe-submit failure poison the download
            # job — it's an opportunistic add-on, not the core contract.
            app.logger.warning("auto-transcribe submit failed for %s: %s", parent.id, e)

    class _DownloadAttemptError(RuntimeError):
        def __init__(self, category: str, message: str):
            super().__init__(message)
            self.error_category = category

    def _build_download_target(
        *,
        url: str,
        format_choice: str,
        format_id,
        title: str,
        thumbnail: str,
        resolve_title: bool = False,
        subtitles: bool = False,
        chapters: bool = False,
        embed: bool = False,
    ):
        def _work(job: Job, *, attempt=None):
            if attempt is None:
                attempt = job._attempt
            out_template = job.out_template
            resolved_title = job.title or title
            resolved_thumbnail = job.thumbnail or thumbnail

            if resolve_title:
                # Bulk-submitted jobs defer title/thumbnail resolution here so
                # the request thread is never blocked on network metadata.
                try:
                    info = run_info(url)
                    if not info.error_category:
                        resolved_title = info.title or resolved_title
                        resolved_thumbnail = info.thumbnail or resolved_thumbnail
                except Exception:
                    pass

            def _on_progress(downloaded, total, speed, eta, frag_idx, frag_count):
                job_manager.update_progress(
                    job.id,
                    job,
                    attempt,
                    downloaded=downloaded,
                    total=total,
                    speed=speed,
                    eta=eta,
                    fragment_index=frag_idx,
                    fragment_count=frag_count,
                )

            def _register_proc(popen):
                job_manager.register_process(job.id, job, attempt, popen)

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
                if job_manager.attempt_cancelled(job.id, job, attempt):
                    return None
                raise _DownloadAttemptError(
                    result.error_category,
                    result.error_raw or result.error_category,
                )
            if not result.file_path:
                raise _DownloadAttemptError("unknown", "download returned no output file")

            staged_media = Path(result.file_path)
            staging_root = Path(job._staging_root)
            try:
                staged_media.relative_to(staging_root)
            except ValueError as exc:
                raise _DownloadAttemptError(
                    "unsafe_output", "download escaped its attempt staging root",
                ) from exc

            ext = staged_media.suffix
            final_media = download_dir / f"{job.id}{ext}"
            promotions = [Promotion(staged_media, final_media)]
            # Preserve user-requested subtitle/text sidecars without ever
            # publishing yt-dlp partial/intermediate media candidates.
            for candidate in staging_root.iterdir():
                if candidate == staged_media or not candidate.is_file():
                    continue
                if candidate.suffix.lower() in {".json", ".srt", ".vtt", ".txt", ".ass"}:
                    promotions.append(Promotion(candidate, download_dir / candidate.name))

            return AttemptOutcome(
                updates={
                    "title": resolved_title,
                    "thumbnail": resolved_thumbnail,
                    "format_choice": format_choice,
                    "format_id": format_id,
                    "file_path": str(staged_media),
                    "filename": sanitize_filename(resolved_title or title, ext),
                },
                promotions=tuple(promotions),
                after_commit=_try_auto_transcribe,
            )

        return _work

    def _enqueue_download(
        url: str, format_choice: str, format_id, title: str,
        thumbnail: str = "", *, auto_transcribe: bool = False,
        subtitles: bool = False, chapters: bool = False, embed: bool = False,
        resolve_title: bool = False,
    ) -> str:
        return job_manager.submit(
            target=_build_download_target(
                url=url,
                format_choice=format_choice,
                format_id=format_id,
                title=title,
                thumbnail=thumbnail,
                resolve_title=resolve_title,
                subtitles=subtitles,
                chapters=chapters,
                embed=embed,
            ),
            title=title,
            url=url,
            auto_transcribe=auto_transcribe,
            thumbnail=thumbnail,
            format_choice=format_choice,
            format_id=format_id,
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
        return job_manager.resume(
            job_id,
            target=_build_download_target(
                url=url,
                format_choice=format_choice,
                format_id=format_id,
                title=title,
                thumbnail=thumbnail,
            ),
        )

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
        return transcribe_manager.submit(
            parent_job_id=parent_job_id,
            model_path=str(model_path),
            target=_build_transcribe_target(media_path, base_no_ext),
        )

    def _import_local_file(path: str, *, auto_transcribe: bool = True) -> str:
        """Register a LOCAL video file as a source (the watch-folder ingest). Copies it into the
        download dir under the job id, marks the job done, and auto-transcribes — reusing the same
        job machinery as a download (no separate code path), so a watched folder yields sources
        identical to a paste-URL import."""
        title = os.path.basename(path)

        def _work(job: Job, *, attempt=None):
            if attempt is None:
                attempt = job._attempt
            ext = os.path.splitext(path)[1] or ".mp4"
            staged = Path(job._staging_root) / f"{job.id}{ext}"
            final = download_dir / f"{job.id}{ext}"
            # Hardlink (not copy) when on the same filesystem so a watched folder of
            # multi-GB videos isn't duplicated into downloads/ (spec §5 P3). The engine
            # only reads the source, so the user's original is never mutated.
            link_or_copy(path, str(staged))
            return AttemptOutcome(
                updates={"file_path": str(staged), "filename": sanitize_filename(title, ext)},
                promotions=(Promotion(staged, final),),
                after_commit=_try_auto_transcribe,
            )

        return job_manager.submit(target=_work, title=title, url=f"file://{path}",
                                  auto_transcribe=auto_transcribe)

    app.extensions["trove.actions"] = {
        "enqueue_download": _enqueue_download,
        "resume_job": _v1_resume_job,
        "start_transcribe": _v1_start_transcribe,
        "import_local_file": _import_local_file,
    }

    # --- watch-folder / channel automation reconciler (spec §5 P3) -------
    # Detect new videos → ingest (download+auto-transcribe for URLs, local import for folder files)
    # → once transcribed, run the watch's recipe (produce). The tick lives in watcher.py; here we
    # inject the real ingest/transcript/produce. Nothing is auto-published (Phase 4) — review gate.
    def _find_existing_source(url: str):
        """Cross-watch ingest dedup: a source already ingested under this canonical
        URL/path is reused instead of re-running download→transcribe→produce. The
        per-watch ``seen`` list can't see other watches; the job store can."""
        for j in job_manager.snapshot_jobs():
            if j.url == url and j.status not in (JobStatus.ERROR, JobStatus.CANCELLED):
                return j.id
        return None

    def _watch_ingest(watch: dict, key: str):
        try:
            if watch.get("kind") == "folder":
                path = os.path.join(watch.get("target", ""), key)
                existing = _find_existing_source(f"file://{path}")
                if existing:
                    return existing
                return _import_local_file(path, auto_transcribe=True)
            existing = _find_existing_source(key)
            if existing:
                return existing
            # ``key`` is a canonical video URL (watcher.list_playlist_items prints ``url``).
            # Resolve its real title/thumbnail up front — same as a paste-URL import (the
            # _start_download path) — so a watched channel/playlist yields sources identical to
            # manual imports rather than ones titled with a raw URL. Best-effort: fall back to
            # the URL if the metadata probe fails (offline / 4xx), never blocking the ingest.
            title, thumbnail = key, ""
            try:
                info = run_info(key)
                if not info.error_category:
                    title = info.title or key
                    thumbnail = info.thumbnail or ""
            except Exception:
                pass
            return _enqueue_download(url=key, format_choice="video", format_id=None,
                                     title=title, thumbnail=thumbnail, auto_transcribe=True)
        except Exception:
            app.logger.warning("watch ingest failed for %r", key, exc_info=True)
            return None

    def _watch_transcript_done(source_id: str) -> bool:
        _, words_path = clip_runner_inst.source_paths(source_id)
        return bool(words_path and os.path.exists(words_path))

    def _watch_produce(watch: dict, source_id: str) -> str:
        recipe = {**(recipe_store.get(watch.get("recipe_id")) or {}), "watch_id": watch.get("id")}
        return clip_manager.submit(
            kind="produce", source_id=source_id,
            params={"recipe_id": watch.get("recipe_id"), "watch_id": watch.get("id")},
            target=clip_runner_inst.produce_target(source_id=source_id, recipe=recipe))

    def _watch_produce_status(job_id):
        """Honest produce completion for the reconciler: produce-done == renders-done.

        A produce job flips to DONE the instant it has ENQUEUED its per-moment render (pipeline)
        jobs — long before any of them finishes. So we can't trust the produce job's own DONE; we
        roll up the CHILD render jobs it recorded in ``result['clip_jobs']`` (each looked up via the
        same clip job manager) into one terminal status the watcher acts on. This is a status rollup,
        NOT a join — it never blocks a worker thread; the reconciler simply re-checks on later ticks
        until the children settle. Returns:
          - the produce job's OWN status if that is terminal-bad (``error``/``cancelled``) — honoured
            directly so a failed/cancelled produce is retried/abandoned per the watcher's rules;
          - ``done``  iff >= 1 child render reached DONE (at least one clip actually landed);
          - ``error`` if every child ERRORED, OR the produce job is DONE with ZERO children (the
            empty-candidates case — no moments → no clips → not a real "produced"; the bounded watch
            retry re-attempts a few ticks then abandons);
          - ``running`` otherwise (children still in flight). None if the produce job is gone."""
        cj = clip_manager.get(job_id) if job_id else None
        if cj is None:
            return None
        own = cj.status.value
        if own in ("error", "cancelled"):
            return own
        children = (cj.result or {}).get("clip_jobs") or []
        if own == "done" and not children:
            return "error"            # DONE but produced nothing (empty candidates) — not "produced"
        if not children:
            return "running"          # produce still finding/ranking; no children enqueued yet
        states = [clip_manager.get(c) for c in children]
        statuses = [s.status.value for s in states if s is not None]
        if any(st == "done" for st in statuses):
            return "done"             # at least one render landed → honestly produced
        if statuses and all(st == "error" for st in statuses):
            return "error"            # every render failed → retry the whole produce
        return "running"              # some children still queued/running

    def _watch_items(watch: dict):
        if watch.get("kind") == "folder":
            return watcher.list_folder_items(watch.get("target", ""))
        return watcher.list_playlist_items(watch.get("target", ""))

    # Per-watch locks so the background poller and concurrent /scan threads can't double-ingest the
    # SAME watch (its get→reconcile→set_state was a read-modify-write race: last-writer-wins lost
    # state + duplicate ingests). PER-watch (not one global lock) because a single reconcile can
    # block ~90s on yt-dlp — unrelated watches must still reconcile in parallel. A small meta-lock
    # guards the lock dict itself. Single-process deploy, so an in-process lock is sufficient.
    _watch_locks: dict[str, threading.Lock] = {}
    _watch_locks_meta = threading.Lock()

    def _watch_lock(watch_id: str) -> threading.Lock:
        with _watch_locks_meta:
            lk = _watch_locks.get(watch_id)
            if lk is None:
                lk = _watch_locks[watch_id] = threading.Lock()
            return lk

    def _reconcile_one(watch: dict) -> dict:
        # Re-read the latest persisted state UNDER the lock so a concurrent tick on the same watch
        # can't clobber our get→reconcile→set_state with a stale snapshot.
        with _watch_lock(watch["id"]):
            fresh = watch_store.get(watch["id"]) or watch
            r = watcher.reconcile_watch(fresh, list_items=_watch_items, ingest=_watch_ingest,
                                        transcript_done=_watch_transcript_done, produce=_watch_produce,
                                        produce_status=_watch_produce_status)
            watch_store.set_state(fresh["id"], seen=r["seen"], pending=r["pending"],
                                  produced=r["produced"], producing=r["producing"],
                                  ingesting=r["ingesting"])
        return r

    def _reconcile_watch_by_id(watch_id: str):
        w = watch_store.get(watch_id)
        if w is None:
            return None
        # Manual scan = explicit "look again now": bypass the listing TTL for this target.
        watcher.invalidate_listing(w.get("target"))
        return _reconcile_one(w)

    def _reconcile_all() -> None:
        for w in watch_store.list():
            if w.get("enabled", True):
                try:
                    _reconcile_one(w)
                except Exception:
                    app.logger.warning("watch reconcile failed for %s", w.get("id"), exc_info=True)

    app.extensions["trove.watch_reconcile"] = _reconcile_watch_by_id
    app.extensions["trove.watch_reconcile_all"] = _reconcile_all

    # Opt-in background poller: SPOOL_WATCH_INTERVAL seconds (0/unset = off — manual "Scan now"
    # always works). A daemon thread so it never blocks shutdown.
    try:
        _watch_interval = float(os.environ.get("SPOOL_WATCH_INTERVAL", "0") or 0)
    except ValueError:
        _watch_interval = 0.0
    if _watch_interval > 0:
        def _poll():
            while True:
                _time.sleep(_watch_interval)
                _reconcile_all()
        threading.Thread(target=_poll, name="watch-poller", daemon=True).start()

    return app


if __name__ == "__main__":
    from config import DEFAULT_HOST, DEFAULT_PORT, assert_safe_bind
    # Refuse to start on a public bind without auth — see config.py.
    assert_safe_bind(DEFAULT_HOST)
    app = create_app()
    app.run(host=DEFAULT_HOST, port=DEFAULT_PORT)
