"""End-to-end tests for the trove-mcp MCP server.

These spawn the real ``mcp_server.py`` as a subprocess over the
official MCP stdio protocol, so they exercise both the FastMCP
wiring and the underlying HTTP plumbing through cli.py. Skipped
when the optional `mcp` SDK isn't installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

mcp_sdk = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_TOOLS = {
    "list_jobs", "get_job", "download_media", "bulk_download",
    "pause_download", "resume_download", "cancel_download",
    "dismiss_download",
    "list_transcripts", "search_transcripts",
    "get_transcript_status", "transcribe",
    "cancel_transcribe", "get_transcript", "get_transcript_chunk",
    "list_models", "install_model", "model_install_progress",
    "set_active_model", "remove_model",
    "storage_info",
    "server_capabilities",
    "find_moments", "rank_candidates", "cut_clip", "reframe_clip", "caption_clip",
    "render_clip", "render_pipeline",
    "list_clip_jobs", "get_clip_job", "cancel_clip_job", "dismiss_clip_job",
    "produce_clips",
    "list_recipes", "get_recipe", "create_recipe", "update_recipe", "delete_recipe",
    "list_watches", "get_watch", "create_watch", "update_watch", "delete_watch", "scan_watch",
    "list_brand_kits", "create_brand_kit", "update_brand_kit", "delete_brand_kit",
    "get_settings", "update_settings", "edit_word", "dismiss_transcribe",
    "source_energy", "source_scenes", "source_filmstrip", "download_render",
}

READ_ONLY_TOOLS = {
    "get_clip_job", "get_job", "get_recipe", "get_settings", "get_transcript",
    "get_transcript_chunk", "get_transcript_status", "get_watch", "list_brand_kits",
    "list_clip_jobs", "list_jobs", "list_models", "list_recipes", "list_transcripts",
    "list_watches", "model_install_progress", "rank_candidates", "search_transcripts",
    "server_capabilities", "source_energy", "source_filmstrip", "source_scenes",
    "storage_info",
}

MUTATING_TOOL_ARGS = {
    "bulk_download": {"urls": ["https://example.com/video"]},
    "cancel_clip_job": {"job_id": "job-1"},
    "cancel_download": {"job_id": "job-1"},
    "cancel_transcribe": {"transcript_id": "transcript-1"},
    "caption_clip": {"clip_id": "clip-1"},
    "create_brand_kit": {"kit": {}},
    "create_recipe": {"recipe": {}},
    "create_watch": {"watch": {}},
    "cut_clip": {"source_id": "source-1", "start": 0.0, "end": 1.0},
    "delete_brand_kit": {"kit_id": "kit-1"},
    "delete_recipe": {"recipe_id": "recipe-1"},
    "delete_watch": {"watch_id": "watch-1"},
    "dismiss_clip_job": {"job_id": "job-1"},
    "dismiss_download": {"job_id": "job-1"},
    "dismiss_transcribe": {"transcript_id": "transcript-1"},
    "download_media": {"url": "https://example.com/video"},
    "download_render": {"clip_id": "clip-1", "render_id": "render-1", "save_to": "/tmp/render.mp4"},
    "edit_word": {"transcript_id": "transcript-1", "word_index": 0, "op": "delete"},
    "find_moments": {"source_id": "source-1"},
    "install_model": {"name": "ggml-tiny.bin"},
    "pause_download": {"job_id": "job-1"},
    "produce_clips": {"source_id": "source-1"},
    "reframe_clip": {"clip_id": "clip-1"},
    "remove_model": {"name": "ggml-tiny.bin"},
    "render_clip": {"clip_id": "clip-1"},
    "render_pipeline": {"source_id": "source-1", "start": 0.0, "end": 1.0},
    "resume_download": {"job_id": "job-1"},
    "scan_watch": {"watch_id": "watch-1"},
    "set_active_model": {"name": "ggml-tiny.bin"},
    "transcribe": {"parent_job_id": "source-1"},
    "update_brand_kit": {"kit_id": "kit-1", "changes": {}},
    "update_recipe": {"recipe_id": "recipe-1", "changes": {}},
    "update_settings": {"changes": {}},
    "update_watch": {"watch_id": "watch-1", "changes": {}},
}

PHASE0_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/v1/phase0-contract.json")
    .read_text(encoding="utf-8")
)
MUTATION_DISABLED = PHASE0_CONTRACT["agent_mutation_disabled"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _trove_server(tmp_path):
    """Spin up `python app.py` on a free port in an isolated cwd so
    the MCP server has a real Trove HTTP backend to talk to."""
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "TROVE_RATE_LIMIT": "0"}
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "app.py")],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        import urllib.request
        url = f"http://127.0.0.1:{port}/api/v1/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("trove server did not come up")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _drive_mcp(port: int) -> dict:
    """Open one MCP session and exercise the contract surface."""
    env = {**os.environ, "TROVE_URL": f"http://127.0.0.1:{port}"}
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(REPO_ROOT, "mcp_server.py")],
        env=env,
    )
    out: dict = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            out["tool_names"] = sorted(t.name for t in tools.tools)

            templates = await session.list_resource_templates()
            out["templates"] = sorted(
                t.uriTemplate for t in templates.resourceTemplates
            )

            async def call(name, args=None):
                r = await session.call_tool(name, args or {})
                return json.loads(r.content[0].text)

            out["list_jobs"] = await call("list_jobs")
            out["list_models"] = await call("list_models")
            out["list_transcripts"] = await call("list_transcripts")
            out["get_job_bad"] = await call("get_job", {"job_id": "nope"})
            out["transcribe_bad"] = await call(
                "transcribe", {"parent_job_id": "nope"})
            out["export_bad"] = await call(
                "get_transcript",
                {"transcript_id": "nope", "format": "txt"})
            out["bad_format"] = await call(
                "get_transcript", {"transcript_id": "x", "format": "docx"})
            out["install_bad"] = await call(
                "install_model", {"name": "not-real.bin"})

            # ---- clip surface (read + validation paths only; no real ffmpeg) ----
            out["list_clip_jobs"] = await call("list_clip_jobs")
            out["get_clip_job_bad"] = await call("get_clip_job", {"job_id": "nope"})
            out["moments_bad"] = await call("find_moments", {"source_id": "nope"})
            out["cut_bad"] = await call("cut_clip", {"source_id": "nope", "start": 1.0, "end": 5.0})
            # aspect+mode provided → no elicitation; bogus clip → clip_not_found
            out["reframe_bad"] = await call(
                "reframe_clip", {"clip_id": "nope", "aspect": "9:16", "mode": "pan"})

            # ---- automation surface (produce / recipes / watches / brand kits) ----
            out["list_recipes"] = await call("list_recipes")
            out["list_watches"] = await call("list_watches")
            out["list_brand_kits"] = await call("list_brand_kits")
            out["produce_bad"] = await call("produce_clips", {"source_id": "nope"})

            jobs_res = await session.read_resource("trove://jobs")
            out["jobs_resource"] = jobs_res.contents[0].text
            clips_res = await session.read_resource("spool://clips")
            out["clips_resource"] = clips_res.contents[0].text
            recipes_res = await session.read_resource("spool://recipes")
            out["recipes_resource"] = recipes_res.contents[0].text
            watches_res = await session.read_resource("spool://watches")
            out["watches_resource"] = watches_res.contents[0].text

            # Read the alias and the legacy text resource for the same
            # bogus tid; both should produce identical (error) payloads
            # so MCP clients can choose either URI shape interchangeably.
            legacy = await session.read_resource("trove://transcript/nope/text")
            alias  = await session.read_resource("trove://transcripts/nope.txt")
            out["txt_legacy"] = legacy.contents[0].text
            out["txt_alias"]  = alias.contents[0].text
    return out


@pytest.mark.timeout(60)
def test_mcp_end_to_end(tmp_path):
    """One real client session against a real trove server.

    Locks: tool surface (count + names), resource templates, success
    paths (list_*), and structured-error paths (no stack traces leak).
    """
    with _trove_server(tmp_path) as port:
        result = asyncio.run(_drive_mcp(port))

    assert set(result["tool_names"]) == EXPECTED_TOOLS, result["tool_names"]
    assert "trove://transcript/{tid}" in result["templates"]
    assert "spool://clips/{job_id}" in result["templates"]
    # New plural/.txt alias must be advertised alongside the legacy URI
    # so MCP clients can address per-transcript text via the REST-shaped
    # path ``trove://transcripts/<id>.txt``.
    assert "trove://transcripts/{tid}.txt" in result["templates"]
    # Alias must produce byte-identical output to the legacy URI so
    # downgrading clients keeps working.
    assert result["txt_alias"] == result["txt_legacy"]

    # Success surface
    assert "jobs" in result["list_jobs"]
    assert "models" in result["list_models"]
    assert result["list_models"]["models"], "models list shouldn't be empty"
    assert "transcripts" in result["list_transcripts"]

    # Error surface — every error must be a {error, status?} dict, never
    # a stack trace or HTML body.
    for key in ("get_job_bad", "export_bad"):
        v = result[key]
        assert isinstance(v, dict) and "error" in v, (key, v)
        assert "status" in v, (key, v)
        assert "Traceback" not in str(v), (key, v)
        assert "<html" not in str(v).lower(), (key, v)

    # The export 404 specifically used to leak HTML — pin the JSON body.
    assert result["export_bad"]["status"] == 404
    assert result["export_bad"]["error"] == "transcript_not_found_or_not_done"

    # Pre-tool-side-validation (bad format) doesn't reach HTTP, no status.
    assert result["bad_format"] == {"error": "format must be txt|srt|vtt|json"}

    # Resources return JSON text, not Python repr.
    assert json.loads(result["jobs_resource"])["jobs"] is not None or \
           "jobs" in json.loads(result["jobs_resource"])

    # Every mutating surface returns the exact Phase 0 envelope, never a backend result.
    for key in (
        "transcribe_bad", "install_bad", "moments_bad", "cut_bad",
        "reframe_bad", "produce_bad",
    ):
        assert result[key] == MUTATION_DISABLED

    # Clip read surfaces remain available.
    assert "clip_jobs" in result["list_clip_jobs"]
    assert result["get_clip_job_bad"]["error"] == "not_found"
    assert "clip_jobs" in json.loads(result["clips_resource"])

    # Automation read surfaces and resources remain available.
    assert "recipes" in result["list_recipes"]
    assert "watches" in result["list_watches"]
    assert "brand_kits" in result["list_brand_kits"]
    assert "recipes" in json.loads(result["recipes_resource"])
    assert "watches" in json.loads(result["watches_resource"])


async def _drive_elicit(port: int) -> tuple[int, dict]:
    """Open a session WITH an elicitation handler; call reframe_clip without an
    aspect so the server must elicit the choice."""
    import mcp.types as mt

    fired = {"n": 0}

    async def on_elicit(ctx, params):
        fired["n"] += 1
        return mt.ElicitResult(action="accept", content={"aspect": "1:1", "mode": "split"})

    env = {**os.environ, "TROVE_URL": f"http://127.0.0.1:{port}"}
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(REPO_ROOT, "mcp_server.py")],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, elicitation_callback=on_elicit) as session:
            await session.initialize()
            r = await session.call_tool("reframe_clip", {"clip_id": "nope"})
            data = json.loads(r.content[0].text)
    return fired["n"], data


@pytest.mark.timeout(60)
def test_mcp_reframe_is_disabled_before_elicitation(tmp_path):
    """A disabled mutator must not prompt the user before the central guard rejects it."""
    with _trove_server(tmp_path) as port:
        fired, data = asyncio.run(_drive_elicit(port))
    assert fired == 0
    assert data == MUTATION_DISABLED


# ---- MCP transport resolution (S14: the Settings "MCP transport" control) ----

def test_resolve_transport_prefers_env_then_settings_then_stdio():
    """The MCP server boots with the transport the user set in Settings (persisted in the
    engine's settings store, read here over HTTP) — env override wins, and anything invalid
    or unreachable degrades to stdio (the desktop-client default), never a crash."""
    from mcp_server import _resolve_transport

    # explicit, valid env var wins over the stored value
    assert _resolve_transport({"SPOOL_MCP_TRANSPORT": "sse"},
                              lambda: {"mcp_transport": "stdio"}) == "sse"
    # invalid env value → fall through to the settings store
    assert _resolve_transport({"SPOOL_MCP_TRANSPORT": "telepathy"},
                              lambda: {"mcp_transport": "streamable-http"}) == "streamable-http"
    # no env → the stored value
    assert _resolve_transport({}, lambda: {"mcp_transport": "sse"}) == "sse"
    # store unreachable (engine down at MCP boot) → stdio. TroveClient.request raises
    # SystemExit (not Exception) on connection-refused — the common Claude Desktop boot
    # order (MCP server starts before the engine).
    def _engine_down():
        raise SystemExit("trove: cannot reach http://127.0.0.1:5000 (connection refused)")
    assert _resolve_transport({}, _engine_down) == "stdio"

    def _boom():
        raise RuntimeError("engine down")
    assert _resolve_transport({}, _boom) == "stdio"
    # stored value isn't a real FastMCP transport → stdio
    assert _resolve_transport({}, lambda: {"mcp_transport": "carrier-pigeon"}) == "stdio"
    # nothing set anywhere → stdio
    assert _resolve_transport({}, lambda: {}) == "stdio"


# ---- Phase 0 central read-only classification + zero-call mutation guard ----

def test_every_mutating_mcp_tool_is_disabled_before_client_access(monkeypatch):
    import mcp_server

    assert set(mcp_server.READ_ONLY_TOOLS) == READ_ONLY_TOOLS
    assert set(MUTATING_TOOL_ARGS) == EXPECTED_TOOLS - READ_ONLY_TOOLS

    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"MCP mutator reached TroveClient.{name}")

    monkeypatch.setattr(mcp_server, "_client", ExplodingClient())
    server = mcp_server._build_server()

    async def _call_all():
        for tool_name, args in MUTATING_TOOL_ARGS.items():
            res = await server.call_tool(tool_name, args)
            parts = res[0] if isinstance(res, tuple) else res
            assert json.loads(parts[0].text) == MUTATION_DISABLED, tool_name

    asyncio.run(_call_all())


def test_phase_zero_signal_reads_disable_durable_cache(monkeypatch):
    import mcp_server

    calls = []

    class CountingClient:
        def source_energy(self, source_id, **kwargs):
            calls.append(("source_energy", source_id, kwargs))
            return {"bars": [0.5], "buckets": kwargs["buckets"]}

        def source_filmstrip(self, source_id, **kwargs):
            calls.append(("source_filmstrip", source_id, kwargs))
            return {"strip": "data:image/jpeg;base64,AAAA", "frames": kwargs["frames"]}

    monkeypatch.setattr(mcp_server, "_client", CountingClient())
    server = mcp_server._build_server()

    async def _call_signals():
        await server.call_tool("source_energy", {"source_id": "src1"})
        await server.call_tool(
            "source_filmstrip",
            {"source_id": "src1", "start": 1.0, "end": 5.0},
        )

    asyncio.run(_call_signals())
    assert calls == [
        (
            "source_energy",
            "src1",
            {"buckets": 96, "start": None, "end": None, "use_cache": False},
        ),
        (
            "source_filmstrip",
            "src1",
            {"start": 1.0, "end": 5.0, "frames": 12, "use_cache": False},
        ),
    ]


def test_phase_zero_transcript_search_disables_index_backfill(monkeypatch):
    import mcp_server

    calls = []

    class CountingClient:
        def search_transcripts(self, query, **kwargs):
            calls.append((query, kwargs))
            return {"query": query, "matches": [], "returned": 0}

    monkeypatch.setattr(mcp_server, "_client", CountingClient())
    server = mcp_server._build_server()

    asyncio.run(server.call_tool("search_transcripts", {"query": "hello"}))

    assert calls == [
        ("hello", {"limit": 50, "context": 60, "backfill_index": False})
    ]


def test_phase_zero_list_models_mcp_read_has_zero_filesystem_delta(
    tmp_path, monkeypatch
):
    import mcp_server
    import models_store

    models_dir = tmp_path / "mcp-models" / "models"
    monkeypatch.setattr(models_store, "MODELS_DIR", models_dir)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    class LocalClient:
        def list_models(self):
            return {
                "active": models_store.get_active(),
                "installed": models_store.list_installed(),
            }

    monkeypatch.setattr(mcp_server, "_client", LocalClient())
    server = mcp_server._build_server()

    asyncio.run(server.call_tool("list_models", {}))

    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert not models_dir.exists()


def test_contract_fixture_mcp_word_edit_and_bulk_schemas_make_zero_client_calls(
    monkeypatch,
):
    import mcp_server

    calls = []

    class CountingClient:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"unexpected": True}
            return call

    monkeypatch.setattr(mcp_server, "_client", CountingClient())
    server = mcp_server._build_server()

    async def _inspect_and_call():
        tools = {tool.name: tool for tool in await server.list_tools()}
        edit_schema = tools["edit_word"].inputSchema
        bulk_schema = tools["bulk_download"].inputSchema
        assert set(PHASE0_CONTRACT["word_edit"]["request"]) <= set(
            edit_schema["properties"]
        )
        assert "text" not in edit_schema["properties"]
        assert set(PHASE0_CONTRACT["bulk_submit"]["request"]) <= set(
            bulk_schema["properties"]
        )

        edit_args = {
            "transcript_id": PHASE0_CONTRACT["word_edit"]["response_subset"]["tid"],
            "word_index": PHASE0_CONTRACT["word_edit"]["response_subset"]["word"]["idx"],
            **PHASE0_CONTRACT["word_edit"]["request"],
        }
        results = []
        for tool_name, args in (
            ("edit_word", edit_args),
            ("bulk_download", PHASE0_CONTRACT["bulk_submit"]["request"]),
        ):
            result = await server.call_tool(tool_name, args)
            parts = result[0] if isinstance(result, tuple) else result
            results.append(json.loads(parts[0].text))
        return results

    assert asyncio.run(_inspect_and_call()) == [MUTATION_DISABLED, MUTATION_DISABLED]
    assert calls == []


def test_dismiss_tool_descriptions_mark_history_hidden_and_preserve_managed_files():
    import mcp_server

    server = mcp_server._build_server()

    async def _descriptions():
        return {
            tool.name: tool.description
            for tool in await server.list_tools()
            if tool.name in {
                "dismiss_download",
                "dismiss_clip_job",
                "dismiss_transcribe",
            }
        }

    descriptions = asyncio.run(_descriptions())
    assert descriptions == {
        "dismiss_download": (
            "Mark a terminal download hidden in history. Managed media remains on disk."
        ),
        "dismiss_clip_job": (
            "Mark a finished clip/render job hidden in history. "
            "Managed media remains on disk."
        ),
        "dismiss_transcribe": (
            "Mark a finished transcribe job hidden in history. "
            "Managed files remain on disk."
        ),
    }


def test_phase_zero_tool_descriptions_do_not_advertise_reasoning_or_automation():
    import mcp_server

    module_truth = " ".join((mcp_server.__doc__ or "").split())
    assert (
        "Remote reasoning, automated discovery, and watch reconciliation are "
        "unavailable in Phase 0."
    ) in module_truth
    assert (
        "The supported manual path is import, transcribe, transcript-range selection, "
        "cut, edit/reframe/caption, and render/export."
    ) in module_truth

    server = mcp_server._build_server()

    async def _descriptions():
        return {
            tool.name: " ".join((tool.description or "").split())
            for tool in await server.list_tools()
            if tool.name in {
                "find_moments",
                "rank_candidates",
                "produce_clips",
                "list_watches",
                "scan_watch",
            }
        }

    descriptions = asyncio.run(_descriptions())
    assert (
        "Unavailable in Phase 0: remote reasoning and automated discovery fail closed."
        in descriptions["find_moments"]
    )
    assert "caller-supplied candidates" in descriptions["rank_candidates"]
    assert "prior ``find_moments``" not in descriptions["rank_candidates"]
    assert (
        "Unavailable in Phase 0: automated discovery and recipe production fail closed."
        in descriptions["produce_clips"]
    )
    assert (
        "Phase 0 does not run watch reconciliation or automatic production."
        in descriptions["list_watches"]
    )
    assert (
        "Unavailable in Phase 0: watch reconciliation is disabled."
        in descriptions["scan_watch"]
    )
    assert "default: the codex bridge" not in descriptions["find_moments"].lower()
