from __future__ import annotations

import ast
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


_ENGINE_ROOT = Path(__file__).resolve().parents[1]


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


def test_run_process_matches_capture_and_check_contracts():
    registry = ServiceProcessRegistry()

    completed = registry.run_process(
        [sys.executable, "-c", "print('owned')"],
        popen=subprocess.Popen,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert completed.args[-1] == "print('owned')"
    assert (completed.returncode, completed.stdout, completed.stderr) == (
        0,
        "owned\n",
        "",
    )
    assert registry.active_count == 0

    with pytest.raises(subprocess.CalledProcessError) as failed:
        registry.run_process(
            [sys.executable, "-c", "raise SystemExit(7)"],
            popen=subprocess.Popen,
            capture_output=True,
            check=True,
        )
    assert failed.value.returncode == 7
    assert registry.active_count == 0


def test_run_process_timeout_force_kills_and_reaps_owned_group(tmp_path):
    registry = ServiceProcessRegistry()
    pid_path = tmp_path / "run-timeout-child.pid"
    child_source = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent_source = "\n".join(
        (
            "import pathlib,signal,subprocess,sys,time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}])",
            "time.sleep(0.1)",
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))",
            "time.sleep(60)",
        )
    )

    with pytest.raises(subprocess.TimeoutExpired):
        registry.run_process(
            [sys.executable, "-c", parent_source, str(pid_path)],
            popen=subprocess.Popen,
            capture_output=True,
            timeout=0.3,
        )

    assert pid_path.exists()
    child_pid = int(pid_path.read_text())
    assert _wait_until(lambda: not _pid_exists(child_pid))
    assert registry.active_count == 0


def test_run_process_creation_is_linearized_against_close():
    registry = ServiceProcessRegistry()
    popen_entered = threading.Event()
    release_popen = threading.Event()
    process_holder = []
    run_result: list[BaseException] = []
    shutdown_result: list[BaseException] = []

    def blocking_popen(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        process_holder.append(process)
        popen_entered.set()
        assert release_popen.wait(2), "test did not release Popen"
        return process

    def run() -> None:
        try:
            registry.run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                popen=blocking_popen,
                timeout=30,
            )
        except BaseException as exc:
            run_result.append(exc)

    def shutdown() -> None:
        try:
            registry.shutdown(timeout=1.0, term_grace=0.05)
        except BaseException as exc:
            shutdown_result.append(exc)

    run_thread = threading.Thread(target=run)
    shutdown_thread = threading.Thread(target=shutdown)
    run_thread.start()
    assert popen_entered.wait(2), "owned run never entered Popen"
    shutdown_thread.start()
    try:
        assert _wait_until(lambda: registry.closing)
        release_popen.set()
        run_thread.join(2)
        shutdown_thread.join(2)

        assert not run_thread.is_alive() and not shutdown_thread.is_alive()
        assert len(run_result) == 1
        assert isinstance(run_result[0], ProcessRegistryClosed)
        assert shutdown_result == []
        assert process_holder[0].poll() is not None
        assert registry.active_count == 0
    finally:
        release_popen.set()
        for process in process_holder:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
        run_thread.join(2)
        shutdown_thread.join(2)


def test_shutdown_broadcasts_term_before_force_kill():
    registry = ServiceProcessRegistry()
    events = []

    class TermIgnoringProcess:
        pid = 999_999_998
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("TERM")

        def kill(self):
            events.append("KILL")
            self.returncode = -signal.SIGKILL

    registry.spawn(
        lambda: OwnedProcessTree(TermIgnoringProcess(), owns_group=False)
    )

    registry.shutdown(timeout=0.5, term_grace=0.02)

    assert events == ["TERM", "KILL"]


def test_many_processes_share_one_tiny_shutdown_deadline():
    registry = ServiceProcessRegistry()
    signals_sent = []

    class SlowSignalProcess:
        returncode = None

        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return self.returncode

        def terminate(self):
            signals_sent.append((self.pid, "TERM"))
            time.sleep(0.02)

        def kill(self):
            signals_sent.append((self.pid, "KILL"))
            time.sleep(0.02)

    for index in range(20):
        registry.spawn(
            lambda index=index: OwnedProcessTree(
                SlowSignalProcess(900_000_000 + index), owns_group=False
            )
        )

    started = time.monotonic()
    with pytest.raises(ProcessDrainError):
        registry.shutdown(timeout=0.035, term_grace=0.01)
    elapsed = time.monotonic() - started

    assert elapsed < 0.12
    assert len(signals_sent) < 10


