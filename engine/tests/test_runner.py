import os
import signal
import subprocess
import sys
import threading
import time
import pytest
import runner
from runner import build_info_argv, build_download_argv
from network_policy import NetworkPolicy, NetworkPolicyError


@pytest.fixture(autouse=True)
def _public_url_validation(monkeypatch):
    """Runner unit tests exercise process contracts, not live DNS."""
    monkeypatch.setattr(runner, "_is_safe_url_unleased", lambda _url: True, raising=False)


@pytest.fixture()
def online_policy():
    return NetworkPolicy()


def test_info_argv_dash_dash_separator():
    argv = build_info_argv("https://example.com/video")
    assert argv[-2:] == ["--", "https://example.com/video"]
    assert argv[0].endswith("yt-dlp")  # bare name or a resolved venv path (_ytdlp_bin)
    assert "--no-playlist" in argv
    assert "-j" in argv


def test_info_argv_injects_cookies_when_env_set(monkeypatch):
    monkeypatch.setenv("TROVE_COOKIES_FROM_BROWSER", "safari")
    argv = build_info_argv("https://example.com/video")
    assert "--cookies-from-browser" in argv
    assert argv[argv.index("--cookies-from-browser") + 1] == "safari"


def test_info_argv_ignores_blank_cookie_env(monkeypatch):
    monkeypatch.setenv("TROVE_COOKIES_FROM_BROWSER", "")
    argv = build_info_argv("https://example.com/video")
    assert "--cookies-from-browser" not in argv


def test_download_argv_audio_mode(tmp_path):
    argv = build_download_argv(
        url="https://example.com/v",
        out_template=str(tmp_path / "out.%(ext)s"),
        format_choice="audio",
        format_id=None,
    )
    assert "-x" in argv
    assert "--audio-format" in argv
    assert argv[argv.index("--audio-format") + 1] == "mp3"
    assert argv[-2:] == ["--", "https://example.com/v"]


def test_download_argv_video_with_format_id(tmp_path):
    argv = build_download_argv(
        url="https://example.com/v",
        out_template=str(tmp_path / "out.%(ext)s"),
        format_choice="video",
        format_id="137",
    )
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "137+bestaudio/best"
    assert "--merge-output-format" in argv
    assert argv[argv.index("--merge-output-format") + 1] == "mp4"
    assert argv[-2:] == ["--", "https://example.com/v"]


def test_download_argv_video_default_format(tmp_path):
    argv = build_download_argv(
        url="https://example.com/v",
        out_template=str(tmp_path / "out.%(ext)s"),
        format_choice="video",
        format_id=None,
    )
    assert argv[argv.index("-f") + 1] == "bestvideo+bestaudio/best"


def test_download_argv_rejects_argv_lookalike_url():
    with pytest.raises(ValueError):
        build_download_argv(
            url="--exec=touch /tmp/pwned",
            out_template="x",
            format_choice="video",
            format_id=None,
        )


from runner import classify_error


@pytest.mark.parametrize("stderr,expected", [
    ("ERROR: Unsupported URL: foo", "unsupported_url"),
    ("ERROR: [youtube] Video unavailable", "private_or_unavailable"),
    ("ERROR: Private video. Sign in if you've been granted access", "private_or_unavailable"),
    ("ERROR: Sign in to confirm your age", "auth_required"),
    ("ERROR: This video is not available in your country", "geo_restricted"),
    ("ERROR: HTTP Error 403: Forbidden", "auth_required"),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: HTTP Error 404: Not Found", "private_or_unavailable"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "auth_required"),
    ("ERROR: [generic] some weird thing", "unknown"),
    ("ERROR: Unable to connect to proxy", "network"),
    ("ERROR: Read timed out.", "timeout"),
    ("", "unknown"),
])
def test_classify_error(stderr, expected):
    assert classify_error(stderr) == expected


import json
from unittest.mock import patch
from runner import run_info, InfoResult


