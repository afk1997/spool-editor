from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from process_ownership import (
    OwnedProcessTree,
    ProcessDrainError,
    ProcessRegistryClosed,
    ServiceProcessRegistry,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX process-group ownership"
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _spawn_tree(
    registry: ServiceProcessRegistry,
    tmp_path: Path,
    *,
    ignore_term: bool,
) -> tuple[OwnedProcessTree, int]:
    pid_path = tmp_path / f"tree-{time.monotonic_ns()}.pids"
    child_source = (
        "import signal,time;"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_term else "")
        + "time.sleep(60)"
    )
    parent_source = "\n".join(
        (
            "import pathlib,signal,subprocess,sys,time",
            "ignore = sys.argv[2] == '1'",
            "if ignore: signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}])",
            # Give the child time to install SIG_IGN before publishing readiness.
            "time.sleep(0.15)",
            "pathlib.Path(sys.argv[1]).write_text(f'{child.pid}\\n')",
            "time.sleep(60)",
        )
    )
    process = registry.spawn_process(
        [sys.executable, "-c", parent_source, str(pid_path), "1" if ignore_term else "0"],
        popen=subprocess.Popen,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _wait_until(pid_path.exists), "process-tree helper did not publish its child"
    return process, int(pid_path.read_text().strip())


def _force_cleanup(process: OwnedProcessTree | None) -> None:
    if process is None:
        return
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        pass


def test_completed_process_releases_its_registered_group():
    registry = ServiceProcessRegistry()
    process = registry.spawn_process(
        [sys.executable, "-c", "print('done')"],
        popen=subprocess.Popen,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout, stderr = process.communicate(timeout=2)

    assert (stdout, stderr, process.returncode) == ("done\n", "", 0)
    assert registry.active_count == 0


@pytest.mark.parametrize("ignore_term", [False, True])
def test_shutdown_terminates_reaps_and_confirms_owned_process_groups(
    tmp_path, ignore_term,
):
    registry = ServiceProcessRegistry()
    process = None
    try:
        process, child_pid = _spawn_tree(
            registry, tmp_path, ignore_term=ignore_term
        )
        parent_pid = process.pid

        started = time.monotonic()
        registry.shutdown(timeout=1.2, term_grace=0.2)
        elapsed = time.monotonic() - started

        assert elapsed < 1.5
        if ignore_term:
            assert elapsed >= 0.18
        assert process.poll() is not None
        assert _wait_until(lambda: not _pid_exists(parent_pid))
        assert _wait_until(lambda: not _pid_exists(child_pid))
        assert registry.active_count == 0
    finally:
        _force_cleanup(process)


def test_shutdown_cannot_miss_a_process_created_while_closing(tmp_path):
    registry = ServiceProcessRegistry()
    factory_entered = threading.Event()
    release_factory = threading.Event()
    process_holder: list[OwnedProcessTree] = []
    spawn_result: list[BaseException] = []
    shutdown_result: list[BaseException] = []

    def factory() -> OwnedProcessTree:
        raw = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        owned = OwnedProcessTree(raw, owns_group=True)
        process_holder.append(owned)
        factory_entered.set()
        assert release_factory.wait(2), "test did not release the process factory"
        return owned

    def spawn() -> None:
        try:
            registry.spawn(factory)
        except BaseException as exc:
            spawn_result.append(exc)

    def shutdown() -> None:
        try:
            registry.shutdown(timeout=1.2, term_grace=0.1)
        except BaseException as exc:
            shutdown_result.append(exc)

    spawn_thread = threading.Thread(target=spawn)
    shutdown_thread = threading.Thread(target=shutdown)
    spawn_thread.start()
    assert factory_entered.wait(2), "spawn never entered its factory"
    shutdown_thread.start()
    try:
        assert _wait_until(lambda: registry.closing)
        release_factory.set()
        spawn_thread.join(2)
        shutdown_thread.join(2)

        assert not spawn_thread.is_alive() and not shutdown_thread.is_alive()
        assert len(spawn_result) == 1
        assert isinstance(spawn_result[0], ProcessRegistryClosed)
        assert shutdown_result == []
        assert process_holder[0].poll() is not None
        assert registry.active_count == 0
    finally:
        release_factory.set()
        if process_holder:
            _force_cleanup(process_holder[0])
        spawn_thread.join(2)
        shutdown_thread.join(2)


def test_closed_registry_rejects_before_invoking_process_factory():
    registry = ServiceProcessRegistry()
    registry.shutdown(timeout=0.2)
    calls = []

    with pytest.raises(ProcessRegistryClosed):
        registry.spawn(lambda: calls.append("spawn") or object())

    assert calls == []


def test_term_grace_lock_contention_does_not_skip_kill_escalation():
    registry = ServiceProcessRegistry()
    term_called = threading.Event()
    registry_lock_held = threading.Event()

    class TermIgnoringProcess:
        pid = 999_999_999
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            term_called.set()
            assert registry_lock_held.wait(1), "registry contention was not installed"

        def kill(self):
            self.returncode = -signal.SIGKILL

    process = registry.spawn(
        lambda: OwnedProcessTree(TermIgnoringProcess(), owns_group=False)
    )

    def briefly_contend_registry() -> None:
        assert term_called.wait(1), "shutdown never sent TERM"
        registry._lock.acquire()
        try:
            registry_lock_held.set()
            time.sleep(0.15)
        finally:
            registry._lock.release()

    contender = threading.Thread(target=briefly_contend_registry)
    contender.start()
    try:
        registry.shutdown(timeout=0.8, term_grace=0.03)
    finally:
        contender.join(1)

    assert process.returncode == -signal.SIGKILL
    assert registry.active_count == 0


def test_shutdown_registry_lock_acquisition_obeys_its_deadline():
    registry = ServiceProcessRegistry()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def contend_registry() -> None:
        registry._lock.acquire()
        try:
            lock_held.set()
            assert release_lock.wait(1), "test did not release registry lock"
        finally:
            registry._lock.release()

    contender = threading.Thread(target=contend_registry)
    contender.start()
    assert lock_held.wait(1), "registry lock was never contended"
    started = time.monotonic()
    try:
        with pytest.raises(ProcessDrainError, match="registry lock.*deadline"):
            registry.shutdown(timeout=0.05)
    finally:
        release_lock.set()
        contender.join(1)

    assert time.monotonic() - started < 0.25
    assert registry.closing is True
