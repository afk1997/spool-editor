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


def _ok_status(_job):
    return "done"


def test_reconcile_ingests_only_new_items():
    watch = {"id": "w", "enabled": True, "seen": ["old.mp4"], "pending": {}, "produced": [], "producing": {}}
    ingested = []
    r = reconcile_watch(watch, list_items=lambda w: ["old.mp4", "new.mp4"],
                        ingest=lambda w, k: (ingested.append(k), f"src-{k}")[1],
                        transcript_done=lambda s: False, produce=lambda w, s: None,
                        produce_status=_ok_status)
    assert ingested == ["new.mp4"]                                   # the already-seen item is skipped
    assert "new.mp4" in r["seen"] and r["pending"] == {"new.mp4": "src-new.mp4"}
    assert r["produced"] == [] and r["ingested"] == ["src-new.mp4"]


def test_reconcile_marks_produced_only_when_the_produce_job_completes():
    # Regression for the premature-'produced' bug: produce() only ENQUEUES an async job, so a
    # source must move to 'producing' (tracking the job), and become 'produced' ONLY once that
    # job reports done — never the instant it was enqueued (which silently lost failures).
    statuses, enqueued = {}, []

    def produce(w, sid):
        jid = f"job-{len(enqueued)}"
        enqueued.append((sid, jid))
        statuses[jid] = "running"
        return jid

    def status(jid):
        return statuses.get(jid)

    watch = {"id": "w", "enabled": True, "seen": ["a.mp4"], "pending": {"a.mp4": "s1"},
             "produced": [], "producing": {}}
    # tick 1: transcript done → produce enqueued, job still running → NOT produced yet
    r1 = reconcile_watch(watch, list_items=lambda w: ["a.mp4"], ingest=lambda w, k: None,
                         transcript_done=lambda s: True, produce=produce, produce_status=status)
    assert enqueued == [("s1", "job-0")]
    assert r1["produced"] == [] and r1["produced_now"] == []
    assert r1["producing"].get("s1", {}).get("job") == "job-0"
    assert r1["pending"] == {}                                        # moved out of pending

    # tick 2: the produce job finished → now produced (and not re-enqueued)
    statuses["job-0"] = "done"
    w2 = {"id": "w", "enabled": True, **{k: r1[k] for k in ("seen", "pending", "produced", "producing")}}
    r2 = reconcile_watch(w2, list_items=lambda w: ["a.mp4"], ingest=lambda w, k: None,
                         transcript_done=lambda s: True, produce=produce, produce_status=status)
    assert r2["produced"] == ["s1"] and r2["produced_now"] == ["s1"]
    assert "s1" not in r2["producing"]
    assert enqueued == [("s1", "job-0")]                             # not produced twice


def test_reconcile_retries_a_failed_produce_then_gives_up_after_max_attempts():
    from watcher import _MAX_PRODUCE_ATTEMPTS
    statuses, enqueued = {}, []

    def produce(w, sid):
        jid = f"job-{len(enqueued)}"
        enqueued.append(jid)
        statuses[jid] = "error"                                      # every produce fails outright
        return jid

    state = {"seen": ["a.mp4"], "pending": {"a.mp4": "s1"}, "produced": [], "producing": {}}
    for _ in range(_MAX_PRODUCE_ATTEMPTS + 3):
        watch = {"id": "w", "enabled": True, **state}
        r = reconcile_watch(watch, list_items=lambda w: ["a.mp4"], ingest=lambda w, k: None,
                            transcript_done=lambda s: True, produce=produce,
                            produce_status=lambda j: statuses.get(j))
        state = {k: r[k] for k in ("seen", "pending", "produced", "producing")}

    assert len(enqueued) == _MAX_PRODUCE_ATTEMPTS                    # retried, but bounded
    assert state["produced"] == []                                   # never falsely marked produced
    assert state["producing"] == {}                                  # abandoned after exhausting retries


def test_reconcile_does_not_retry_a_cancelled_produce():
    # A user-cancelled produce is an intent signal, not a transient failure — don't auto-retry it.
    statuses, enqueued = {}, []

    def produce(w, sid):
        jid = f"job-{len(enqueued)}"
        enqueued.append(jid)
        statuses[jid] = "cancelled"
        return jid

    watch = {"id": "w", "enabled": True, "seen": ["a.mp4"], "pending": {"a.mp4": "s1"},
             "produced": [], "producing": {}}
    r = reconcile_watch(watch, list_items=lambda w: ["a.mp4"], ingest=lambda w, k: None,
                        transcript_done=lambda s: True, produce=produce,
                        produce_status=lambda j: statuses.get(j))
    assert enqueued == ["job-0"]                                     # enqueued once, NOT retried
    assert r["produced"] == [] and r["producing"] == {}             # dropped, not produced


def test_reconcile_disabled_watch_is_a_noop():
    watch = {"id": "w", "enabled": False, "seen": [], "pending": {}, "produced": [], "producing": {}}
    calls = []
    r = reconcile_watch(watch, list_items=lambda w: (calls.append("list"), ["x"])[1],
                        ingest=lambda w, k: calls.append("ingest"), transcript_done=lambda s: True,
                        produce=lambda w, s: calls.append("produce"), produce_status=_ok_status)
    assert calls == [] and r["ingested"] == [] and r["produced_now"] == []


def test_list_folder_items_finds_videos(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert list_folder_items(str(tmp_path)) == ["a.mp4", "b.mkv"]    # videos only, sorted
    assert list_folder_items(str(tmp_path / "missing")) == []        # missing dir → []