def test_run_info_success(monkeypatch, online_policy):
    fake_stdout = json.dumps({
        "title": "T",
        "thumbnail": "https://x/y.jpg",
        "duration": 30,
        "uploader": "U",
        "formats": [
            {"format_id": "137", "height": 1080, "vcodec": "avc1", "tbr": 5000},
            {"format_id": "136", "height": 720, "vcodec": "avc1", "tbr": 2500},
        ],
    })

    class FakeCompleted:
        returncode = 0
        stdout = fake_stdout
        stderr = ""

    monkeypatch.setattr("runner.subprocess.run", lambda *a, **kw: FakeCompleted())

    res = run_info("https://example.com/v", network_policy=online_policy)
    assert isinstance(res, InfoResult)
    assert res.title == "T"
    assert res.uploader == "U"
    assert res.duration == 30
    assert len(res.formats) == 2
    assert res.formats[0]["height"] == 1080
    assert res.formats[0]["label"] == "1080p"
    assert online_policy.active_leases == 0


def test_run_info_handles_multiline_stdout(monkeypatch, online_policy):
    obj = {"title": "first", "thumbnail": "", "duration": 0, "uploader": "", "formats": []}
    fake = json.dumps(obj) + "\n" + json.dumps({"title": "second"})

    class FakeCompleted:
        returncode = 0
        stdout = fake
        stderr = ""

    monkeypatch.setattr("runner.subprocess.run", lambda *a, **kw: FakeCompleted())

    res = run_info("https://example.com/v", network_policy=online_policy)
    assert res.title == "first"


def test_run_info_returns_error_on_nonzero(monkeypatch, online_policy):
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "ERROR: HTTP Error 403: Forbidden"

    monkeypatch.setattr("runner.subprocess.run", lambda *a, **kw: FakeCompleted())

    res = run_info("https://example.com/v", network_policy=online_policy)
    assert res.error_category == "auth_required"
    assert res.title is None
    assert online_policy.active_leases == 0


def test_run_info_offline_invokes_neither_dns_nor_subprocess(monkeypatch):
    policy = NetworkPolicy(offline=True)
    dns_calls = []
    process_calls = []
    monkeypatch.setattr(runner, "_is_safe_url_unleased", lambda url: dns_calls.append(url))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: process_calls.append((a, kw)))

    with pytest.raises(NetworkPolicyError) as denied:
        run_info("https://example.com/v", network_policy=policy)

    assert denied.value.error_category == "offline_network_disabled"
    assert dns_calls == []
    assert process_calls == []
    assert policy.active_leases == 0


def test_run_info_online_holds_one_lease_and_releases_on_error(monkeypatch):
    policy = NetworkPolicy()

    def validate(_url):
        assert policy.active_leases == 1
        return True

    def fail(*_args, **_kwargs):
        assert policy.active_leases == 1
        raise OSError("spawn failed")

    monkeypatch.setattr(runner, "_is_safe_url_unleased", validate)
    monkeypatch.setattr(runner.subprocess, "run", fail)

    with pytest.raises(OSError, match="spawn failed"):
        run_info("https://example.com/v", network_policy=policy)
    assert policy.active_leases == 0


from runner import run_download, DownloadResult


def _blocking_popen_factory(
    *, returncode=0, stdout="", stderr="", communicate_error=None, seen=None,
):
    class FakeBlockingProc:
        def __init__(self, *_args, **_kwargs):
            self.returncode = returncode

        def communicate(self, timeout=None):
            if seen is not None:
                seen["timeout"] = timeout
            if communicate_error is not None:
                raise communicate_error
            return stdout, stderr

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    return FakeBlockingProc


def test_run_download_success(monkeypatch, tmp_path, online_policy):
    out_template = str(tmp_path / "abc.%(ext)s")
    target = tmp_path / "abc.mp4"
    target.write_bytes(b"fakempegdata")

    monkeypatch.setattr(runner.subprocess, "Popen", _blocking_popen_factory())

    res = run_download(
        url="https://example.com/v",
        out_template=out_template,
        format_choice="video",
        format_id=None,
        network_policy=online_policy,
    )
    assert isinstance(res, DownloadResult)
    assert res.error_category is None
    assert res.file_path == str(target)
    assert online_policy.active_leases == 0


