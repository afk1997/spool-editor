"""Phase 0 regression matrix: cleanup changes history visibility, never artifact bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import clip_jobs
import jobs
import transcribe_jobs
from clip_jobs import ClipJob, ClipJobManager, ClipStatus
from jobs import Job, JobManager, JobStatus
from transcribe_jobs import TranscribeJob, TranscribeJobManager, TranscribeStatus


DISMISSED_AT = "2026-07-13T12:34:56.789Z"


def artifact_hashes(root: Path, paths: list[Path]) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _seed_managers(tmp_path: Path):
    source = tmp_path / "media" / "source.mp4"
    words = tmp_path / "media" / "source.words.json"
    srt = tmp_path / "media" / "source.srt"
    vtt = tmp_path / "media" / "source.vtt"
    txt = tmp_path / "media" / "source.txt"
    intermediate = tmp_path / "clips" / "clip-1" / "reframed.mp4"
    sidecar = tmp_path / "clips" / "clip-1" / "captions.ass"
    rendered = tmp_path / "clips" / "clip-1" / "captioned.mp4"
    export = tmp_path / "clips" / "clip-1" / "renders" / "export.mp4"
    artifacts = [source, words, srt, vtt, txt, intermediate, sidecar, rendered, export]
    for i, path in enumerate(artifacts):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"published-artifact-{i}".encode())

    stores = {
        "download": tmp_path / "stores" / "jobs.json",
        "transcribe": tmp_path / "stores" / "transcribe_jobs.json",
        "clip": tmp_path / "stores" / "clip_jobs.json",
    }
    managers = {
        "download": JobManager(max_workers=1, ttl_seconds=0, store_path=stores["download"]),
        "transcribe": TranscribeJobManager(max_workers=1, ttl_seconds=0, store_path=stores["transcribe"]),
        "clip": ClipJobManager(max_workers=1, ttl_seconds=0, store_path=stores["clip"]),
    }
    records = {
        "download": Job(
            id="download-done", url="https://example.test/video", title="Source",
            status=JobStatus.DONE, file_path=str(source), filename=source.name,
        ),
        "transcribe": TranscribeJob(
            id="transcribe-done", parent_job_id="download-done", model_used="local.bin",
            status=TranscribeStatus.DONE, progress_pct=100,
        ),
        "clip": ClipJob(
            id="clip-done", kind="export", source_id="download-done", clip_id="clip-1",
            status=ClipStatus.DONE, progress_pct=100,
            result={
                "reframed_path": str(intermediate), "ass_path": str(sidecar),
                "captioned_path": str(rendered), "output_path": str(export),
                "render_id": "export",
            },
        ),
    }
    for name, manager in managers.items():
        with manager._lock:
            manager._jobs[records[name].id] = records[name]
        manager._persist()
    return artifacts, stores, managers, records


def _restart(stores):
    return {
        "download": JobManager(max_workers=1, ttl_seconds=0, store_path=stores["download"]),
        "transcribe": TranscribeJobManager(max_workers=1, ttl_seconds=0, store_path=stores["transcribe"]),
        "clip": ClipJobManager(max_workers=1, ttl_seconds=0, store_path=stores["clip"]),
    }


@pytest.mark.parametrize("manager_name", ["download", "transcribe", "clip"])
@pytest.mark.parametrize("operation", ["cancel", "dismiss", "sweep"])
def test_terminal_cleanup_preserves_every_published_byte_across_restart(
    tmp_path, monkeypatch, manager_name, operation,
):
    monkeypatch.setattr(jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    monkeypatch.setattr(transcribe_jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    monkeypatch.setattr(clip_jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    artifacts, stores, managers, records = _seed_managers(tmp_path)
    before = artifact_hashes(tmp_path, artifacts)
    manager = managers[manager_name]
    record = records[manager_name]

    if operation == "cancel":
        assert manager.cancel(record.id) is False
        assert manager.get(record.id).status is record.status
    elif operation == "dismiss":
        assert manager.dismiss(record.id) is True
        assert manager.get(record.id).dismissed_at == DISMISSED_AT
    else:
        record.last_accessed = 0
        assert manager.sweep() == 1
        assert manager.get(record.id).dismissed_at == DISMISSED_AT

    assert artifact_hashes(tmp_path, artifacts) == before
    for item in managers.values():
        item.shutdown(wait=True)

    restarted = _restart(stores)
    try:
        for name, item in restarted.items():
            restored = item.get(records[name].id)
            assert restored is not None
            if name == manager_name and operation in {"dismiss", "sweep"}:
                assert restored.dismissed_at == DISMISSED_AT
        assert artifact_hashes(tmp_path, artifacts) == before
    finally:
        for item in restarted.values():
            item.shutdown(wait=True)


def test_clear_finished_dismisses_all_history_without_deleting_artifacts_across_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    monkeypatch.setattr(transcribe_jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    monkeypatch.setattr(clip_jobs, "_utc_now_rfc3339", lambda: DISMISSED_AT, raising=False)
    artifacts, stores, managers, records = _seed_managers(tmp_path)
    source = artifacts[0]
    export = artifacts[-1]
    terminal_records = {
        "download": [
            records["download"],
            Job(
                id="download-error", url="https://example.test/error", title="Errored source",
                status=JobStatus.ERROR, file_path=str(source), filename=source.name,
            ),
            Job(
                id="download-cancelled", url="https://example.test/cancelled",
                title="Cancelled source", status=JobStatus.CANCELLED,
                file_path=str(source), filename=source.name,
            ),
        ],
        "transcribe": [
            records["transcribe"],
            TranscribeJob(
                id="transcribe-error", parent_job_id="download-done", model_used="local.bin",
                status=TranscribeStatus.ERROR,
            ),
            TranscribeJob(
                id="transcribe-cancelled", parent_job_id="download-done",
                model_used="local.bin", status=TranscribeStatus.CANCELLED,
            ),
        ],
        "clip": [
            records["clip"],
            ClipJob(
                id="clip-error", kind="export", source_id="download-done", clip_id="clip-error",
                status=ClipStatus.ERROR, result={"output_path": str(export)},
            ),
            ClipJob(
                id="clip-cancelled", kind="export", source_id="download-done",
                clip_id="clip-cancelled", status=ClipStatus.CANCELLED,
                result={"output_path": str(export)},
            ),
        ],
    }
    expected_statuses = {
        "download": {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED},
        "transcribe": {
            TranscribeStatus.DONE, TranscribeStatus.ERROR, TranscribeStatus.CANCELLED,
        },
        "clip": {ClipStatus.DONE, ClipStatus.ERROR, ClipStatus.CANCELLED},
    }
    for name, manager in managers.items():
        with manager._lock:
            manager._jobs.update({record.id: record for record in terminal_records[name]})
        manager._persist()
    before = artifact_hashes(tmp_path, artifacts)

    for name, manager in managers.items():
        terminal = list(manager.snapshot_jobs())
        assert {record.status for record in terminal} == expected_statuses[name]
        assert all(manager.dismiss(record.id) for record in terminal)
        assert all(manager.get(record.id).dismissed_at == DISMISSED_AT for record in terminal)
    assert artifact_hashes(tmp_path, artifacts) == before
    for item in managers.values():
        item.shutdown(wait=True)

    restarted = _restart(stores)
    try:
        for name, item in restarted.items():
            for record in terminal_records[name]:
                restored = item.get(record.id)
                assert restored is not None
                assert restored.status is record.status
                assert restored.dismissed_at == DISMISSED_AT
        assert artifact_hashes(tmp_path, artifacts) == before
    finally:
        for item in restarted.values():
            item.shutdown(wait=True)
