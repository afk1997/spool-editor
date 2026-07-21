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

    with lifecycle.sigterm_shutdown(lambda: None):
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
    bridge = lifecycle.sigterm_shutdown(lambda: None)

    with pytest.raises(RuntimeError, match="cannot start shutdown worker"):
        bridge.__enter__()

    assert callable(signal_handlers[0])
    assert signal_handlers[-1] == "previous"
    assert bridge._read_fd is None
    assert bridge._write_fd is None


def test_failed_sigterm_drain_does_not_exit_and_can_be_retried(monkeypatch):
    import lifecycle

    first_attempt = threading.Event()
    second_attempt = threading.Event()
    exit_codes = []
    attempts = 0

    def shutdown():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt.set()
            raise RuntimeError("tree still live")
        second_attempt.set()

    monkeypatch.setattr(lifecycle.os, "_exit", exit_codes.append)

    with lifecycle.sigterm_shutdown(shutdown) as bridge:
        os.write(bridge._write_fd, lifecycle._TERMINATE)
        assert first_attempt.wait(1)
        time.sleep(0.01)
        assert exit_codes == []

        os.write(bridge._write_fd, lifecycle._TERMINATE)
        assert second_attempt.wait(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not exit_codes:
            time.sleep(0.001)
        assert exit_codes == [128 + signal.SIGTERM]


def test_normal_server_exit_retries_failed_drain_before_restoring_handler(monkeypatch):
    import lifecycle
    from contextlib import contextmanager

    attempts = []
    events = []

    class FakeApp:
        def shutdown(self, *, wait):
            attempts.append(wait)
            if len(attempts) == 1:
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
    monkeypatch.setattr(
        lifecycle.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert lifecycle.run_flask_app(FakeApp()) == "stopped"
    assert attempts == [True, True]
    assert events == ["install", "restore"]


def test_app_exposes_one_shutdown_boundary_for_all_owned_workers(tmp_path, monkeypatch):
    import app as app_module
    from clip import llm

    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.delenv("SPOOL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPOOL_LLM_EGRESS_CONSENT", raising=False)
    monkeypatch.delenv("SPOOL_OFFLINE", raising=False)
    application = app_module.create_app()

    registry = application.extensions["trove.reasoning_processes"]
    assert registry is llm.reasoning_process_registry(
        application.extensions["trove.network_policy"]
    )

    application.extensions["trove.shutdown"](wait=True)

    assert registry.closing is True
    assert application.extensions["trove.jobs"]._accepting is False
    assert application.extensions["trove.transcribe"]._accepting is False
    assert application.extensions["trove.clips"]._accepting is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM/process-group lifecycle")
def test_sigterm_bridge_drains_live_reasoning_process_before_exit(tmp_path):
    bridge_pid_file = tmp_path / "bridge.pid"
    bridge = tmp_path / "codex-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "with open(os.environ['SPOOL_TEST_BRIDGE_PID'], 'w') as f:\n"
        "    f.write(str(os.getpid())); f.flush(); os.fsync(f.fileno())\n"
        "sys.stdin.read()\n"
        "time.sleep(60)\n"
    )
    bridge.chmod(0o755)

    engine_dir = Path(__file__).parents[1]
    supervisor_source = """
import os
import signal
import time

import lifecycle
from clip import llm
from network_policy import NetworkPolicy

policy = NetworkPolicy()
registry = llm.reasoning_process_registry(policy)
provider = llm.CodexProvider(
    network_policy=policy,
    privacy_state=lambda: {
        "offline": False,
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    },
    process_registry=registry,
    bin=os.environ["SPOOL_TEST_BRIDGE"],
    timeout=60,
)
with lifecycle.sigterm_shutdown(lambda: registry.shutdown(timeout=3)):
    provider.complete("transcript")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(engine_dir)
    env["SPOOL_TEST_BRIDGE"] = str(bridge)
    env["SPOOL_TEST_BRIDGE_PID"] = str(bridge_pid_file)
    supervisor = subprocess.Popen([sys.executable, "-c", supervisor_source], env=env)
    bridge_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not bridge_pid_file.exists():
            if supervisor.poll() is not None:
                pytest.fail(f"supervisor exited before bridge launch: {supervisor.returncode}")
            time.sleep(0.01)
        assert bridge_pid_file.exists(), "supervisor never launched Codex bridge"
        bridge_pid = int(bridge_pid_file.read_text())

        os.kill(supervisor.pid, signal.SIGTERM)
        assert supervisor.wait(timeout=8) == 128 + signal.SIGTERM

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(bridge_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("SIGTERM shutdown returned while Codex bridge was still alive")
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if bridge_pid is not None:
            try:
                os.killpg(bridge_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
