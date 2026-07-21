"""Product-truth checks for security and capacity claims in the engine README."""
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
