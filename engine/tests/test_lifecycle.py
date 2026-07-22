"""Process lifecycle tests for engine-owned workers and Codex subprocesses."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest


def test_run_flask_app_drains_engine_on_normal_return():
    import lifecycle

    calls = []

    class FakeApp:
        extensions = {
            "trove.shutdown": lambda *, wait: calls.append(("shutdown", wait)),
        }

        def run(self, **options):
            calls.append(("run", options))
            return "stopped"

    result = lifecycle.run_flask_app(FakeApp(), host="127.0.0.1", port=8899)

    assert result == "stopped"
    assert calls == [
        ("run", {"host": "127.0.0.1", "port": 8899}),
        ("shutdown", True),
    ]


def test_run_flask_app_installs_shutdown_before_building_app(monkeypatch):
    import lifecycle
    from contextlib import contextmanager

    events = []

    class FakeApp:
        extensions = {
            "trove.shutdown": lambda *, wait: events.append(("shutdown", wait)),
        }

        def run(self, **_options):
            events.append("run")
            return "stopped"

    def build_app():
        events.append("build")
        return FakeApp()

    @contextmanager
    def observed_bridge(_shutdown):
        events.append("install")
        try:
            yield
        finally:
            events.append("restore")

    monkeypatch.setattr(lifecycle, "sigterm_shutdown", observed_bridge)

    result = lifecycle.run_flask_app(app_factory=build_app)

    assert result == "stopped"
    assert events == [
        "install",
        "build",
        "run",
        ("shutdown", True),
        "restore",
    ]


def test_signal_closes_and_drains_global_owner_before_app_construction(monkeypatch):
    import lifecycle
    from contextlib import contextmanager

    events = []
    drained = threading.Event()
    bridge_thread = None
    callback_errors = []
    deadline = time.monotonic() + 1.0

    class FakeRegistry:
        def close(self):
            events.append("close")

        def shutdown_until(self, *, deadline):
            events.append(("drain", deadline))
            drained.set()

    class FakeApp:
        def __init__(self):
            self.extensions = {
                "trove.shutdown": lambda *, wait: events.append(("normal", wait)),
            }

        def run(self, **_options):
            events.append("run")
            return "stopped"

    def build_app():
        assert drained.wait(1), "signal did not drain before app construction"
        events.append("build")
        return FakeApp()

    @contextmanager
    def triggered_bridge(shutdown):
        nonlocal bridge_thread

        def trigger():
            try:
                shutdown(deadline)
            except BaseException as exc:
                callback_errors.append(exc)

        bridge_thread = threading.Thread(target=trigger)
        bridge_thread.start()
        try:
            yield
        finally:
            bridge_thread.join(1)

    monkeypatch.setattr(lifecycle, "service_processes", FakeRegistry())
    monkeypatch.setattr(lifecycle, "sigterm_shutdown", triggered_bridge)

    assert lifecycle.run_flask_app(app_factory=build_app) == "stopped"

    assert callback_errors == []
    assert not bridge_thread.is_alive()
    assert events == [
        "close",
        ("drain", deadline),
        "build",
        "run",
        ("normal", True),
    ]


def test_signal_does_not_wait_for_stuck_app_construction(monkeypatch):
    import lifecycle
    from contextlib import contextmanager

    callback_done = threading.Event()
    callback_errors = []
    elapsed = []

    class FakeRegistry:
        def close(self):
            pass

        def shutdown_until(self, *, deadline):
            pass

    class FakeApp:
        extensions = {"trove.shutdown": lambda *, wait: None}

        def run(self, **_options):
            return "stopped"

    def build_app():
        assert callback_done.wait(1), "signal callback waited for app construction"
        return FakeApp()

    @contextmanager
    def triggered_bridge(shutdown):
        def trigger():
            started = time.monotonic()
            try:
                shutdown(started + 1.0)
            except BaseException as exc:
                callback_errors.append(exc)
            finally:
                elapsed.append(time.monotonic() - started)
                callback_done.set()

        thread = threading.Thread(target=trigger)
        thread.start()
        try:
            yield
        finally:
            thread.join(1)

    monkeypatch.setattr(lifecycle, "service_processes", FakeRegistry())
    monkeypatch.setattr(lifecycle, "sigterm_shutdown", triggered_bridge)

    assert lifecycle.run_flask_app(app_factory=build_app) == "stopped"

    assert callback_errors == []
    assert elapsed[0] < 0.1


def test_sigterm_relay_is_installed_before_worker_start(monkeypatch):
    import lifecycle

    events = []

    class FakeThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            events.append("worker-start")

        def join(self, timeout=None):
            pass

    def record_signal(_signum, handler):
        events.append("install" if callable(handler) else "restore")

    monkeypatch.setattr(lifecycle.threading, "Thread", FakeThread)
    monkeypatch.setattr(lifecycle.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(lifecycle.signal, "signal", record_signal)

    with lifecycle.sigterm_shutdown(lambda _deadline: None):
        pass

    assert events[:2] == ["install", "worker-start"]


def test_sigterm_worker_start_failure_restores_handler_and_closes_pipe(monkeypatch):
    import lifecycle

    signal_handlers = []

    class FailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start shutdown worker")

    def record_signal(_signum, handler):
        signal_handlers.append(handler)

    monkeypatch.setattr(lifecycle.threading, "Thread", FailedThread)
    monkeypatch.setattr(lifecycle.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(lifecycle.signal, "signal", record_signal)
    bridge = lifecycle.sigterm_shutdown(lambda _deadline: None)

    with pytest.raises(RuntimeError, match="cannot start shutdown worker"):
        bridge.__enter__()

    assert callable(signal_handlers[0])
    assert signal_handlers[-1] == "previous"
    assert bridge._read_fd is None
    assert bridge._write_fd is None


def test_failed_sigterm_drain_waits_for_a_fresh_signal_before_retrying(monkeypatch):
    import lifecycle

    first_attempt = threading.Event()
    second_attempt = threading.Event()
    exited = threading.Event()
    exit_codes = []
    deadlines = []

    def shutdown(deadline):
        deadlines.append(deadline)
        if len(deadlines) == 1:
            first_attempt.set()
            raise RuntimeError("tree still live")
        second_attempt.set()

    def record_exit(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(lifecycle.os, "_exit", record_exit)

    with lifecycle.sigterm_shutdown(shutdown) as bridge:
        os.write(bridge._write_fd, lifecycle._TERMINATE)
        assert first_attempt.wait(1)
        time.sleep(0.05)

        assert len(deadlines) == 1
        assert exit_codes == []
        assert bridge._worker.is_alive()
        assert isinstance(bridge._last_shutdown_error, RuntimeError)

        os.write(bridge._write_fd, lifecycle._TERMINATE)
        assert second_attempt.wait(1)
        assert exited.wait(1)

    assert len(deadlines) == 2
    assert deadlines[1] > deadlines[0]
    assert exit_codes == [128 + signal.SIGTERM]
    assert bridge._last_shutdown_error is None


def test_successful_sigterm_drain_gets_one_absolute_deadline_then_exits(monkeypatch):
    import lifecycle

    drained = threading.Event()
    exited = threading.Event()
    deadlines = []
    exit_codes = []

    def shutdown(deadline):
        deadlines.append(deadline)
        drained.set()

    def record_exit(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(lifecycle.os, "_exit", record_exit)
    started = time.monotonic()

    with lifecycle.sigterm_shutdown(shutdown) as bridge:
        os.write(bridge._write_fd, lifecycle._TERMINATE)
        assert drained.wait(1)
        assert exited.wait(1)

    assert exit_codes == [128 + signal.SIGTERM]
    assert len(deadlines) == 1
    assert started < deadlines[0] <= started + lifecycle.SIGTERM_SHUTDOWN_TIMEOUT + 0.1


def test_normal_server_exit_surfaces_failed_drain_without_retry(monkeypatch):
    import lifecycle
    from contextlib import contextmanager

    attempts = []
    events = []

    class FakeApp:
        def shutdown(self, *, wait):
            attempts.append(wait)
            raise RuntimeError("tree still live")

        def __init__(self):
            self.extensions = {"trove.shutdown": self.shutdown}

        def run(self, **_options):
            return "stopped"

    @contextmanager
    def observed_bridge(_shutdown):
        events.append("install")
        try:
            yield
        finally:
            events.append("restore")

    monkeypatch.setattr(lifecycle, "sigterm_shutdown", observed_bridge)
    with pytest.raises(RuntimeError, match="tree still live"):
        lifecycle.run_flask_app(FakeApp())

    assert attempts == [True]
    assert events == ["install", "restore"]


def test_app_exposes_one_shutdown_boundary_for_all_owned_workers(tmp_path, monkeypatch):
    import app as app_module

    service_events = []

    class FakeReasoningRegistry:
        closing = False

        def shutdown(self, *, timeout):
            self.closing = True

    reasoning = FakeReasoningRegistry()

    class FakeServiceRegistry:
        def close(self):
            service_events.append("close")

        def shutdown_until(self, *, deadline):
            service_events.append(("drain", deadline))

    monkeypatch.setattr(app_module, "service_processes", FakeServiceRegistry())
    monkeypatch.setattr(
        app_module.clip_llm,
        "reasoning_process_registry",
        lambda _network_policy: reasoning,
        raising=False,
    )
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    application = app_module.create_app()

    registry = application.extensions["trove.reasoning_processes"]
    assert registry is reasoning

    started = time.monotonic()
    application.extensions["trove.shutdown"](wait=True)

    assert service_events[0] == "close"
    assert service_events[1][0] == "drain"
    assert (
        started
        < service_events[1][1]
        <= started + app_module.NORMAL_SHUTDOWN_TIMEOUT + 0.1
    )
    assert registry.closing is True
    assert application.extensions["trove.jobs"]._accepting is False
    assert application.extensions["trove.transcribe"]._accepting is False
    assert application.extensions["trove.clips"]._accepting is False


def test_app_signal_drain_bypasses_contended_normal_shutdown_lock(tmp_path, monkeypatch):
    import app as app_module

    close_calls = []
    drains = []

    class FakeServiceRegistry:
        def close(self):
            close_calls.append(threading.current_thread().name)

        def shutdown_until(self, *, deadline):
            drains.append(deadline)

    class FakeReasoningRegistry:
        def shutdown(self, *, timeout):
            pass

    monkeypatch.setattr(app_module, "service_processes", FakeServiceRegistry())
    monkeypatch.setattr(
        app_module.clip_llm,
        "reasoning_process_registry",
        lambda _network_policy: FakeReasoningRegistry(),
        raising=False,
    )
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    application = app_module.create_app()
    normal_entered = threading.Event()
    release_normal = threading.Event()
    reasoning = application.extensions["trove.reasoning_processes"]

    def block_normal_shutdown(*, timeout):
        normal_entered.set()
        assert release_normal.wait(2), "test did not release normal shutdown"

    monkeypatch.setattr(reasoning, "shutdown", block_normal_shutdown)
    normal_result = []
    normal_thread = threading.Thread(
        target=lambda: normal_result.append(
            application.extensions["trove.shutdown"](wait=True)
        )
    )
    normal_thread.start()
    assert normal_entered.wait(1), "normal shutdown never acquired its lock"
    deadline = time.monotonic() + 0.2
    started = time.monotonic()
    try:
        application.extensions["trove.signal_shutdown"](deadline=deadline)
    finally:
        release_normal.set()
        normal_thread.join(2)

    assert time.monotonic() - started < 0.1
    assert len(close_calls) == 2
    assert len(drains) == 2
    assert drains[-1] == deadline
    assert normal_result == [None]


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM/process-group lifecycle")
def test_sigterm_drains_download_transcribe_and_clip_groups_before_exit(tmp_path):
    pid_file = tmp_path / "service-groups.pids"
    term_file = tmp_path / "service-groups.term"
    ready_file = tmp_path / "supervisor.ready"
    engine_dir = Path(__file__).parents[1]
    supervisor_source = r'''
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import lifecycle
from clip_jobs import ClipJobManager
from jobs import JobManager
from process_ownership import service_processes, spawn_service_process
from transcribe_jobs import TranscribeJobManager

pid_file = Path(os.environ["SPOOL_TEST_PID_FILE"])
term_file = os.environ["SPOOL_TEST_TERM_FILE"]
ready_file = Path(os.environ["SPOOL_TEST_READY_FILE"])

child_source = r"""
import os, signal, sys, time
def on_term(_signum, _frame):
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, f"{sys.argv[2]} child {os.getpid()}\n".encode())
    finally: os.close(fd)
