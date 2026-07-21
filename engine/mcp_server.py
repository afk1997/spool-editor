"""``trove-mcp`` — MCP (Model Context Protocol) server for Trove.

Exposes the Trove HTTP API as a set of MCP tools for coding agents
(Claude Desktop, Cursor, Replit Agent, etc.). During Phase 0 all
schemas remain advertised for compatibility, but the runtime allows
only explicit read-only inspection tools. Manual UI, REST, and CLI
mutations remain available.

Transport: stdio (the default for desktop MCP clients).

Configuration (env vars):
    TROVE_URL    Base URL of the Trove server (default localhost:8899).
    TROVE_TOKEN  Bearer token if the server was started with one.

Usage in a client config (Claude Desktop / Cursor):
    {
      "mcpServers": {
        "trove": {
          "command": "trove-mcp",
          "env": { "TROVE_URL": "http://127.0.0.1:8899" }
        }
      }
    }

The server expects the Trove HTTP server to already be running. Each
tool returns a clear error if the server is unreachable so the agent
knows to prompt the user to start it (``trove serve``).
"""
from __future__ import annotations

import os
import sys

# Depend on the shared client, NOT on cli.py — the MCP server should
# never inherit CLI-specific behavior (terminal formatting, exit
# semantics, banners, argparse assumptions, stdout/stderr conventions).
from trove_client import TroveClient, TroveError


# Module-level client. Properties read TROVE_URL / TROVE_TOKEN at call
# time so a re-export of the env var (e.g. via the host MCP client
# config) takes effect on the next tool call without rebuilding.
_client = TroveClient()

READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "get_clip_job",
    "get_job",
    "get_recipe",
    "get_settings",
    "get_transcript",
    "get_transcript_chunk",
    "get_transcript_status",
    "get_watch",
    "list_brand_kits",
    "list_clip_jobs",
    "list_jobs",
    "list_models",
    "list_recipes",
    "list_transcripts",
    "list_watches",
    "model_install_progress",
    "rank_candidates",
    "search_transcripts",
    "server_capabilities",
    "source_energy",
    "source_filmstrip",
    "source_scenes",
    "storage_info",
})

MUTATION_DISABLED_ERROR = {
    "error": "agent_mutation_disabled",
    "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
}


def _guard_tool(tool_name: str) -> dict | None:
    """Return the Phase 0 disabled envelope before a non-read tool can do any work."""
    if tool_name not in READ_ONLY_TOOLS:
        return dict(MUTATION_DISABLED_ERROR)
    return None


def _safe(tool_name, call):
    """Wrap a TroveError into an MCP-friendly ``{error: str}`` dict so
    the agent always gets a machine-readable response, never a stack
    trace. The named Phase 0 guard runs before ``call`` is evaluated,
    so a disabled schema can never reach TroveClient. Any other
    exception bubbles up to the SDK, which already serializes it as a
    tool error."""
    disabled = _guard_tool(tool_name)
    if disabled:
        return disabled
    try:
        return call()
    except TroveError as e:
        msg = e.body.get("error") if isinstance(e.body, dict) else str(e.body)
        return {"error": msg, "status": e.status}
    except SystemExit as e:
        return {"error": str(e)}


