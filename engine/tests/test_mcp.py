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

import pytest

mcp_sdk = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    expected_tools = {
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
        # clip surface
        "find_moments", "rank_candidates", "cut_clip", "reframe_clip", "caption_clip",
        "render_clip", "render_pipeline",
        "list_clip_jobs", "get_clip_job", "cancel_clip_job", "dismiss_clip_job",
        # automation surface (Phase 3 — parity with the studio)
        "produce_clips",
        "list_recipes", "get_recipe", "create_recipe", "update_recipe", "delete_recipe",
        "list_watches", "get_watch", "create_watch", "update_watch", "delete_watch", "scan_watch",
        "list_brand_kits", "create_brand_kit", "update_brand_kit", "delete_brand_kit",
        # settings / transcript-edit / timeline signals (UI<->MCP<->CLI parity)
        "get_settings", "update_settings", "edit_word", "dismiss_transcribe",
        "source_energy", "source_scenes", "source_filmstrip", "download_render",
    }
    assert set(result["tool_names"]) == expected_tools, result["tool_names"]
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
    for key in ("get_job_bad", "transcribe_bad", "export_bad", "install_bad"):
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

    # Clip surface: read path returns the render queue, validation paths return
    # structured errors (never a stack trace), mirroring the trove tools.
    assert "clip_jobs" in result["list_clip_jobs"]
    for key in ("get_clip_job_bad", "moments_bad", "cut_bad", "reframe_bad"):
        v = result[key]
        assert isinstance(v, dict) and "error" in v, (key, v)
        assert "Traceback" not in str(v), (key, v)
    assert result["moments_bad"]["error"] == "source_not_ready"
    assert result["reframe_bad"]["error"] == "clip_not_found"
    assert "clip_jobs" in json.loads(result["clips_resource"])

    # Automation surface: list reads return the stores; produce on a bad source errs cleanly;
    # the recipes/watches resources surface live state — full parity with the studio.
    assert "recipes" in result["list_recipes"]
    assert "watches" in result["list_watches"]
    assert "brand_kits" in result["list_brand_kits"]
    assert isinstance(result["produce_bad"], dict) and "error" in result["produce_bad"]
    assert "Traceback" not in str(result["produce_bad"])
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
def test_mcp_reframe_elicits_missing_aspect(tmp_path):
    """When reframe_clip is called without an aspect, the server elicits the choice
    (spec §4) and feeds it through — proven by the callback firing and the elicited
    clip flowing to the (bogus here) backend call."""
    with _trove_server(tmp_path) as port:
        fired, data = asyncio.run(_drive_elicit(port))
    assert fired == 1, "server should have elicited the aspect/mode"
    assert data.get("error") == "clip_not_found"  # elicited params reached the backend


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


# ---- produce_clips kwargs-collision guard ----

def test_produce_clips_strips_reserved_keys(monkeypatch):
    """An inline ``recipe`` that happens to carry ``source_id`` / ``recipe_id`` must not
    collide with the positional/keyword args of ``_client.produce`` (TypeError: multiple
    values). The tool strips those reserved keys before the splat, so the call stays clean
    (mirrors the ``produce_bad`` no-Traceback assertion)."""
    import mcp_server

    seen: dict = {}

    def fake_produce(source_id, *, recipe_id=None, **recipe):
        seen["source_id"] = source_id
        seen["recipe_id"] = recipe_id
        seen["recipe"] = recipe
        return {"id": "p1", "kind": "produce", "status": "queued"}

    monkeypatch.setattr(mcp_server._client, "produce", fake_produce)
    server = mcp_server._build_server()

    async def _call(recipe):
        res = await server.call_tool(
            "produce_clips", {"source_id": "src1", "recipe": recipe})
        # call_tool returns a list of content parts; parse the text payload.
        parts = res[0] if isinstance(res, tuple) else res
        return json.loads(parts[0].text)

    # A recipe colliding on the positional source_id — would raise TypeError pre-fix.
    out = asyncio.run(_call({"source_id": "x", "count": 3}))
    assert out == {"id": "p1", "kind": "produce", "status": "queued"}
    assert "Traceback" not in str(out)
    assert seen["source_id"] == "src1"  # the real positional, not the recipe's "x"
    assert seen["recipe"] == {"count": 3}  # reserved key dropped

    # A recipe colliding on the keyword recipe_id — same clean outcome.
    out = asyncio.run(_call({"recipe_id": "r", "aspect": "1:1"}))
    assert out == {"id": "p1", "kind": "produce", "status": "queued"}
    assert "Traceback" not in str(out)
    assert seen["recipe_id"] is None  # the explicit (empty) recipe_id, not the recipe's "r"
    assert seen["recipe"] == {"aspect": "1:1"}
