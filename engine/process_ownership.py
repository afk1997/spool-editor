"""Process-wide ownership for subprocess trees started by the engine service.

Every production subprocess must be created through :class:`ServiceProcessRegistry`.
Creation and registration share one lock, so signal shutdown cannot observe the gap
between ``Popen`` returning and the service recording ownership.  POSIX children are
session leaders, which gives the engine a process group it can terminate and verify.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Callable


class ProcessRegistryClosed(RuntimeError):
    """A service subprocess launch raced or followed process shutdown."""


class ProcessDrainError(RuntimeError):
    """The service could not confirm that every owned process tree exited."""


class OwnedProcessTree:
    """Proxy one direct child while retaining its isolated POSIX process group."""

    def __init__(self, process, *, owns_group: bool):
        self._process = process
        self._pgid = process.pid if owns_group and hasattr(process, "pid") else None
        self._tree_exited = False
        self._tree_lock = threading.RLock()
        self._registry: ServiceProcessRegistry | None = None

    def __getattr__(self, name):
        return getattr(self._process, name)

    @property
    def pgid(self) -> int | None:
        return self._pgid

    @property
    def tree_exited(self) -> bool:
        with self._tree_lock:
            return self._tree_exited

    def _acquire_tree_lock(self, deadline: float | None = None) -> bool:
        if deadline is None:
            self._tree_lock.acquire()
            return True
        return self._tree_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))

    def _parent_exited_locked(self) -> bool:
        poll = getattr(self._process, "poll", None)
        if callable(poll):
            return poll() is not None
        return getattr(self._process, "returncode", None) is not None

    def _group_exited_locked(self) -> bool:
        if self._pgid is None:
            return True
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            self._pgid = None
            return True
        except AttributeError:
            self._pgid = None
            return True
        except OSError:
            # EPERM and other errors do not prove that the group disappeared.
            return False
        return False

    def confirm_tree_exited(self, *, deadline: float | None = None) -> bool:
        """Reap opportunistically and prove both parent and owned group are gone."""
        if not self._acquire_tree_lock(deadline):
            return False
        try:
            if self._tree_exited:
                return True
            parent_exited = self._parent_exited_locked()
            group_exited = self._group_exited_locked()
            self._tree_exited = parent_exited and group_exited
            return self._tree_exited
        finally:
            self._tree_lock.release()

    def _release_if_exited(self) -> None:
        registry = self._registry
        if registry is not None:
            registry.release_if_exited(self)

    def poll(self):
        result = self._process.poll()
        if result is not None:
            self._release_if_exited()
        return result

    def wait(self, timeout=None):
        result = self._process.wait(timeout=timeout)
        self._release_if_exited()
        return result

    def communicate(self, *args, **kwargs):
        result = self._process.communicate(*args, **kwargs)
        self._release_if_exited()
        return result

    def _signal_tree(self, sig: int, *, deadline: float | None = None) -> None:
        if not self._acquire_tree_lock(deadline):
            raise ProcessDrainError("process ownership lock exceeded the shutdown deadline")
        try:
            if self._tree_exited:
                return
            fallback = (
                self._process.kill
                if sig == getattr(signal, "SIGKILL", sig)
                else self._process.terminate
            )
            if self._pgid is not None:
                try:
                    os.killpg(self._pgid, sig)
                    return
                except ProcessLookupError:
                    # A test shim may accept start_new_session without creating a group.
                    self._pgid = None
                except (AttributeError, OSError):
                    pass
            try:
                fallback()
            except ProcessLookupError:
                # The nonblocking proof below still decides whether descendants exist.
                pass
        finally:
            self._tree_lock.release()

    def terminate(self, *, deadline: float | None = None) -> None:
        self._signal_tree(signal.SIGTERM, deadline=deadline)

    def kill(self, *, deadline: float | None = None) -> None:
        self._signal_tree(getattr(signal, "SIGKILL", signal.SIGTERM), deadline=deadline)

    def wait_for_group_exit(
        self,
        *,
        timeout: float = 0.25,
        forced_timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        """Confirm normal exit, then force any descendant left by the parent."""
        overall_deadline = deadline
        if overall_deadline is None:
            forced = max(timeout, 1.0) if forced_timeout is None else forced_timeout
            overall_deadline = time.monotonic() + max(0.0, timeout) + max(0.0, forced)
        graceful_deadline = min(
            overall_deadline, time.monotonic() + max(0.0, timeout)
        )
        while not self.confirm_tree_exited(deadline=graceful_deadline):
            remaining = graceful_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
        if self.confirm_tree_exited(deadline=overall_deadline):
            self._release_if_exited()
            return

        self.kill(deadline=overall_deadline)
        while not self.confirm_tree_exited(deadline=overall_deadline):
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDrainError("process group did not exit after SIGKILL")
            time.sleep(min(0.01, remaining))
        self._release_if_exited()


class ServiceProcessRegistry:
    """Own all subprocess groups launched by one engine service process."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active: set[OwnedProcessTree] = set()
        # This one-way latch is deliberately lock-free on the bounded signal path.
        self._closing = False
        self._last_shutdown_error: BaseException | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def last_shutdown_error(self) -> BaseException | None:
        return self._last_shutdown_error

    def close(self) -> None:
        """Reject fresh factories before waiting on any potentially contended lock."""
        self._closing = True

    def spawn(self, factory: Callable[[], OwnedProcessTree]) -> OwnedProcessTree:
        """Create and register a tree in the same critical section as shutdown."""
        if self._closing:
            raise ProcessRegistryClosed("service subprocess registry is shutting down")

        late_process = None
        with self._lock:
            if self._closing:
                raise ProcessRegistryClosed("service subprocess registry is shutting down")
            process = factory()
            if not isinstance(process, OwnedProcessTree):
                raise TypeError("service process factory must return OwnedProcessTree")
            process._registry = self
            self._active.add(process)
            if self._closing:
                # Shutdown latched while Popen was constructing. Retain ownership so
                # its drain snapshot sees the child, and reject it from the caller.
                late_process = process

        if late_process is not None:
            try:
                late_process.terminate()
            except (OSError, ProcessDrainError):
                pass
            raise ProcessRegistryClosed("service subprocess launch raced shutdown")
        return process

    def spawn_process(self, argv, *, popen=subprocess.Popen, **kwargs) -> OwnedProcessTree:
        """Spawn ``argv`` in an isolated session, tolerating lightweight test fakes."""

        def factory() -> OwnedProcessTree:
            owns_group = os.name == "posix"
            try:
                process = popen(argv, start_new_session=owns_group, **kwargs)
            except TypeError:
                process = popen(argv, **kwargs)
                owns_group = False
            return OwnedProcessTree(process, owns_group=owns_group)

        return self.spawn(factory)

    def run_process(
        self,
        argv,
        *,
        popen=subprocess.Popen,
        input=None,
        capture_output: bool = False,
        timeout: float | None = None,
        check: bool = False,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        """Owned equivalent of ``subprocess.run`` for service-reachable commands."""
        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin and input arguments may not both be used")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError(
                    "stdout and stderr arguments may not be used with capture_output"
                )
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE

        process = self.spawn_process(argv, popen=popen, **kwargs)
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as timeout_error:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                stdout = timeout_error.output
                stderr = timeout_error.stderr
            try:
                process.wait_for_group_exit(timeout=0.05, forced_timeout=2.0)
            except (OSError, ProcessDrainError, subprocess.SubprocessError):
                # Retained registry ownership lets signal shutdown retry the drain.
                pass
            timeout_error.stdout = stdout
            timeout_error.stderr = stderr
            raise
        except BaseException:
            try:
                process.kill()
                process.wait(timeout=2.0)
                process.wait_for_group_exit(timeout=0.05, forced_timeout=2.0)
            except (OSError, ProcessDrainError, subprocess.SubprocessError):
                pass
            raise

        process.wait_for_group_exit(timeout=0.05, forced_timeout=2.0)
        returncode = process.returncode
        if check and returncode:
            raise subprocess.CalledProcessError(
                returncode,
                argv,
                output=stdout,
                stderr=stderr,
            )
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def release_if_exited(
        self,
        process: OwnedProcessTree,
        *,
        deadline: float | None = None,
    ) -> bool:
        if not process.confirm_tree_exited(deadline=deadline):
            return False
        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            self._active.discard(process)
            process._registry = None
            return True
        finally:
            self._lock.release()

    def _snapshot(self, *, deadline: float) -> tuple[OwnedProcessTree, ...]:
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ProcessDrainError("process registry lock exceeded the shutdown deadline")
        try:
            return tuple(self._active)
        finally:
            self._lock.release()

    def _wait_until(
        self,
        *,
        deadline: float,
        known: tuple[OwnedProcessTree, ...],
        soft_deadline: bool = False,
    ) -> tuple[OwnedProcessTree, ...]:
        while True:
            if time.monotonic() >= deadline:
                return known
            try:
                active = self._snapshot(deadline=deadline)
            except ProcessDrainError:
                if soft_deadline:
                    return known
                raise
            known = active
            for process in active:
                self.release_if_exited(process, deadline=deadline)
            try:
                remaining = self._snapshot(deadline=deadline)
            except ProcessDrainError:
                if soft_deadline:
                    return known
                raise
            known = remaining
            if not remaining or time.monotonic() >= deadline:
                return remaining
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def shutdown(self, *, timeout: float = 5.0, term_grace: float = 0.25) -> None:
        self.shutdown_until(
            deadline=time.monotonic() + max(0.0, timeout),
            term_grace=term_grace,
        )

    def shutdown_until(self, *, deadline: float, term_grace: float = 0.25) -> None:
        """TERM all trees, then KILL/reap/confirm them under one deadline."""
        self.close()
        self._last_shutdown_error = None
        active = self._snapshot(deadline=deadline)

        # Broadcast each phase before waiting so one slow tree cannot starve peers.
        for process in active:
            if time.monotonic() >= deadline:
                break
            try:
                process.terminate(deadline=deadline)
            except (OSError, ProcessDrainError) as exc:
                self._last_shutdown_error = exc

        graceful_deadline = min(deadline, time.monotonic() + max(0.0, term_grace))
        remaining = self._wait_until(
            deadline=graceful_deadline,
            known=active,
            soft_deadline=True,
        )
        for process in remaining:
            if time.monotonic() >= deadline:
                break
            try:
                process.kill(deadline=deadline)
            except (OSError, ProcessDrainError) as exc:
                self._last_shutdown_error = exc

        remaining = self._wait_until(deadline=deadline, known=remaining)
        if remaining:
            error = ProcessDrainError(
                f"could not confirm exit for {len(remaining)} service process tree(s)"
            )
            if self._last_shutdown_error is None:
                self._last_shutdown_error = error
            raise error


service_processes = ServiceProcessRegistry()


def spawn_service_process(argv, *, popen=subprocess.Popen, **kwargs) -> OwnedProcessTree:
    """Spawn through the process-wide engine registry."""
    return service_processes.spawn_process(argv, popen=popen, **kwargs)


def run_service_process(argv, *, popen=subprocess.Popen, **kwargs):
    """Run through the process-wide engine registry."""
    return service_processes.run_process(argv, popen=popen, **kwargs)