def _build_server():
    """Construct the FastMCP server with all tools + resources.

    Lazy-imported so ``import mcp_server`` never fails when the
    optional ``mcp`` SDK isn't installed — only ``main()`` requires
    it. This lets the test suite import the module to inspect tool
    metadata without forcing the dep.
    """
    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError as e:
        raise SystemExit(
            "trove-mcp: the 'mcp' package is required.\n"
            "  Install with: pip install 'trove[mcp]'  (or just: pip install mcp)"
        ) from e

    # ``from __future__ import annotations`` stringizes tool signatures, and FastMCP
    # eval()s them against this module's globals. ``Context`` is imported lazily (so the
    # module stays importable without the SDK), so publish it into globals for the eval.
    globals()["Context"] = Context

    mcp = FastMCP("trove")

    # ---- jobs (downloads) -------------------------------------------

    @mcp.tool()
    def list_jobs(
        status: str = "",
        limit: int = 100,
        offset: int = 0,
        order: str = "newest",
    ) -> dict:
        """List download jobs (paginated, filterable).

        Args:
            status: Comma-separated status filter (e.g. ``"done,error"``).
                Empty string returns all jobs.
            limit: 1-500, default 100.
            offset: Skip this many jobs (use with ``limit`` to page).
            order: ``"newest"`` (default) or ``"oldest"``.

        Returns ``{jobs, total, returned, limit, offset}``.
        """
        return _safe("list_jobs", lambda: _client.list_jobs(
            status=status, limit=limit, offset=offset, order=order))

    @mcp.tool()
    def get_job(job_id: str) -> dict:
        """Get the current state of one download job.

        Returns rich progress data so you can give the user a useful
        live update on every poll:

        - ``status``: queued / downloading / paused / done / error / cancelled
        - ``progress_pct`` (0-100), ``downloaded_bytes``, ``total_bytes``
        - ``speed_bps`` (bytes/sec), ``eta_seconds``, ``elapsed_seconds``
        - ``fragment_index`` / ``fragment_count`` for HLS/DASH streams
        - ``human``: pre-formatted strings — ``progress`` (``"42%"``),
          ``downloaded`` (``"12.4 MB"``), ``size``, ``speed``
          (``"5.2 MB/s"``), ``eta`` (``"0:03"``), ``elapsed``, plus a
          ``summary`` one-liner you can paste straight into a reply
          (e.g. ``"downloading · 42% · 12.4 MB / 29.7 MB · 5.2 MB/s · ETA 0:03"``).
        """
        return _safe("get_job", lambda: _client.get_job(job_id))

    @mcp.tool()
    def bulk_download(
        urls: list[str],
        format: str = "video",
        auto_transcribe: bool = False,
    ) -> dict:
        """Queue many downloads in one call.

        Args:
            urls: List of source URLs (max 100).
            format: ``"video"`` or ``"audio"`` — applied to all.
            auto_transcribe: Trigger transcription on each successful download.

        Returns ``{submitted, failed, results}``. Each ``results`` entry
        is either ``{url, id, title}`` (success) or ``{url, error}`` (failure)
        — partial failures don't fail the whole call.
        """
        return _safe("bulk_download", lambda: _client.bulk_download(
            urls, fmt=format, auto_transcribe=auto_transcribe))

    @mcp.tool()
    def storage_info() -> dict:
        """Disk-usage report for the download directory.

        Returns total bytes, file count, per-job breakdown
        (``by_job``, sorted biggest first) and any orphan files left
        behind by crashes.
        """
        return _safe("storage_info", lambda: _client.storage_info())

    @mcp.tool()
    def download_media(
        url: str,
        format: str = "video",
        auto_transcribe: bool = False,
        title: str = "",
    ) -> dict:
        """Queue a new media download.

        Args:
            url: The source URL (YouTube, Vimeo, anything yt-dlp supports).
            format: ``"video"`` (mp4) or ``"audio"`` (mp3).
            auto_transcribe: Trigger transcription on success when an
                active model is installed.
            title: Optional override; defaults to the source title.
        """
        return _safe("download_media", lambda: _client.submit_download(
            url, fmt=format, auto_transcribe=auto_transcribe,
            title=title or None))

    @mcp.tool()
    def pause_download(job_id: str) -> dict:
        """Pause an in-flight download. The .part file is preserved."""
        return _safe("pause_download", lambda: _client.pause_job(job_id))

    @mcp.tool()
    def resume_download(job_id: str) -> dict:
        """Resume a paused download (re-uses the persisted format/url)."""
        return _safe("resume_download", lambda: _client.resume_job(job_id))

    @mcp.tool()
    def cancel_download(job_id: str) -> dict:
        """Cancel a download. Removes any partial output."""
        return _safe("cancel_download", lambda: _client.cancel_job(job_id))

    @mcp.tool()
    def dismiss_download(job_id: str) -> dict:
        """Mark a terminal download hidden in history. Managed media remains on disk."""
        r = _safe("dismiss_download", lambda: _client.dismiss_job(job_id))
        return {"ok": True, "job_id": job_id} if r is None else r

    # ---- transcripts ------------------------------------------------

    @mcp.tool()
    def list_transcripts(
        status: str = "",
        limit: int = 100,
        offset: int = 0,
        order: str = "newest",
    ) -> dict:
        """List transcribe jobs (paginated, filterable).

        Same paging semantics as ``list_jobs``.
        """
        return _safe("list_transcripts", lambda: _client.list_transcripts(
            status=status, limit=limit, offset=offset, order=order))

    @mcp.tool()
    def search_transcripts(query: str, limit: int = 50, context: int = 60) -> dict:
        """Substring-search across all completed transcripts.

        Args:
            query: The phrase to find (case-insensitive).
            limit: Max matches to return (1-200).
            context: Characters of surrounding context per match.

        Returns ``{query, matches, returned}``. Each match has
        ``transcript_id``, ``parent_job_id``, ``title``, ``snippet``,
        ``start_seconds`` and ``end_seconds`` so the agent can deep-link.
        """
        return _safe("search_transcripts", lambda: _client.search_transcripts(
            query, limit=limit, context=context))

    @mcp.tool()
    def get_transcript_status(transcript_id: str) -> dict:
        """Get the lifecycle state + progress of one transcribe job.

        Returns: ``status`` (queued/running/done/error/cancelled),
        ``progress_pct``, ``elapsed_seconds``, ``duration_seconds``
        (length of the source audio), ``language_detected``,
        ``model_used``, plus a ``human`` block with pre-formatted
        ``progress``, ``elapsed``, ``audio_duration`` and a
        ``summary`` one-liner (e.g. ``"running · 42% · of 9:12 audio
        · elapsed 1:08 · model=ggml-tiny.bin"``).
        """
        return _safe("get_transcript_status", lambda: _client.get_transcript_status(transcript_id))

    @mcp.tool()
    def transcribe(parent_job_id: str) -> dict:
        """Kick off transcription for a downloaded clip.

        Idempotent — if a transcribe is already running for this clip,
        returns the existing one instead of starting a duplicate.
        Requires an active whisper model (use ``install_model`` /
        ``set_active_model`` first if needed).
        """
        return _safe("transcribe", lambda: _client.transcribe(parent_job_id))

    @mcp.tool()
    def cancel_transcribe(transcript_id: str) -> dict:
        """Cancel an in-flight transcribe job."""
        return _safe("cancel_transcribe", lambda: _client.cancel_transcribe(transcript_id))

    @mcp.tool()
    def get_transcript_chunk(transcript_id: str, format: str = "txt",
                             offset: int = 0, limit: int = 0) -> dict:
        """Read a slice of a finished transcript (paginated).

        Designed for context-bounded LLM callers: a 90-minute podcast
        transcript easily exceeds the per-tool reply budget, so this
        returns one page at a time and the agent stitches them.

        Args:
            transcript_id: The transcript id from ``list_transcripts``.
            format: ``txt|srt|vtt`` slice by *byte* offset (matches the
                bytes the export endpoint would serve); ``json`` slices
                by *segment* index over the v2 schema.
            offset: Where to start the page (byte or segment index per
                ``format``). 0 starts from the beginning.
            limit: Page size. ``0`` (the default) lets the server pick:
                4000 bytes for text, 50 segments for json. Capped at
                64000 bytes / 500 segments server-side.

        Returns:
            ``{format, offset, limit, returned, total, has_more, ...}``
            with ``content`` for text formats or ``segments`` + ``words``
            (filtered to those referenced by the returned segments) for
            ``json``. Loop while ``has_more`` is true, advancing
            ``offset`` by ``returned`` each call.
        """
        if format not in {"txt", "srt", "vtt", "json"}:
            return {"error": "format must be txt|srt|vtt|json"}
        kw = {"offset": offset}
        if limit:
            kw["limit"] = limit
        return _safe("get_transcript_chunk", lambda: _client.get_transcript_chunk(
            transcript_id, format, **kw))

    @mcp.tool()
    def get_transcript(transcript_id: str, format: str = "txt") -> dict:
        """Fetch a finished transcript.

        Args:
            transcript_id: The transcript id from ``list_transcripts``.
            format: One of ``"txt"`` (plain), ``"srt"`` (subtitles),
                ``"vtt"`` (web subtitles), or ``"json"`` (raw v2 schema
                with word-level timing — useful for programmatic edits).

        Returns:
            ``{format, content}`` for txt/srt/vtt; the parsed JSON tree
            for ``"json"``.
        """
        if format not in {"txt", "srt", "vtt", "json"}:
            return {"error": "format must be txt|srt|vtt|json"}
        body = _safe("get_transcript", lambda: _client.export_transcript(transcript_id, format))
        if isinstance(body, dict) and body.get("error"):
            return body
        if format == "json":
            return body if isinstance(body, dict) else {"error": "unexpected_response"}
        return {"format": format, "content": body}

    # ---- meta -------------------------------------------------------

    @mcp.tool()
    def server_capabilities() -> dict:
        """Probe what the connected Trove server supports.

        Returns the feature / limit / scope registry — useful for an
        agent to decide whether diarization is available, what the
        chunk-size caps are, whether the server requires a bearer
        token, and which transcript export formats are supported.
        Safe to call without authentication.
        """
        return _safe("server_capabilities", lambda: _client.capabilities())

    # ---- models -----------------------------------------------------

    @mcp.tool()
    def list_models() -> dict:
        """List known whisper models with installed/active state."""
        return _safe("list_models", lambda: _client.list_models())

    @mcp.tool()
    def install_model(name: str) -> dict:
        """Start downloading a whisper model from HuggingFace.

        Background operation — poll ``model_install_progress`` for status.
        Names are e.g. ``"ggml-tiny.bin"``, ``"ggml-base.bin"``,
        ``"ggml-small.bin"``, ``"ggml-medium.bin"``.
        """
        return _safe("install_model", lambda: _client.install_model(name))

    @mcp.tool()
    def model_install_progress() -> dict:
        """Get the current model-install download progress."""
        return _safe("model_install_progress", lambda: _client.model_install_progress())

    @mcp.tool()
    def set_active_model(name: str) -> dict:
        """Mark an installed model as the active one (used for new transcribes)."""
        return _safe("set_active_model", lambda: _client.set_active_model(name))

    @mcp.tool()
    def remove_model(name: str) -> dict:
        """Delete an installed model from disk."""
        r = _safe("remove_model", lambda: _client.remove_model(name))
        return {"ok": True, "name": name} if r is None else r

    # ---- clips (the render queue) -----------------------------------
    # Same delegation pattern as the trove tools: each calls the shared client →
    # the same /api/v1 surface → the same engine + job store the studio uses (the
    # golden rule). Clip ops are async jobs: the tool returns a clip-job view; poll
    # get_clip_job (or the spool://clips resource) for staged progress + result.

    from pydantic import BaseModel, Field  # ships with the mcp SDK

    class _ReframeChoice(BaseModel):
        aspect: str = Field(default="9:16", description="Aspect ratio: 9:16, 16:9, 1:1, or 4:5")
        mode: str = Field(default="pan", description="Reframe mode: pan (speaker pan), split, or center")

    @mcp.tool()
    def find_moments(source_id: str, mode: str = "funny", count: int = 5) -> dict:
        """Find clip-worthy moments in a source's transcript via the moment-finding LLM
        (default: the codex bridge — the user's ChatGPT/Codex subscription, no key/GPU).

        ``mode`` ∈ funny / insightful / hot-take / story / how-to / q&a. Returns a
        clip-job; poll ``get_clip_job`` for ``result.candidates`` —
        ``[{start, end, title, rationale, signals}]``. Only transcript text egresses.
        """
        return _safe("find_moments", lambda: _client.find_moments(source_id, mode=mode, count=count))

    @mcp.tool()
    def rank_candidates(source_id: str, candidates: list[dict],
                        weights: dict | None = None) -> dict:
        """Re-rank candidates with the glass-box opportunity score (discover.rank).

        Pass the ``candidates`` from a prior ``find_moments`` (each carrying its ``features``)
        and optional factor ``weights`` — a dict over ``hook / self_contained / arc / energy /
        length_fit`` (need not sum to 1). Returns ``{candidates, count, weights}`` re-scored and
        sorted best-first. The score is a transparent weighted sum of those named factors, so
        every candidate's ``factors`` + ``score`` explain the ordering (no opaque 0–99)."""
        return _safe("rank_candidates", lambda: _client.rank_candidates(source_id, candidates, weights=weights))

    @mcp.tool()
    def cut_clip(source_id: str, start: float, end: float) -> dict:
        """Cut a clip ``[start, end]`` (seconds) from a source. The clip-job's
        ``result.clip_id`` drives the subsequent reframe/caption/render calls."""
        return _safe("cut_clip", lambda: _client.cut_clip(source_id, start=start, end=end))

    @mcp.tool()
    async def reframe_clip(clip_id: str, aspect: str | None = None,
                           mode: str | None = None, ctx: Context = None) -> dict:
        """Reframe a clip to a target aspect with the diar⊕ROI speaker pan.

        If ``aspect``/``mode`` are omitted, the server **elicits** the choice from the
        user (aspect ∈ 9:16/16:9/1:1/4:5; mode ∈ pan/split/center) — the spec's
        human-judgment pause. Clients without elicitation support fall back to defaults.
        """
        disabled = _guard_tool("reframe_clip")
        if disabled:
            return disabled
        if (aspect is None or mode is None) and ctx is not None:
            try:
                res = await ctx.elicit(
                    "Choose the reframe aspect ratio and mode for this clip.",
                    schema=_ReframeChoice,
                )
                if res.action == "accept" and res.data is not None:
                    aspect = aspect or res.data.aspect
                    mode = mode or res.data.mode
            except Exception:
                pass  # elicitation unsupported / failed → fall through to defaults
        return _safe("reframe_clip", lambda: _client.reframe_clip(
            clip_id, aspect=aspect or "9:16", mode=mode or "pan"))

    @mcp.tool()
    def caption_clip(clip_id: str, style: str = "opus") -> dict:
        """Generate + burn styled captions (``style`` ∈ opus/karaoke/minimal), sliced to
        the clip window from the source transcript — no re-transcribe."""
        return _safe("caption_clip", lambda: _client.caption_clip(clip_id, style=style))

    @mcp.tool()
    def render_clip(clip_id: str, preset: str = "tiktok", fast: bool = True) -> dict:
        """Export the clip to a platform preset (tiktok/reels/shorts/youtube/linkedin/x)
        at -14 LUFS. ``result.render_id`` identifies the produced .mp4."""
        return _safe("render_clip", lambda: _client.render_clip(clip_id, preset=preset, fast=fast))

    @mcp.tool()
    def render_pipeline(source_id: str, start: float, end: float, aspect: str = "9:16",
                        mode: str = "pan", style: str = "opus", preset: str = "tiktok") -> dict:
        """One-shot cut→reframe→caption→export of a source window into a finished vertical
        clip. Returns a clip-job; poll ``get_clip_job`` for staged progress + ``result``
        (clip_id, render_id, output_path)."""
        return _safe("render_pipeline", lambda: _client.render_pipeline(
            source_id, start=start, end=end, aspect=aspect, mode=mode,
            style=style, preset=preset))

    @mcp.tool()
    def list_clip_jobs(kind: str = "", status: str = "", limit: int = 100) -> dict:
        """List clip/render jobs (the render queue). Filter by ``kind`` (moments/cut/
        reframe/caption/export/pipeline) and/or ``status`` (comma-separated)."""
        return _safe("list_clip_jobs", lambda: _client.list_clip_jobs(kind=kind, status=status, limit=limit))

    @mcp.tool()
    def get_clip_job(job_id: str) -> dict:
        """Get one clip/render job — status, staged progress, and ``result`` (candidates
        for moments jobs; clip_id / render_id / output_path for the rest)."""
        return _safe("get_clip_job", lambda: _client.get_clip_job(job_id))

    @mcp.tool()
    def cancel_clip_job(job_id: str) -> dict:
        """Cancel a queued/running clip job (kills the underlying ffmpeg)."""
        return _safe("cancel_clip_job", lambda: _client.cancel_clip_job(job_id))

    @mcp.tool()
    def dismiss_clip_job(job_id: str) -> dict:
        """Mark a finished clip/render job hidden in history. Managed media remains on disk."""
        r = _safe("dismiss_clip_job", lambda: _client.dismiss_clip_job(job_id))
        return {"ok": True, "job_id": job_id} if r is None else r

    # ---- automation: produce / recipes / watches / brand kits (Phase 3) ----
    # Same delegation pattern → the agent drives the SAME /api/v1 automation the studio does, so
    # agent mode and manual mode never diverge (the golden rule, now for the Phase-3 surface too).

    @mcp.tool()
    def produce_clips(source_id: str, recipe_id: str = "", recipe: dict | None = None) -> dict:
        """Apply a recipe to a source end-to-end → the review queue: find moments → glass-box rank →
        take the top N → run a full cut→reframe→caption→export pipeline per moment with the recipe's
        aspect/reframe/caption/brand-kit/platform. Pass a saved ``recipe_id`` (from ``list_recipes``)
        OR an inline ``recipe`` dict (content_mode/count/aspect/reframe_mode/caption_preset/platform/
        fast/weights/brand_kit_id). Returns a produce clip-job; poll ``get_clip_job`` for the fan-out.
        Clips are NOT published (Phase 4) — they land for review (the honest gate)."""
        # Strip the reserved (non-inline) keys so they can't collide with the positional
        # ``source_id`` / keyword ``recipe_id`` in the splat (TypeError: multiple values).
        recipe = {k: v for k, v in (recipe or {}).items() if k not in ("source_id", "recipe_id")}
        return _safe("produce_clips", lambda: _client.produce(source_id, recipe_id=recipe_id or None, **recipe))

    @mcp.tool()
    def list_recipes() -> dict:
        """List saved recipes (a recipe = the reusable pipeline decisions: content mode + count,
        ranking weights, render settings). Use a recipe's ``id`` with ``produce_clips``."""
        return _safe("list_recipes", lambda: _client.list_recipes())

    @mcp.tool()
    def get_recipe(recipe_id: str) -> dict:
        """Get one saved recipe by id."""
        return _safe("get_recipe", lambda: _client.get_recipe(recipe_id))

    @mcp.tool()
    def create_recipe(recipe: dict) -> dict:
        """Create a saved recipe. Fields: name, content_mode (funny/insightful/hot-take/story/how-to/
        q&a), count, aspect (9:16/16:9/1:1/4:5), reframe_mode (pan/split/center), caption_preset
        (opus/karaoke/minimal), platform (tiktok/reels/shorts/youtube/linkedin/x), fast (bool),
        brand_kit_id, weights (dict over hook/self_contained/arc/energy/length_fit)."""
        return _safe("create_recipe", lambda: _client.create_recipe(recipe))

    @mcp.tool()
    def update_recipe(recipe_id: str, changes: dict) -> dict:
        """Patch a saved recipe with the changed fields (same fields as create_recipe)."""
        return _safe("update_recipe", lambda: _client.update_recipe(recipe_id, changes))

    @mcp.tool()
    def delete_recipe(recipe_id: str) -> dict:
        """Delete a saved recipe."""
        r = _safe("delete_recipe", lambda: _client.delete_recipe(recipe_id))
        return {"ok": True, "recipe_id": recipe_id} if r is None else r

    @mcp.tool()
    def list_watches() -> dict:
        """List folder/channel/playlist watches (new videos auto-produce ranked clips per a recipe
        into the review queue). Each shows its seen/pending/producing/produced reconcile state."""
        return _safe("list_watches", lambda: _client.list_watches())

    @mcp.tool()
    def get_watch(watch_id: str) -> dict:
        """Get one watch by id."""
        return _safe("get_watch", lambda: _client.get_watch(watch_id))

    @mcp.tool()
    def create_watch(watch: dict) -> dict:
        """Create a watch. Fields: name, kind (folder/channel/playlist), target (a local folder path
        or a channel/playlist URL), recipe_id (the recipe to produce with), enabled (bool)."""
        return _safe("create_watch", lambda: _client.create_watch(watch))

    @mcp.tool()
    def update_watch(watch_id: str, changes: dict) -> dict:
        """Patch a watch (name/kind/target/recipe_id/enabled)."""
        return _safe("update_watch", lambda: _client.update_watch(watch_id, changes))

    @mcp.tool()
    def delete_watch(watch_id: str) -> dict:
        """Delete a watch."""
        r = _safe("delete_watch", lambda: _client.delete_watch(watch_id))
        return {"ok": True, "watch_id": watch_id} if r is None else r

    @mcp.tool()
    def scan_watch(watch_id: str) -> dict:
        """Reconcile a watch now: detect new videos → ingest → produce per its recipe once
        transcribed. Returns this tick's ingested / producing / produced source ids."""
        return _safe("scan_watch", lambda: _client.scan_watch(watch_id))

    @mcp.tool()
    def list_brand_kits() -> dict:
        """List brand kits (a reusable look: caption preset + overrides + watermark + lower-third)."""
        return _safe("list_brand_kits", lambda: _client.list_brand_kits())

    @mcp.tool()
    def create_brand_kit(kit: dict) -> dict:
        """Create a brand kit. Fields: name, palette (list of hex), caption_preset, caption_overrides
        (dict), watermark (str), lower_third (str), fonts. Reference its id from a recipe's brand_kit_id."""
        return _safe("create_brand_kit", lambda: _client.create_brand_kit(kit))

    @mcp.tool()
    def update_brand_kit(kit_id: str, changes: dict) -> dict:
        """Patch a brand kit (same fields as create_brand_kit)."""
        return _safe("update_brand_kit", lambda: _client.update_brand_kit(kit_id, changes))

    @mcp.tool()
    def delete_brand_kit(kit_id: str) -> dict:
        """Delete a brand kit."""
        r = _safe("delete_brand_kit", lambda: _client.delete_brand_kit(kit_id))
        return {"ok": True, "kit_id": kit_id} if r is None else r

    # ---- settings / transcript-edit / timeline signals (UI<->MCP<->CLI parity) ----
    # These reach engine surfaces the studio uses (writable settings, word editing, the editor
    # timeline's energy/scenes/filmstrip lanes) so an agent has the SAME reach as the UI.

    @mcp.tool()
    def get_settings() -> dict:
        """Read writable engine config (render defaults: fast/preset/aspect, offline egress switch, concurrency, MCP transport)."""
        return _safe("get_settings", lambda: _client.get_settings())

    @mcp.tool()
    def update_settings(changes: dict) -> dict:
        """Patch writable engine config (the changed keys only)."""
        return _safe("update_settings", lambda: _client.update_settings(changes))

    @mcp.tool()
    def edit_word(transcript_id: str, word_index: int, op: str, w: str = "") -> dict:
        """Edit ONE transcript word in place — op = set_text|delete|insert_after|merge_next (``w``
        required for set_text/insert_after). Re-renders srt/vtt/txt + re-indexes. Fix a misheard word
        before captioning."""
        return _safe("edit_word", lambda: _client.edit_word(transcript_id, word_index, op, w=w or None))

    @mcp.tool()
    def dismiss_transcribe(transcript_id: str) -> dict:
        """Mark a finished transcribe job hidden in history. Managed files remain on disk."""
        r = _safe("dismiss_transcribe", lambda: _client.dismiss_transcribe(transcript_id))
        return {"ok": True, "transcript_id": transcript_id} if r is None else r

    @mcp.tool()
    def source_energy(source_id: str, buckets: int = 96, start: float | None = None,
                      end: float | None = None) -> dict:
        """Normalized 0..1 loudness envelope (the audio-energy waveform); optional start/end window it."""
        return _safe("source_energy", lambda: _client.source_energy(
            source_id, buckets=buckets, start=start, end=end, use_cache=False,
        ))

    @mcp.tool()
    def source_scenes(source_id: str, start: float, end: float) -> dict:
        """Scene-cut timestamps within [start, end] (the editor timeline's Scenes lane)."""
        return _safe("source_scenes", lambda: _client.source_scenes(source_id, start=start, end=end))

    @mcp.tool()
    def source_filmstrip(source_id: str, start: float, end: float, frames: int = 12) -> dict:
        """Thumbnail filmstrip data-URI across [start, end] (the editor timeline's Video lane)."""
        return _safe("source_filmstrip", lambda: _client.source_filmstrip(
            source_id, start=start, end=end, frames=frames, use_cache=False,
        ))

    @mcp.tool()
    def download_render(clip_id: str, render_id: str, save_to: str) -> dict:
        """Save a finished render .mp4 to a local path (the produced clip bytes)."""
        return _safe("download_render", lambda: _client.download_render(clip_id, render_id, stream_to=save_to))

    # ---- resources --------------------------------------------------
    # Resources let the agent surface live application state to the user
    # without spending tool-call budget on plain reads.

    @mcp.resource("trove://jobs")
    def jobs_resource() -> str:
        import json as _json
        return _json.dumps(_safe("list_jobs", lambda: _client.list_jobs()), indent=2)

    @mcp.resource("trove://transcripts")
    def transcripts_resource() -> str:
        import json as _json
        return _json.dumps(_safe("list_transcripts", lambda: _client.list_transcripts()), indent=2)

    @mcp.resource("trove://transcript/{tid}")
    def transcript_resource(tid: str) -> str:
        import json as _json
        body = _safe("get_transcript", lambda: _client.export_transcript(tid, "json"))
        return _json.dumps(body, indent=2) if isinstance(body, dict) else str(body)

    @mcp.resource("trove://transcript/{tid}/text")
    def transcript_text_resource(tid: str) -> str:
        """Plain-text export of a transcript — handy for the agent to
        ingest as a single string without parsing the v2 JSON tree."""
        body = _safe("get_transcript", lambda: _client.export_transcript(tid, "txt"))
        if isinstance(body, dict) and body.get("error"):
            return f"(error: {body['error']})"
        return body if isinstance(body, str) else str(body)

    @mcp.resource("trove://transcripts/{tid}.txt")
    def transcript_txt_alias_resource(tid: str) -> str:
        """Alias of ``trove://transcript/{tid}/text`` using the
        plural-collection / file-suffix URI shape that mirrors the
        public REST path (``/transcripts/<id>/export.txt``). Both URIs
        are kept so existing MCP clients keep working."""
        return transcript_text_resource(tid)

    @mcp.resource("trove://storage")
    def storage_resource() -> str:
        import json as _json
        return _json.dumps(_safe("storage_info", lambda: _client.storage_info()), indent=2)

    @mcp.resource("spool://clips")
    def clips_resource() -> str:
        """The render queue — every clip/render job and its status (spec §4)."""
        import json as _json
        return _json.dumps(_safe("list_clip_jobs", lambda: _client.list_clip_jobs()), indent=2)

    @mcp.resource("spool://clips/{job_id}")
    def clip_resource(job_id: str) -> str:
        """One clip/render job, including its ``result`` — candidates for a moments job,
        or the produced render's id + output path."""
        import json as _json
        return _json.dumps(_safe("get_clip_job", lambda: _client.get_clip_job(job_id)), indent=2)

    @mcp.resource("spool://recipes")
    def recipes_resource() -> str:
        """All saved recipes — the reusable pipelines that drive produce + watch automation."""
        import json as _json
        return _json.dumps(_safe("list_recipes", lambda: _client.list_recipes()), indent=2)

    @mcp.resource("spool://watches")
    def watches_resource() -> str:
        """All folder/channel/playlist watches + their reconcile state."""
        import json as _json
        return _json.dumps(_safe("list_watches", lambda: _client.list_watches()), indent=2)

    return mcp


