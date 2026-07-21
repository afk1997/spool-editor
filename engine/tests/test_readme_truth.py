"""Product-truth checks for safety, security, and capacity claims in the README."""
from pathlib import Path


README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_names_the_exact_public_discovery_allowlist():
    text = README.read_text(encoding="utf-8")

    assert "every `/api/*` request" not in text
    assert (
        "public discovery is limited to `/api/v1/health`, `/api/v1/capabilities`, "
        "and `/api/v1/openapi.json`"
    ) in text
    assert "`/api/v1/doctor` requires the configured bearer token" in text


def test_readme_documents_the_canonical_download_capacity_response():
    text = README.read_text(encoding="utf-8")

    max_workers_row = next(
        line for line in text.splitlines() if line.startswith("| `TROVE_MAX_WORKERS`")
    )
    assert "HTTP 429" in max_workers_row
    assert "`queue_full`" in max_workers_row
    assert "`Retry-After: 1`" in max_workers_row
    assert "503" not in max_workers_row


def test_readme_names_every_outbound_boundary():
    text = " ".join(README.read_text(encoding="utf-8").split())

    assert "the only enabled outbound paths in Phase 0 are" in text
    assert "yt-dlp fetching user-requested media" in text
    assert "user-started model downloads from Hugging Face" in text
    assert (
        "Remote reasoning is unavailable and fails closed until a supported "
        "zero-tool transport exists."
    ) in text
    assert "Offline mode blocks all non-loopback egress" in text
    assert "transcription and rendering remain local" in text
    assert "optional Codex remote reasoning" not in text
    assert "the only outbound calls trove makes are" not in text


def test_readme_describes_phase_zero_mcp_as_inspection_only():
    text = " ".join(README.read_text(encoding="utf-8").split())

    assert (
        "use the CLI for manual operations. During Phase 0, MCP executes read-only "
        "inspection tools only"
    ) in text
    assert (
        "Mutation schemas remain advertised for contract compatibility, but every "
        "mutation returns `agent_mutation_disabled` before any `TroveClient` call."
    ) in text
    assert (
        "Manual mutations remain available through the authenticated UI, REST API, "
        "and CLI."
    ) in text
    assert (
        "read-only tools execute; every advertised mutation is centrally rejected"
    ) in text
    assert "drive Trove end-to-end" not in text
    assert "Tool surface mirrors the CLI 1:1" not in text
