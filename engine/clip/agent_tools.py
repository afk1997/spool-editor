"""The in-app Agent's tool catalog — the SAME /api/v1 surface the studio UI, the MCP server,
and the CLI drive (the README "golden rule"). Every tool here delegates to a :class:`TroveClient`.
During Phase 0 the catalog remains fully classified for contract coverage, but the agent prompt and
runtime expose only the explicit read-only allowlist; manual UI, REST, and CLI writes remain intact.

Each :class:`Tool` is name + one-line description + a ``run(client, args)`` that calls the matching
TroveClient method. The agent loop (``clip.agent.run_agent``) renders the catalog into its system
prompt and dispatches the model's chosen tool here. ``writes=True`` marks state-changing tools
(used for tracing / future confirmation gates). NO publish tool exists — clips land in review (the
honest Phase-4 gate), so the agent cannot publish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    desc: str
    params: dict[str, str] = field(default_factory=dict)   # name -> short type/desc, for the prompt
    run: Callable[[Any, dict], Any] = lambda c, a: None     # (TroveClient, args) -> JSON-able result
    writes: bool = False
    exports: bool = False                                   # renders a finished file (past the review gate)


def _i(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _f(v):
    return float(v)


def _status(a: dict) -> str:
    """Normalize a status filter: models often pass 'all'/'any'/'*' to mean 'no filter', but the API
    treats status as a literal match (so 'all' → zero results). Map those to '' (everything)."""
    s = str(a.get("status", "") or "").strip().lower()
    return "" if s in ("", "all", "any", "*", "everything", "none") else s


# The catalog. Tool names mirror the MCP tool surface so the two agent surfaces stay recognizable.
TOOLS: list[Tool] = [
    # ---- sources / downloads -------------------------------------------------
    Tool("list_jobs", "List source/download jobs (videos downloaded or downloading). The Library.",
         {"status": "optional: queued|running|done|error", "limit": "optional int"},
         lambda c, a: c.list_jobs(status=_status(a), limit=_i(a.get("limit"), 50))),
    Tool("get_job", "Get one source/download job by id.", {"job_id": "str"},
         lambda c, a: c.get_job(a["job_id"])),
    Tool("download_media", "Download a video from a URL (optionally auto-transcribe). Starts a job.",
         {"url": "str", "transcribe": "optional bool (default true)"},
         lambda c, a: c.submit_download(a["url"], auto_transcribe=bool(a.get("transcribe", True))), writes=True),
    Tool("pause_download", "Pause a running download.", {"job_id": "str"},
         lambda c, a: c.pause_job(a["job_id"]), writes=True),
    Tool("resume_download", "Resume a paused download.", {"job_id": "str"},
         lambda c, a: c.resume_job(a["job_id"]), writes=True),
    Tool("cancel_download", "Cancel a download job.", {"job_id": "str"},
         lambda c, a: c.cancel_job(a["job_id"]), writes=True),
    Tool("dismiss_download", "Drop a finished download job from the list.", {"job_id": "str"},
         lambda c, a: c.dismiss_job(a["job_id"]), writes=True),
    Tool("storage_info", "Disk usage + free space for the working set.", {},
         lambda c, a: c.storage_info()),

    # ---- transcripts ---------------------------------------------------------
    Tool("list_transcripts", "List transcripts (status: transcribing/done) for downloaded sources.",
         {"status": "optional", "limit": "optional int"},
         lambda c, a: c.list_transcripts(status=_status(a), limit=_i(a.get("limit"), 50))),
    Tool("get_transcript_status", "Transcription progress/status for a transcript id.", {"transcript_id": "str"},
         lambda c, a: c.get_transcript_status(a["transcript_id"])),
    Tool("transcribe", "Start (or re-run) transcription for a downloaded source job.", {"parent_job_id": "str"},
         lambda c, a: c.transcribe(a["parent_job_id"]), writes=True),
    Tool("cancel_transcribe", "Cancel a running transcription.", {"transcript_id": "str"},
         lambda c, a: c.cancel_transcribe(a["transcript_id"]), writes=True),
    Tool("search_transcripts", "Substring search across completed transcripts.",
         {"query": "str", "limit": "optional int"},
         lambda c, a: c.search_transcripts(a["query"], limit=_i(a.get("limit"), 25))),

    # ---- discovery / ranking -------------------------------------------------
    Tool("find_moments", "Find clip-worthy moments in a source's transcript (starts a job).",
         {"source_id": "str", "mode": "funny|insightful|hot-take|story|how-to|q&a", "count": "int"},
         lambda c, a: c.find_moments(a["source_id"], mode=a.get("mode", "funny"), count=_i(a.get("count"), 5)), writes=True),
    Tool("rank_candidates", "Glass-box re-rank a list of candidate moments by reweightable factors.",
         {"source_id": "str", "candidates": "list of {start,end,...}", "weights": "optional dict"},
         lambda c, a: c.rank_candidates(a["source_id"], a.get("candidates", []), weights=a.get("weights"))),

    # ---- single-clip ops -----------------------------------------------------
    Tool("cut_clip", "Cut a clip [start,end] (seconds) from a source (starts a job).",
         {"source_id": "str", "start": "float", "end": "float"},
         lambda c, a: c.cut_clip(a["source_id"], start=_f(a["start"]), end=_f(a["end"])), writes=True),
    Tool("reframe_clip", "Reframe a clip to an aspect (speaker-pan/split/center).",
         {"clip_id": "str", "aspect": "9:16|16:9|1:1|4:5", "mode": "pan|split|center"},
         lambda c, a: c.reframe_clip(a["clip_id"], aspect=a.get("aspect", "9:16"), mode=a.get("mode", "pan")), writes=True),
    Tool("caption_clip", "Burn captions on a clip with a preset.", {"clip_id": "str", "style": "opus|karaoke|minimal"},
         lambda c, a: c.caption_clip(a["clip_id"], style=a.get("style", "opus")), writes=True),
    Tool("render_clip", "EXPORT a clip for a platform preset (only on explicit render/export request).",
         {"clip_id": "str", "preset": "tiktok|reels|shorts|youtube|linkedin|x"},
         lambda c, a: c.render_clip(a["clip_id"], preset=a.get("preset", "tiktok")), writes=True, exports=True),
    Tool("make_clips", "PREFERRED for 'make/clip this' requests: cut + auto-reframe a [start,end] → "
         "the review queue (NO export — the honest gate; the user reviews before rendering).",
         {"source_id": "str", "start": "float", "end": "float", "aspect": "optional 9:16|16:9|1:1|4:5", "mode": "optional pan|split|center"},
         lambda c, a: c.render_pipeline(a["source_id"], start=_f(a["start"]), end=_f(a["end"]),
                                        aspect=a.get("aspect", "9:16"), mode=a.get("mode", "pan"),
                                        stop_after="reframe"), writes=True),
    Tool("render_pipeline", "EXPORT a finished clip: full cut→reframe→caption→export of a [start,end]. "
         "Only use when the user EXPLICITLY asks to render/export — otherwise prefer make_clips.",
         {"source_id": "str", "start": "float", "end": "float", "aspect": "optional", "mode": "optional", "style": "optional", "preset": "optional"},
         lambda c, a: c.render_pipeline(a["source_id"], start=_f(a["start"]), end=_f(a["end"]),
                                        aspect=a.get("aspect", "9:16"), mode=a.get("mode", "pan"),
                                        style=a.get("style", "opus"), preset=a.get("preset", "tiktok")), writes=True, exports=True),

    # ---- the render queue (clip jobs) ----------------------------------------
    Tool("list_clip_jobs", "List the render/clip-job QUEUE (produce/cut/reframe/caption/export/moments jobs) + status.",
         {"kind": "optional", "status": "optional", "limit": "optional int"},
         lambda c, a: c.list_clip_jobs(kind=a.get("kind", ""), status=_status(a), limit=_i(a.get("limit"), 50))),
    Tool("get_clip_job", "Get one clip/render job by id (progress, result).", {"job_id": "str"},
         lambda c, a: c.get_clip_job(a["job_id"])),
    Tool("cancel_clip_job", "Cancel a clip/render job.", {"job_id": "str"},
         lambda c, a: c.cancel_clip_job(a["job_id"]), writes=True),
    Tool("dismiss_clip_job", "Drop a finished clip/render job.", {"job_id": "str"},
         lambda c, a: c.dismiss_clip_job(a["job_id"]), writes=True),

    # ---- produce + automation (recipes / watches / brand kits) ---------------
    Tool("produce_clips", "Apply a recipe end-to-end (find→rank→top-N→render per moment) → review queue.",
         {"source_id": "str", "recipe_id": "optional saved recipe id", "recipe": "optional inline recipe dict"},
         lambda c, a: c.produce(a["source_id"], recipe_id=a.get("recipe_id") or None, **(a.get("recipe") or {})), writes=True),
    Tool("list_recipes", "List saved recipes (reusable produce pipelines).", {}, lambda c, a: c.list_recipes()),
    Tool("get_recipe", "Get one recipe by id.", {"recipe_id": "str"}, lambda c, a: c.get_recipe(a["recipe_id"])),
    Tool("create_recipe", "Create a saved recipe (name, content_mode, count, aspect, reframe_mode, caption_preset, platform, fast, weights, brand_kit_id).",
         {"recipe": "dict"}, lambda c, a: c.create_recipe(a.get("recipe") or a), writes=True),
    Tool("update_recipe", "Patch a saved recipe.", {"recipe_id": "str", "changes": "dict"},
         lambda c, a: c.update_recipe(a["recipe_id"], a.get("changes") or {}), writes=True),
    Tool("delete_recipe", "Delete a saved recipe.", {"recipe_id": "str"},
         lambda c, a: c.delete_recipe(a["recipe_id"]), writes=True),
    Tool("list_watches", "List folder/channel/playlist watches (auto-produce automations) + their state.", {},
         lambda c, a: c.list_watches()),
    Tool("get_watch", "Get one watch by id.", {"watch_id": "str"}, lambda c, a: c.get_watch(a["watch_id"])),
    Tool("create_watch", "Create a watch (name, kind folder|channel|playlist, target path/URL, recipe_id, enabled).",
         {"watch": "dict"}, lambda c, a: c.create_watch(a.get("watch") or a), writes=True),
    Tool("update_watch", "Patch a watch.", {"watch_id": "str", "changes": "dict"},
         lambda c, a: c.update_watch(a["watch_id"], a.get("changes") or {}), writes=True),
    Tool("delete_watch", "Delete a watch.", {"watch_id": "str"}, lambda c, a: c.delete_watch(a["watch_id"]), writes=True),
    Tool("scan_watch", "Reconcile a watch now: ingest new videos → produce per its recipe.", {"watch_id": "str"},
         lambda c, a: c.scan_watch(a["watch_id"]), writes=True),
    Tool("list_brand_kits", "List brand kits (reusable look: caption preset/overrides/watermark/lower-third).", {},
         lambda c, a: c.list_brand_kits()),
    Tool("create_brand_kit", "Create a brand kit (name, palette, caption_preset, caption_overrides, watermark, lower_third, fonts).",
         {"kit": "dict"}, lambda c, a: c.create_brand_kit(a.get("kit") or a), writes=True),
    Tool("update_brand_kit", "Patch a brand kit.", {"kit_id": "str", "changes": "dict"},
         lambda c, a: c.update_brand_kit(a["kit_id"], a.get("changes") or {}), writes=True),
    Tool("delete_brand_kit", "Delete a brand kit.", {"kit_id": "str"},
         lambda c, a: c.delete_brand_kit(a["kit_id"]), writes=True),

    # ---- models + capabilities ----------------------------------------------
    Tool("list_models", "List transcription (whisper) models + which is active/installed.", {},
         lambda c, a: c.list_models()),
    Tool("install_model", "Download/install a transcription model by name.", {"name": "str"},
         lambda c, a: c.install_model(a["name"]), writes=True),
    Tool("set_active_model", "Set the active transcription model.", {"name": "str"},
         lambda c, a: c.set_active_model(a["name"]), writes=True),
    Tool("remove_model", "Remove an installed transcription model.", {"name": "str"},
         lambda c, a: c.remove_model(a["name"]), writes=True),
    Tool("capabilities", "Server feature/limit/scope registry (formats, presets, modes, flags).", {},
         lambda c, a: c.capabilities()),

    # ---- settings / transcript-edit / timeline signals -----------------------
    Tool("get_settings", "Read writable engine config (render defaults, concurrency, transport).", {},
         lambda c, a: c.get_settings()),
    Tool("update_settings", "Patch engine config (the changed keys only).", {"changes": "dict"},
         lambda c, a: c.update_settings(a.get("changes") or a), writes=True),
    Tool("edit_word", "Fix ONE transcript word — op=set_text|delete|insert_after|merge_next (text for "
         "set_text/insert_after). Use to correct a misheard word before captioning.",
         {"transcript_id": "str", "word_index": "int", "op": "str", "text": "optional str"},
         lambda c, a: c.edit_word(a["transcript_id"], _i(a.get("word_index"), 0), a["op"], text=a.get("text")), writes=True),
    Tool("dismiss_transcribe", "Drop a finished transcribe job.", {"transcript_id": "str"},
         lambda c, a: c.dismiss_transcribe(a["transcript_id"]), writes=True),
    Tool("source_energy", "Audio-energy loudness envelope (0..1 bars) for a source; optional start/end window.",
         {"source_id": "str", "start": "optional float", "end": "optional float"},
         lambda c, a: c.source_energy(a["source_id"], start=(_f(a["start"]) if a.get("start") is not None else None),
                                      end=(_f(a["end"]) if a.get("end") is not None else None),
                                      use_cache=False)),
    Tool("source_scenes", "Scene-cut timestamps within [start,end] for a source.",
         {"source_id": "str", "start": "float", "end": "float"},
         lambda c, a: c.source_scenes(a["source_id"], start=_f(a["start"]), end=_f(a["end"]))),
]

CATALOG: dict[str, Tool] = {t.name: t for t in TOOLS}

# Phase 0's frozen read-only agent surface. Every catalog tool outside this set must be classified
# ``writes=True`` and is rejected before its implementation or TroveClient method is evaluated.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "capabilities",
    "get_clip_job",
    "get_job",
    "get_recipe",
    "get_settings",
    "get_transcript_status",
    "get_watch",
    "list_brand_kits",
    "list_clip_jobs",
    "list_jobs",
    "list_models",
    "list_recipes",
    "list_transcripts",
    "list_watches",
    "rank_candidates",
    "search_transcripts",
    "source_energy",
    "source_scenes",
    "storage_info",
})

MUTATION_DISABLED_ERROR = {
    "error": "agent_mutation_disabled",
    "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
}

# Tools that must not run without an explicit human go-ahead: gates the exports-flagged
# tools (render_clip / render_pipeline) plus the delete/settings/model-removal family --
# retained as compatibility metadata for later approval work. Phase 0 rejects every write before
# this historical confirmation classification is consulted.
CONFIRM_REQUIRED: frozenset = frozenset(
    {t.name for t in TOOLS if t.exports}
    | {"delete_recipe", "delete_watch", "delete_brand_kit", "remove_model", "update_settings"}
)

# Tools whose result is a freshly-started job (id + status) — surfaced to the studio as job chips +
# a "started N jobs" toast, and tracked in the render queue.
JOB_STARTING = {
    "download_media", "transcribe", "find_moments", "cut_clip", "reframe_clip", "caption_clip",
    "render_clip", "render_pipeline", "make_clips", "produce_clips", "scan_watch",
}


def catalog_prompt() -> str:
    """Render only the Phase 0 read-only inspection tools into the agent system prompt."""
    lines = []
    for t in TOOLS:
        if t.name not in READ_ONLY_TOOLS:
            continue
        ps = ", ".join(f"{k}" for k in t.params) if t.params else ""
        lines.append(f"- {t.name}({ps}) — {t.desc}")
    return "\n".join(lines)
