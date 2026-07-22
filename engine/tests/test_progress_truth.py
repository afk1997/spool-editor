"""Product-truth checks for the current reasoning boundary in PROGRESS.md."""
from pathlib import Path


PROGRESS = Path(__file__).resolve().parents[2] / "docs" / "PROGRESS.md"


def test_progress_records_the_current_phase_zero_reasoning_boundary():
    lines = PROGRESS.read_text(encoding="utf-8").splitlines()
    text = " ".join(line.removeprefix("> ").strip() for line in lines)

    assert "Current runtime/provider default is **none**." in text
    assert "`SPOOL_LLM_PROVIDER` (default `none`)" in text
    assert (
        "Remote Codex reasoning is unavailable in Phase 0 and fails closed until "
        "a supported zero-tool transport exists."
    ) in text
    assert (
        "Codex and live-agent references below are pre-fuse historical evidence only."
    ) in text
    assert (
        "Remote reasoning, automated discovery, and watch reconciliation are unavailable "
        "in Phase 0."
    ) in text
    assert (
        "The supported local workflow is import media → transcribe → select a transcript "
        "range manually → cut → edit/reframe/caption → render/export."
    ) in text
    assert "DEFAULT = **codex bridge**" not in text
    assert 'default "codex bridge"' not in text
    assert "`SPOOL_LLM_PROVIDER` (default `codex`)" not in text
    assert "The provider scaffolding remains" not in text
    assert "remote_agent_tools_disabled" not in text