# The transports FastMCP.run() actually accepts (Literal['stdio','sse','streamable-http']).
_VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _resolve_transport(env, settings_getter) -> str:
    """Which transport to boot with (the Settings "MCP transport" control, spec §5 P2).

    Order: an explicit ``SPOOL_MCP_TRANSPORT`` env var → the engine's persisted setting
    (read over HTTP, since the MCP server is just an API client — the golden rule) → ``stdio``.
    Anything invalid or a settings read that fails (engine not up yet at MCP boot) degrades to
    ``stdio`` — the desktop-client default — rather than crashing the server."""
    t = (env.get("SPOOL_MCP_TRANSPORT") or "").strip()
    if t in _VALID_TRANSPORTS:
        return t
    try:
        stored = str((settings_getter() or {}).get("mcp_transport", "stdio"))
    except (Exception, SystemExit):
        # TroveClient.request raises SystemExit (a BaseException) when the engine is
        # unreachable; the docstring's promise is "degrade to stdio, never crash".
        return "stdio"
    return stored if stored in _VALID_TRANSPORTS else "stdio"


def main() -> int:
    from config import DEFAULT_BASE_URL
    server = _build_server()
    base = os.environ.get("TROVE_URL", DEFAULT_BASE_URL)
    transport = _resolve_transport(os.environ, lambda: _client.get_settings())
    print(f"trove-mcp: Trove API → {base} (transport={transport})", file=sys.stderr)
    server.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
