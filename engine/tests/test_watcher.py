"""Tests for the watch reconciler tick (spec §5 Phase 3). The tick is dependency-injected
(list_items / ingest / transcript_done / produce) so the state machine — detect new → ingest →
produce once transcribed — is tested without real downloads, jobs, or yt-dlp."""
from __future__ import annotations

import watcher
from watcher import reconcile_watch, list_folder_items, list_playlist_items


def test_list_playlist_items_returns_canonical_urls_not_bare_ids(monkeypatch):
    # The reconciler hands each item straight to enqueue_download(url=…), so the listing
    # must yield canonical webpage URLs — bare ids aren't a reliable download target for
    # non-YouTube extractors. Simulate a yt-dlp that prints whichever field is requested.
    captured = {}

    class _Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        field = argv[argv.index("--print") + 1]
        captured["field"] = field
        data = {"id": "aaa\nbbb\n",
                "url": "https://site/watch?v=aaa\nhttps://site/watch?v=bbb\n"}
        return _Result(data.get(field, ""))

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    items = list_playlist_items("https://site/playlist", limit=10)

    assert captured["field"] == "url"                       # asks yt-dlp for the URL, not the id
    assert items == ["https://site/watch?v=aaa", "https://site/watch?v=bbb"]


def test_reconcile_ingests_only_new_items():
    watch = {"id": "w", "enabled": True, "seen": ["old.mp4"], "pending": {}, "produced": []}
    ingested = []
    r = reconcile_watch(watch, list_items=lambda w: ["old.mp4", "new.mp4"],
                        ingest=lambda w, k: (ingested.append(k), f"src-{k}")[1],
                        transcript_done=lambda s: False, produce=lambda w, s: None)
    assert ingested == ["new.mp4"]                                   # the already-seen item is skipped
    assert "new.mp4" in r["seen"] and r["pending"] == {"new.mp4": "src-new.mp4"}
    assert r["produced"] == [] and r["ingested"] == ["src-new.mp4"]


def test_reconcile_produces_only_transcribed_pending_sources():
    watch = {"id": "w", "enabled": True, "seen": ["a.mp4", "b.mp4"],
             "pending": {"a.mp4": "s1", "b.mp4": "s2"}, "produced": []}
    produced = []
    done = {"s1": True, "s2": False}
    r = reconcile_watch(watch, list_items=lambda w: ["a.mp4", "b.mp4"], ingest=lambda w, k: None,
                        transcript_done=lambda s: done.get(s, False), produce=lambda w, s: produced.append(s))
    assert produced == ["s1"]                                        # only the transcribed source produced
    assert r["produced"] == ["s1"] and r["produced_now"] == ["s1"]
    assert r["pending"] == {"b.mp4": "s2"}                           # s1 cleared, s2 still waiting


def test_reconcile_disabled_watch_is_a_noop():
    watch = {"id": "w", "enabled": False, "seen": [], "pending": {}, "produced": []}
    calls = []
    r = reconcile_watch(watch, list_items=lambda w: (calls.append("list"), ["x"])[1],
                        ingest=lambda w, k: calls.append("ingest"), transcript_done=lambda s: True,
                        produce=lambda w, s: calls.append("produce"))
    assert calls == [] and r["ingested"] == [] and r["produced_now"] == []


def test_list_folder_items_finds_videos(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert list_folder_items(str(tmp_path)) == ["a.mp4", "b.mkv"]    # videos only, sorted
    assert list_folder_items(str(tmp_path / "missing")) == []        # missing dir → []