def test_run_download_audio_must_be_mp3(monkeypatch, tmp_path, online_policy):
    out_template = str(tmp_path / "abc.%(ext)s")
    leftover = tmp_path / "abc.webm"
    leftover.write_bytes(b"x")

    monkeypatch.setattr(runner.subprocess, "Popen", _blocking_popen_factory())

    res = run_download(
        url="https://example.com/v",
        out_template=out_template,
        format_choice="audio",
        format_id=None,
        network_policy=online_policy,
    )
    assert res.error_category == "unknown"
    assert "mp3" in (res.error_raw or "").lower()


def test_run_download_cleans_orphans_on_timeout(monkeypatch, tmp_path, online_policy):
    out_template = str(tmp_path / "abc.%(ext)s")
    (tmp_path / "abc.part").write_bytes(b"x")
    (tmp_path / "abc.webm").write_bytes(b"x")

    monkeypatch.setattr(
        runner.subprocess, "Popen",
        _blocking_popen_factory(
            communicate_error=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1),
        ),
    )

    res = run_download(
        url="https://example.com/v",
        out_template=out_template,
        format_choice="video",
        format_id=None,
        network_policy=online_policy,
    )
    assert res.error_category == "timeout"
    assert not (tmp_path / "abc.part").exists()
    assert not (tmp_path / "abc.webm").exists()


@pytest.mark.parametrize("streaming", [False, True])
def test_run_download_offline_invokes_neither_dns_nor_process(
    monkeypatch, tmp_path, streaming,
):
    policy = NetworkPolicy(offline=True)
    dns_calls = []
    run_calls = []
    popen_calls = []
    monkeypatch.setattr(runner, "_is_safe_url_unleased", lambda url: dns_calls.append(url))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: run_calls.append((a, kw)))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw)))
    kwargs = {}
    if streaming:
        kwargs = {"progress_cb": lambda *_a: None}

    with pytest.raises(NetworkPolicyError) as denied:
        run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "x.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
            **kwargs,
        )

    assert denied.value.code == "offline_network_disabled"
    assert dns_calls == []
    assert run_calls == []
    assert popen_calls == []
    assert policy.active_leases == 0


def test_run_download_online_releases_lease_after_blocking_error(monkeypatch, tmp_path):
    policy = NetworkPolicy()
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        _blocking_popen_factory(returncode=1, stderr="boom"),
    )

    result = run_download(
        url="https://example.com/v",
        out_template=str(tmp_path / "x.%(ext)s"),
        format_choice="video",
        format_id=None,
        network_policy=policy,
    )

    assert result.error_category == "unknown"
    assert policy.active_leases == 0


def test_blocking_communicate_error_reaps_fake_process_before_lease_release(
    monkeypatch, tmp_path,
):
    policy = NetworkPolicy()
    events = []

    class FakeProcessWithoutPosixPid:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, timeout=None):
            assert policy.active_leases == 1
            raise RuntimeError("pipe failed")

        def poll(self):
            return self.returncode

        def kill(self):
            assert policy.active_leases == 1
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            assert policy.active_leases == 1
            events.append("wait")
            return self.returncode

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcessWithoutPosixPid)

    with pytest.raises(RuntimeError, match="pipe failed"):
        run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "x.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
        )

    assert events == ["kill", "wait"]
    assert policy.active_leases == 0