signal.signal(signal.SIGTERM, on_term)
time.sleep(60)
"""

worker_source = r"""
import os, signal, subprocess, sys, time
label, pid_path, term_path, child_source_arg = sys.argv[1:]
def on_term(_signum, _frame):
    fd = os.open(term_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, f"{label} parent {os.getpid()}\n".encode())
    finally: os.close(fd)
signal.signal(signal.SIGTERM, on_term)
child = subprocess.Popen(
    [sys.executable, "-c", child_source_arg, term_path, label],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(0.15)
fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try: os.write(fd, f"{label} {os.getpid()} {child.pid}\n".encode())
finally: os.close(fd)
time.sleep(60)
"""

downloads = JobManager(max_workers=1)
transcribes = TranscribeJobManager(max_workers=1)
clips = ClipJobManager(max_workers=1)

def launch(label):
    return spawn_service_process(
        [sys.executable, "-c", worker_source, label, str(pid_file), term_file, child_source],
        popen=subprocess.Popen,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def download_target(job, *, attempt):
    process = launch("download")
    assert downloads.register_process(job.id, job, attempt, process)
    process.wait()

def transcribe_target(job, *, model_path, attempt):
    process = launch("transcribe")
    assert transcribes.register_process(job.id, job, attempt, process)
    process.wait()

def clip_target(job, *, attempt):
    process = launch("clip")
    assert clips.register_process(job.id, job, attempt, process)
    process.wait()

class App:
    extensions = {"trove.shutdown": lambda *, wait: None}

    def run(self, **_options):
        downloads.submit(target=download_target, title="d", url="file://d")
        transcribes.submit(parent_job_id="d", model_path="m", target=transcribe_target)
        clips.submit(kind="cut", target=clip_target)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pid_file.exists() and len(pid_file.read_text().splitlines()) == 3:
                ready_file.write_text("ready")
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("service process groups did not start")
        while True:
            time.sleep(1)

lifecycle.run_flask_app(App())
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(engine_dir)
    env["SPOOL_TEST_PID_FILE"] = str(pid_file)
    env["SPOOL_TEST_TERM_FILE"] = str(term_file)
    env["SPOOL_TEST_READY_FILE"] = str(ready_file)
    supervisor = subprocess.Popen([sys.executable, "-c", supervisor_source], env=env)
    owned_pids = []
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not ready_file.exists():
            if supervisor.poll() is not None:
                pytest.fail(f"supervisor exited before workers: {supervisor.returncode}")
            time.sleep(0.01)
        assert ready_file.exists(), "supervisor never launched all service groups"
        rows = [line.split() for line in pid_file.read_text().splitlines()]
        assert {row[0] for row in rows} == {"download", "transcribe", "clip"}
        owned_pids = [int(pid) for row in rows for pid in row[1:]]

        started = time.monotonic()
        os.kill(supervisor.pid, signal.SIGTERM)
        assert supervisor.wait(timeout=4) == 128 + signal.SIGTERM
        assert time.monotonic() - started < 3.0

        term_rows = term_file.read_text().splitlines()
        assert {int(row.split()[-1]) for row in term_rows} == set(owned_pids)
        for pid in owned_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        for pid in owned_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