def test_shutdown_forces_term_ignoring_child_after_parent_was_reaped(tmp_path):
    registry = ServiceProcessRegistry()
    child_pid_path = tmp_path / "reaped-parent-child.pid"
    child_source = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent_source = "\n".join(
        (
            "import pathlib,subprocess,sys,time",
            f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}])",
            "time.sleep(0.1)",
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))",
        )
    )
    process = registry.spawn_process(
        [sys.executable, "-c", parent_source, str(child_pid_path)],
        popen=subprocess.Popen,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    try:
        assert process.wait(timeout=2) == 0
        child_pid = int(child_pid_path.read_text())
        assert registry.active_count == 1

        registry.shutdown(timeout=0.8, term_grace=0.1)

        assert _wait_until(lambda: not _pid_exists(child_pid))
        assert registry.active_count == 0
    finally:
        if child_pid is not None and _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
            _wait_until(lambda: not _pid_exists(child_pid))


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


def test_production_service_has_no_unowned_raw_subprocess_calls():
    raw_calls = []

    spawning_calls = {
        "subprocess": {
            "Popen",
            "run",
            "call",
            "check_call",
            "check_output",
            "getoutput",
            "getstatusoutput",
        },
        "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    }

    class RawSubprocessVisitor(ast.NodeVisitor):
        def __init__(self, relative_path: str, tree: ast.AST):
            self.relative_path = relative_path
            self.function_names: list[str] = []
            self.module_aliases = {"subprocess": "subprocess", "asyncio": "asyncio", "os": "os"}
            self.function_aliases = {}
            for candidate in ast.walk(tree):
                if isinstance(candidate, ast.Import):
                    for alias in candidate.names:
                        if alias.name in {"subprocess", "asyncio", "os"}:
                            self.module_aliases[alias.asname or alias.name] = alias.name
                elif (
                    isinstance(candidate, ast.ImportFrom)
                    and candidate.module in {"subprocess", "asyncio", "os"}
                ):
                    for alias in candidate.names:
                        self.function_aliases[alias.asname or alias.name] = (
                            candidate.module,
                            alias.name,
                        )

        def visit_FunctionDef(self, node):
            self.function_names.append(node.name)
            self.generic_visit(node)
            self.function_names.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            function = node.func
            module_name = None
            function_name = None
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                module_name = self.module_aliases.get(function.value.id)
                function_name = function.attr
            elif isinstance(function, ast.Name) and function.id in self.function_aliases:
                module_name, function_name = self.function_aliases[function.id]

            is_spawn = (
                module_name in spawning_calls
                and function_name in spawning_calls[module_name]
            ) or (
                module_name == "os"
                and (
                    function_name in {"system", "popen"}
                    or str(function_name).startswith("spawn")
                )
            )
            if is_spawn:
                raw_calls.append(
                    (
                        self.relative_path,
                        self.function_names[-1] if self.function_names else "<module>",
                        f"{module_name}.{function_name}",
                    )
                )
            self.generic_visit(node)

    for source_path in sorted(_ENGINE_ROOT.rglob("*.py")):
        relative = source_path.relative_to(_ENGINE_ROOT)
        if relative.parts[0] in {"tests", "scripts"}:
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(relative)
        )
        RawSubprocessVisitor(str(relative), tree).visit(tree)

    # The CLI-backed Codex provider is unreachable through Phase-0 routes while
    # the security slice removes it. Keep this exception exact so any other raw
    # launch fails immediately; deleting the dormant provider must delete it too.
    assert raw_calls == [
        ("clip/llm.py", "_spawn_reasoning_process", "subprocess.Popen"),
        ("clip/llm.py", "_spawn_reasoning_process", "subprocess.Popen"),
    ]
    routes_source = (_ENGINE_ROOT / "routes" / "api_v1.py").read_text(
        encoding="utf-8"
    )
    assert '_REASONING_PROVIDERS = ("none",)' in routes_source
    assert "return _remote_reasoning_unavailable()" in routes_source