def test_streaming_download_keeps_lease_until_spawned_process_is_reaped_on_callback_error(
    monkeypatch, tmp_path,
):
    policy = NetworkPolicy()
    events = []

    class FakeProc:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.stdout = iter([])
            self.stderr = iter([])

        def poll(self):
            return self.returncode

        def kill(self):
            assert policy.active_leases == 1
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            assert policy.active_leases == 1
            events.append("wait")
            return self.returncode

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProc)

    with pytest.raises(RuntimeError, match="register failed"):
        run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "x.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
            register_process=lambda _proc: (_ for _ in ()).throw(RuntimeError("register failed")),
        )

    assert events == ["kill", "wait"]
    assert policy.active_leases == 0


def _slow_reap_proc_factory(policy, events):
    class SlowReapProc:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.stdout = iter([])
            self.stderr = iter([])

        def poll(self):
            return self.returncode

        def kill(self):
            assert policy.active_leases == 1
            events.append("kill")

        def wait(self, timeout=None):
            assert policy.active_leases == 1
            if timeout is not None:
                events.append("timed_wait")
                raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=timeout)
            events.append("blocking_wait")
            self.returncode = -9
            return self.returncode

    return SlowReapProc


def test_streaming_timeout_blocks_until_child_is_reaped_after_timed_wait_expires(
    monkeypatch, tmp_path,
):
    policy = NetworkPolicy()
    events = []
    monkeypatch.setattr(runner.subprocess, "Popen", _slow_reap_proc_factory(policy, events))

    result = run_download(
        url="https://example.com/v",
        out_template=str(tmp_path / "x.%(ext)s"),
        format_choice="video",
        format_id=None,
        network_policy=policy,
        timeout=0,
        progress_cb=lambda *_args: None,
    )

    assert result.error_category == "timeout"
    assert events == ["kill", "timed_wait", "blocking_wait"]
    assert policy.active_leases == 0


def test_exceptional_cleanup_blocks_until_child_is_reaped_after_timed_wait_expires(
    monkeypatch, tmp_path,
):
    policy = NetworkPolicy()
    events = []
    monkeypatch.setattr(runner.subprocess, "Popen", _slow_reap_proc_factory(policy, events))

    with pytest.raises(RuntimeError, match="register failed"):
        run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "x.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
            register_process=lambda _proc: (_ for _ in ()).throw(RuntimeError("register failed")),
        )

    assert events == ["kill", "timed_wait", "blocking_wait"]
    assert policy.active_leases == 0


def _process_tree_helper(tmp_path):
    helper = tmp_path / "spawn_tree.py"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
        "with open(os.environ['TREE_PID_FILE'], 'w') as handle:\n"
        "    handle.write(f'{os.getpid()} {child.pid}')\n"
        "    handle.flush()\n"
        "    os.fsync(handle.fileno())\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o755)
    return helper


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed grandchild may briefly remain as a zombie until launchd/init reaps it;
    # it cannot perform network work and is therefore no longer a live descendant.
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    )
    state = status.stdout.strip()
    return status.returncode == 0 and bool(state) and not state.startswith("Z")


