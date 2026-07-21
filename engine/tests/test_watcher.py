"""Tests for the watch reconciler tick (spec §5 Phase 3). The tick is dependency-injected
(list_items / ingest / transcript_done / produce) so the state machine — detect new → ingest →
produce once transcribed — is tested without real downloads, jobs, or yt-dlp."""
from __future__ import annotations

import os
import time
import types

import pytest
import watcher
from network_policy import NetworkPolicy, NetworkPolicyError
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
    items = list_playlist_items("https://site/playlist", limit=10,
                                network_policy=NetworkPolicy())

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


def test_reconcile_retries_a_transient_ingest_failure_instead_of_dropping_it():
    # Regression for the drop-on-transient-failure bug: the key used to be marked seen BEFORE ingest,
    # so an ingest that returned None (a rare swallowed transient error) was dropped forever. Now a
    # failed ingest is NOT marked seen — it's retried on a later tick, and succeeds once ingest does.
    outcomes = iter([None, None, "src-x"])                            # fail, fail, then succeed
    attempts = []

    def ingest(w, k):
        attempts.append(k)
        return next(outcomes)

    state = {"seen": [], "pending": {}, "produced": [], "producing": {}}
    for _ in range(3):
        watch = {"id": "w", "enabled": True, **state}
        r = reconcile_watch(watch, list_items=lambda w: ["x.mp4"], ingest=ingest,
                            transcript_done=lambda s: False, produce=lambda w, s: None,
                            produce_status=_ok_status)
        state = {k: r[k] for k in ("seen", "ingesting", "pending", "produced", "producing")}

    assert attempts == ["x.mp4", "x.mp4", "x.mp4"]                    # retried each tick, not dropped
    assert r["seen"] == ["x.mp4"]                                     # marked seen only once it landed
    assert r["pending"] == {"x.mp4": "src-x"}                         # now awaiting transcript
    assert r["ingesting"] == {}                                       # retry count cleared on success
    assert r["ingested"] == ["src-x"]


def test_reconcile_gives_up_on_a_persistently_bad_ingest_after_max_attempts():
    from watcher import _MAX_INGEST_ATTEMPTS
    # A permanently-bad item (ingest always fails) must NOT retry forever: after the bounded number
    # of ticks it's marked seen and abandoned, so the watch stops hammering it.
    attempts = []

    def ingest(w, k):
        attempts.append(k)
        return None                                                  # never succeeds

    state = {"seen": [], "pending": {}, "produced": [], "producing": {}}
    for _ in range(_MAX_INGEST_ATTEMPTS + 3):
        watch = {"id": "w", "enabled": True, **state}
        r = reconcile_watch(watch, list_items=lambda w: ["bad.mp4"], ingest=ingest,
                            transcript_done=lambda s: False, produce=lambda w, s: None,
                            produce_status=_ok_status)
        state = {k: r[k] for k in ("seen", "ingesting", "pending", "produced", "producing")}

    assert len(attempts) == _MAX_INGEST_ATTEMPTS                      # retried, but bounded
    assert r["seen"] == ["bad.mp4"]                                   # given up → marked seen
    assert r["ingesting"] == {}                                       # retry marker cleared on give-up
    assert r["pending"] == {}                                         # nothing left dangling
    assert r["ingested"] == []                                        # nothing ever ingested


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

    # Start with the source ALREADY producing (job-0, attempt 1) so each tick is a pure RETRY of an
    # in-flight job — no step-2 enqueue muddies the per-tick count. The test controls the per-tick
    # status (always "error"), so we can assert ONE retry per tick: exactly one new enqueue and the
    # attempt count up by exactly one each tick (catches a same-tick retry-storm), bounded overall.
    statuses["job-0"] = "error"
    enqueued.append("job-0")
    state = {"seen": ["a.mp4"], "pending": {}, "produced": [],
             "producing": {"s1": {"job": "job-0", "attempts": 1}}}
    for tick in range(_MAX_PRODUCE_ATTEMPTS + 3):
        before_enqueued = len(enqueued)
        prev_attempts = state["producing"].get("s1", {}).get("attempts")
        watch = {"id": "w", "enabled": True, **state}
        r = reconcile_watch(watch, list_items=lambda w: ["a.mp4"], ingest=lambda w, k: None,
                            transcript_done=lambda s: True, produce=produce,
                            produce_status=lambda j: statuses.get(j))
        new_this_tick = len(enqueued) - before_enqueued
        if prev_attempts is not None and prev_attempts < _MAX_PRODUCE_ATTEMPTS:
            # still has retry budget: exactly one re-enqueue, attempt count up by exactly one.
            assert new_this_tick == 1
            assert r["producing"]["s1"]["attempts"] == prev_attempts + 1
        else:
            # exhausted (or already abandoned): no new enqueue, source dropped from producing.
            assert new_this_tick == 0
            assert "s1" not in r["producing"]
        state = {k: r[k] for k in ("seen", "pending", "produced", "producing")}

    # job-0 (initial) + one re-enqueue per retry tick until the budget runs out → _MAX_PRODUCE_ATTEMPTS.
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
    old = time.time() - 3600
    for name, content in [("a.mp4", b"x"), ("b.mkv", b"x"), ("notes.txt", None)]:
        p = tmp_path / name
        if content is not None:
            p.write_bytes(content)
        else:
            p.write_text("x")
        os.utime(p, (old, old))
    assert list_folder_items(str(tmp_path)) == ["a.mp4", "b.mkv"]    # videos only, sorted
    assert list_folder_items(str(tmp_path / "missing")) == []        # missing dir → []


