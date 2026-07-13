from __future__ import annotations

from pathlib import Path


def test_attempt_staging_roots_are_private_and_same_filesystem(tmp_path):
    from attempt_staging import attempt_root

    roots = {
        kind: attempt_root(tmp_path, kind, f"{kind}-job")
        for kind in ("download", "transcribe", "clip")
    }

    assert roots == {
        "download": tmp_path / ".attempts" / "download" / "download-job",
        "transcribe": tmp_path / ".attempts" / "transcribe" / "transcribe-job",
        "clip": tmp_path / ".attempts" / "clip" / "clip-job",
    }
    assert all(root.parent.parent.parent == tmp_path for root in roots.values())


def test_attempt_staging_promotes_and_rewrites_nested_result_paths(tmp_path):
    from attempt_staging import AttemptOutcome, Promotion, commit_outcome

    stage = tmp_path / ".attempts" / "clip" / "job"
    staged_video = stage / "clip-a" / "renders" / "r1.mp4"
    staged_meta = stage / "clip-a" / "meta.json"
    staged_video.parent.mkdir(parents=True)
    staged_video.write_bytes(b"new-render")
    staged_meta.write_text("{}")
    final_video = tmp_path / "clips" / "clip-a" / "renders" / "r1.mp4"
    final_meta = tmp_path / "clips" / "clip-a" / "meta.json"

    outcome = AttemptOutcome(
        updates={
            "result": {
                "output_path": str(staged_video),
                "nested": [str(staged_meta)],
            }
        },
        promotions=(
            Promotion(staged_video, final_video),
            Promotion(staged_meta, final_meta),
        ),
    )

    committed = commit_outcome(outcome)

    assert final_video.read_bytes() == b"new-render"
    assert final_meta.read_text() == "{}"
    assert not staged_video.exists() and not staged_meta.exists()
    assert committed.updates["result"]["output_path"] == str(final_video)
    assert committed.updates["result"]["nested"] == [str(final_meta)]


def test_attempt_cleanup_never_touches_published_sibling(tmp_path):
    from attempt_staging import cleanup_attempt

    published = tmp_path / "source.mp4"
    published.write_bytes(b"published")
    stage = tmp_path / ".attempts" / "download" / "job"
    stage.mkdir(parents=True)
    (stage / "source.mp4.part").write_bytes(b"partial")

    cleanup_attempt(stage)

    assert not stage.exists()
    assert published.read_bytes() == b"published"


def test_tree_promotions_include_only_files_beneath_attempt_root(tmp_path):
    from attempt_staging import tree_promotions

    stage = tmp_path / ".attempts" / "clip" / "job"
    (stage / "clip-a" / "renders").mkdir(parents=True)
    (stage / "clip-a" / "meta.json").write_text("{}")
    (stage / "clip-a" / "renders" / "r.mp4").write_bytes(b"render")

    promotions = tree_promotions(stage, tmp_path / "clips")

    assert {(p.staged, p.final) for p in promotions} == {
        (stage / "clip-a" / "meta.json", tmp_path / "clips" / "clip-a" / "meta.json"),
        (stage / "clip-a" / "renders" / "r.mp4", tmp_path / "clips" / "clip-a" / "renders" / "r.mp4"),
    }