def _wait_for_pids_gone(pids, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return not any(_pid_alive(pid) for pid in pids)


def _cleanup_test_pids(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_for_pids_gone(pids)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_blocking_timeout_terminates_real_download_process_tree_before_lease_release(
    monkeypatch, tmp_path,
):
    helper = _process_tree_helper(tmp_path)
    pid_file = tmp_path / "tree.pids"
    monkeypatch.setenv("TREE_PID_FILE", str(pid_file))
    monkeypatch.setattr(runner, "build_download_argv", lambda **_kwargs: [str(helper)])
    real_spawn = runner._spawn_download_process

    def spawn_then_timeout(argv, **kwargs):
        process = real_spawn(argv, **kwargs)
        real_communicate = process.communicate

        def communicate_after_tree_started(timeout=None):
            deadline = time.time() + 3
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.01)
            assert pid_file.exists(), "process tree helper did not start"
            return real_communicate(timeout=0)

        process.communicate = communicate_after_tree_started
        return process

    monkeypatch.setattr(runner, "_spawn_download_process", spawn_then_timeout)
    policy = NetworkPolicy()
    pids = []

    try:
        result = run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "out.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
            timeout=10,
        )
        pids = [int(value) for value in pid_file.read_text().split()]

        assert result.error_category == "timeout"
        assert policy.active_leases == 0
        assert _wait_for_pids_gone(pids)
    finally:
        _cleanup_test_pids(pids)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_registered_streaming_handle_kills_real_process_tree_before_lease_release(
    monkeypatch, tmp_path,
):
    helper = _process_tree_helper(tmp_path)
    pid_file = tmp_path / "tree.pids"
    monkeypatch.setenv("TREE_PID_FILE", str(pid_file))
    monkeypatch.setattr(runner, "build_download_argv", lambda **_kwargs: [str(helper)])
    policy = NetworkPolicy()
    pids = []
    killer = None

    def register(handle):
        nonlocal killer

        def cancel_tree():
            deadline = time.time() + 2
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.01)
            handle.kill()

        killer = threading.Thread(target=cancel_tree)
        killer.start()

    try:
        result = run_download(
            url="https://example.com/v",
            out_template=str(tmp_path / "out.%(ext)s"),
            format_choice="video",
            format_id=None,
            network_policy=policy,
            register_process=register,
            was_paused_check=lambda: True,
        )
        killer.join(timeout=2)
        pids = [int(value) for value in pid_file.read_text().split()]

        assert result.error_category == "cancelled"
        assert policy.active_leases == 0
        assert _wait_for_pids_gone(pids)
    finally:
        if killer is not None:
            killer.join(timeout=2)
        _cleanup_test_pids(pids)


def test_download_argv_includes_concurrent_fragments():
    argv = build_download_argv(
        url="https://example.com/v",
        out_template="/tmp/x.%(ext)s",
        format_choice="video",
        format_id=None,
    )
    idx = argv.index("--concurrent-fragments")
    assert argv[idx + 1] == "4"  # default


def test_download_argv_concurrent_fragments_env_clamps_high(monkeypatch):
    monkeypatch.setenv("TROVE_CONCURRENT_FRAGMENTS", "100")
    argv = build_download_argv(
        url="https://example.com/v", out_template="/tmp/x.%(ext)s",
        format_choice="video", format_id=None,
    )
    idx = argv.index("--concurrent-fragments")
    assert argv[idx + 1] == "32"  # clamped to max


def test_download_argv_concurrent_fragments_env_clamps_low(monkeypatch):
    monkeypatch.setenv("TROVE_CONCURRENT_FRAGMENTS", "0")
    argv = build_download_argv(
        url="https://example.com/v", out_template="/tmp/x.%(ext)s",
        format_choice="video", format_id=None,
    )
    idx = argv.index("--concurrent-fragments")
    assert argv[idx + 1] == "1"  # clamped to min


def test_download_argv_concurrent_fragments_env_handles_garbage(monkeypatch):
    """Non-int env var should fall back to default 4, not crash."""
    monkeypatch.setenv("TROVE_CONCURRENT_FRAGMENTS", "not-a-number")
    argv = build_download_argv(
        url="https://example.com/v", out_template="/tmp/x.%(ext)s",
        format_choice="video", format_id=None,
    )
    idx = argv.index("--concurrent-fragments")
    assert argv[idx + 1] == "4"  # fallback to default


def test_download_argv_includes_retry_flags():
    argv = build_download_argv(
        url="https://example.com/v",
        out_template="/tmp/x.%(ext)s",
        format_choice="video",
        format_id=None,
    )
    r_idx = argv.index("--retries")
    fr_idx = argv.index("--fragment-retries")
    assert argv[r_idx + 1] == "5"
    assert argv[fr_idx + 1] == "10"