def test_list_playlist_items_uses_separator_and_rejects_option_shaped(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(stdout="https://youtu.be/x\n", returncode=0)
    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    policy = NetworkPolicy()
    items = watcher.list_playlist_items("https://example.com/playlist", network_policy=policy)
    assert items == ["https://youtu.be/x"]
    argv = calls[0]
    sep = argv.index("--")
    assert argv[sep + 1] == "https://example.com/playlist"  # target can never parse as a flag

    # an option-shaped target (e.g. --config-location=...) must never reach a subprocess
    assert watcher.list_playlist_items("--config-location=/tmp/evil", network_policy=policy) == []
    assert len(calls) == 1


def test_list_folder_items_skips_files_still_being_written(tmp_path):
    settled = tmp_path / "done.mp4"
    settled.write_bytes(b"x" * 64)
    old = time.time() - 3600
    os.utime(settled, (old, old))
    fresh = tmp_path / "copying.mp4"
    fresh.write_bytes(b"x" * 64)                       # mtime = now → still settling
    partial = tmp_path / "grab.mp4.part"
    partial.write_bytes(b"x")

    assert watcher.list_folder_items(str(tmp_path)) == ["done.mp4"]
    # the fresh file is only deferred — it appears once its mtime stops moving
    assert watcher.list_folder_items(str(tmp_path), settle_seconds=0) == ["copying.mp4", "done.mp4"]


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    watcher.clear_listing_cache()
    yield
    watcher.clear_listing_cache()


def test_playlist_listing_is_cached_within_ttl(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(stdout="https://youtu.be/a\n", returncode=0)
    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    url = "https://93.184.216.34/list"
    policy = NetworkPolicy()
    assert watcher.list_playlist_items(url, now=1000.0, network_policy=policy) == ["https://youtu.be/a"]
    assert watcher.list_playlist_items(url, now=1100.0, network_policy=policy) == ["https://youtu.be/a"]
    assert len(calls) == 1                                  # second tick hit the cache
    watcher.list_playlist_items(url, now=1000.0 + watcher._LISTING_TTL + 1,
                                network_policy=policy)
    assert len(calls) == 2                                  # expired → refetched


def test_failed_listing_backs_off_exponentially(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(stdout="", returncode=0)   # dead URL: empty listing
    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    url = "https://93.184.216.34/dead"
    t0 = 1000.0
    policy = NetworkPolicy()
    assert watcher.list_playlist_items(url, now=t0, network_policy=policy) == []
    assert watcher.list_playlist_items(url, now=t0 + 10, network_policy=policy) == []
    assert len(calls) == 1                                  # inside the first backoff window
    watcher.list_playlist_items(url, now=t0 + watcher._LISTING_TTL + 1,
                                network_policy=policy)   # window over → retry
    assert len(calls) == 2
    # second consecutive failure doubles the window
    watcher.list_playlist_items(url, now=t0 + watcher._LISTING_TTL + 2,
                                network_policy=policy)
    assert len(calls) == 2


def test_invalidate_listing_forces_a_fresh_fetch(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(stdout="https://youtu.be/a\n", returncode=0)
    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    url = "https://93.184.216.34/list"
    policy = NetworkPolicy()
    watcher.list_playlist_items(url, now=1000.0, network_policy=policy)
    watcher.invalidate_listing(url)
    watcher.list_playlist_items(url, now=1001.0, network_policy=policy)
    assert len(calls) == 2


def test_playlist_listing_holds_lease_before_cache_and_through_cache_update(monkeypatch):
    policy = NetworkPolicy()
    events = []

    class _LeaseCheckedCache(dict):
        def get(self, key, default=None):
            events.append(("get", policy.active_leases))
            return super().get(key, default)

        def __setitem__(self, key, value):
            events.append(("set", policy.active_leases))
            return super().__setitem__(key, value)

    monkeypatch.setattr(watcher, "_listing_cache", _LeaseCheckedCache())

    def fake_run(*_args, **_kwargs):
        events.append(("run", policy.active_leases))
        with pytest.raises(NetworkPolicyError) as blocked:
            policy.enable_offline()
        assert blocked.value.code == "network_work_active"
        return types.SimpleNamespace(stdout="https://youtu.be/a\n", returncode=0)

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    assert watcher.list_playlist_items(
        "https://93.184.216.34/list", network_policy=policy,
    ) == ["https://youtu.be/a"]
    assert events == [("get", 1), ("run", 1), ("set", 1)]
    assert policy.active_leases == 0


def test_offline_playlist_listing_rejects_before_warm_cache_or_subprocess(monkeypatch):
    policy = NetworkPolicy(offline=True)
    target = "https://93.184.216.34/list"

    class _ObservedCache(dict):
        reads = 0

        def get(self, key, default=None):
            self.reads += 1
            return super().get(key, default)

    cache = _ObservedCache({
        (target, 30): {"items": ["https://youtu.be/cached"], "expires": float("inf"), "fails": 0},
    })
    before = {key: dict(value) for key, value in cache.items()}
    subprocess_calls = []
    monkeypatch.setattr(watcher, "_listing_cache", cache)
    monkeypatch.setattr(
        watcher.subprocess, "run", lambda *a, **kw: subprocess_calls.append((a, kw)),
    )

    with pytest.raises(NetworkPolicyError) as denied:
        watcher.list_playlist_items(target, network_policy=policy)

    assert denied.value.code == "offline_network_disabled"
    assert denied.value.purpose == "watch_listing"
    assert cache.reads == 0
    assert cache == before
    assert subprocess_calls == []
    assert policy.active_leases == 0


def test_playlist_listing_reraises_policy_denial_and_releases_outer_lease(monkeypatch):
    policy = NetworkPolicy()
    denial = NetworkPolicyError("offline_network_disabled", purpose="nested_test")
    monkeypatch.setattr(watcher.subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(denial))

    with pytest.raises(NetworkPolicyError) as raised:
        watcher.list_playlist_items(
            "https://93.184.216.34/list", network_policy=policy,
        )

    assert raised.value is denial
    assert watcher._listing_cache == {}
    assert policy.active_leases == 0


def test_failed_playlist_listing_releases_lease_and_updates_backoff_inside_it(monkeypatch):
    policy = NetworkPolicy()
    updates = []

    class _LeaseCheckedCache(dict):
        def __setitem__(self, key, value):
            updates.append(policy.active_leases)
            return super().__setitem__(key, value)

    monkeypatch.setattr(watcher, "_listing_cache", _LeaseCheckedCache())

    def fail(*_args, **_kwargs):
        assert policy.active_leases == 1
        raise OSError("yt-dlp unavailable")

    monkeypatch.setattr(watcher.subprocess, "run", fail)

    assert watcher.list_playlist_items(
        "https://93.184.216.34/dead", network_policy=policy,
    ) == []
    assert updates == [1]
    assert policy.active_leases == 0