def test_run_download_skips_cleanup_when_was_paused(monkeypatch, tmp_path, online_policy):
    """When the caller flags the job as paused, .part files must be preserved."""
    from runner import run_download
    out_template = str(tmp_path / "abc.%(ext)s")
    part_file = tmp_path / "abc.mp4.part"
    part_file.write_bytes(b"partial bytes")
    other_part = tmp_path / "abc.webm"
    other_part.write_bytes(b"x")

    # Build a fake Popen that returns non-zero (as if it was killed)
    class FakeProc:
        returncode = -9
        def __init__(self, *a, **kw):
            self.stdout = iter([])
            self.stderr = iter([])
        def poll(self):
            return -9
        def wait(self, timeout=None):
            return -9
        def kill(self):
            pass

    monkeypatch.setattr("runner.subprocess.Popen", FakeProc)

    # State set by JobManager.pause() before kill
    pause_signal = {"was_paused": True}
    def progress_cb(*args, **kwargs):
        pass
    def register_process(proc):
        pass

    # The streaming path needs to know it was paused. We'll signal via a
    # sentinel kwarg threaded through.
    res = run_download(
        url="https://example.com/v",
        out_template=out_template,
        format_choice="video",
        format_id=None,
        progress_cb=progress_cb,
        register_process=register_process,
        was_paused_check=lambda: pause_signal["was_paused"],
        network_policy=online_policy,
    )
    assert part_file.exists()  # NOT cleaned up
    assert other_part.exists()  # NOT cleaned up
    assert online_policy.active_leases == 0


def test_run_download_runs_cleanup_when_not_paused(monkeypatch, tmp_path, online_policy):
    """When the failure was a real error, cleanup runs as before."""
    from runner import run_download
    out_template = str(tmp_path / "abc.%(ext)s")
    part_file = tmp_path / "abc.mp4.part"
    part_file.write_bytes(b"partial bytes")

    class FakeProc:
        returncode = 1
        def __init__(self, *a, **kw):
            self.stdout = iter([])
            self.stderr = iter(["ERROR: video unavailable\n"])
        def poll(self):
            return 1
        def wait(self, timeout=None):
            return 1
        def kill(self):
            pass

    monkeypatch.setattr("runner.subprocess.Popen", FakeProc)

    res = run_download(
        url="https://example.com/v",
        out_template=out_template,
        format_choice="video",
        format_id=None,
        progress_cb=lambda *a, **k: None,
        register_process=lambda p: None,
        was_paused_check=lambda: False,
        network_policy=online_policy,
    )
    assert not part_file.exists()  # cleaned up
    assert res.error_category is not None
    assert online_policy.active_leases == 0


def test_download_timeout_defaults_to_an_hour(monkeypatch, tmp_path, online_policy):
    monkeypatch.delenv("TROVE_DOWNLOAD_TIMEOUT", raising=False)
    seen = {}
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        _blocking_popen_factory(returncode=1, stderr="boom", seen=seen),
    )
    runner.run_download(url="https://example.com/v", out_template=str(tmp_path / "x.%(ext)s"),
                        format_choice="video", format_id=None, network_policy=online_policy)
    assert seen["timeout"] == 3600


def test_download_timeout_env_override(monkeypatch, tmp_path, online_policy):
    monkeypatch.setenv("TROVE_DOWNLOAD_TIMEOUT", "7200")
    seen = {}
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        _blocking_popen_factory(returncode=1, stderr="boom", seen=seen),
    )
    runner.run_download(url="https://example.com/v", out_template=str(tmp_path / "x.%(ext)s"),
                        format_choice="video", format_id=None, network_policy=online_policy)
    assert seen["timeout"] == 7200


def test_download_timeout_garbage_env_falls_back(monkeypatch, tmp_path, online_policy):
    monkeypatch.setenv("TROVE_DOWNLOAD_TIMEOUT", "not-a-number")
    seen = {}
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        _blocking_popen_factory(returncode=1, stderr="boom", seen=seen),
    )
    runner.run_download(url="https://example.com/v", out_template=str(tmp_path / "x.%(ext)s"),
                        format_choice="video", format_id=None, network_policy=online_policy)
    assert seen["timeout"] == 3600
