"""Pluggable LLM provider layer for the clip engine.

Moment-finding (and, later, title/description generation) needs a language model.
Spool is local-first, so remote reasoning is disabled by default. Users can explicitly
select the **codex bridge**, which shells out to their own Codex CLI, authed with their
ChatGPT/Codex subscription — no API key, no local GPU. Other providers slot in behind
the same tiny ``complete(prompt, *, system)`` interface:

- ``none``   (default) — no reasoning provider; transcript text stays local.
- ``codex``  (opt-in) — bridge to the Codex CLI. Network egress: only the prompt text.
- ``agent``  — the driving MCP agent's own LLM, injected as a :class:`CallableProvider`
               by the MCP layer (the engine itself performs no egress).
- future ``claude`` (hosted key) / ``local`` (Ollama/llama.cpp) — offline-safe.

This **supersedes** the spec's local-Ollama default (Product Overview §10 #2): the
default is the codex bridge instead.

Privacy / offline. Only transcript *text* is ever sent — media never leaves the
machine. The offline switch (``SPOOL_OFFLINE=1``) hard-disables every egress provider;
a local provider would still run offline. Egress providers declare ``egress = True`` so
:func:`complete` can refuse them when offline.
"""
from __future__ import annotations

import logging
import os
import signal
import shutil
import subprocess
import tempfile
import time
from threading import RLock
from typing import Callable, Mapping, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from network_policy import NetworkPolicy, NetworkPolicyError

_log = logging.getLogger(__name__)

# Configurable knobs (env). New Spool functionality → ``SPOOL_*`` namespace.
DEFAULT_PROVIDER = "none"
CODEX_BIN = os.environ.get("SPOOL_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("SPOOL_CODEX_MODEL") or None  # None → the CLI's own default
CODEX_TIMEOUT = int(os.environ.get("SPOOL_CODEX_TIMEOUT", "180"))
# Moment-finding is extraction, not deep reasoning — default codex to "low" effort so the
# bridge is fast + cheap (xhigh burns ~10x the tokens for the same JSON). Set
# SPOOL_CODEX_REASONING="" to fall back to the CLI's configured default.
CODEX_REASONING = os.environ.get("SPOOL_CODEX_REASONING", "low")

_TRUE = {"1", "true", "yes", "on"}


class OfflineError(RuntimeError):
    """Raised when an egress provider is requested while offline-mode is on."""

    error_category = "offline_network_disabled"


class EgressConsentError(OfflineError):
    """Raised when a remote provider is selected without explicit transcript-egress consent."""

    error_category = "egress_consent_required"


class ProviderUnavailableError(RuntimeError):
    """Raised when a provider can't run (unknown name, or the Codex CLI is missing)."""


class ReasoningDisabledError(ProviderUnavailableError):
    """Raised when no reasoning provider has been selected."""

    error_category = "reasoning_provider_required"


class ReasoningDrainError(RuntimeError):
    """Raised when shutdown cannot prove every owned reasoning tree exited."""


def is_offline(env: dict | None = None) -> bool:
    """True when the engine-wide offline switch (``SPOOL_OFFLINE``) is set."""
    e = env if env is not None else os.environ
    return (e.get("SPOOL_OFFLINE") or "").strip().lower() in _TRUE


PrivacyState = Callable[[], Mapping[str, object]]


def _env_privacy_state(env: Mapping[str, object]) -> dict[str, object]:
    """Translate the live applied environment into the persisted settings shape."""
    return {
        "offline": str(env.get("SPOOL_OFFLINE") or "").strip().lower() in _TRUE,
        "reasoning_provider": str(
            env.get("SPOOL_LLM_PROVIDER") or DEFAULT_PROVIDER
        ).strip().lower(),
        "reasoning_egress_consent": str(
            env.get("SPOOL_LLM_EGRESS_CONSENT") or ""
        ).strip().lower() in _TRUE,
    }


def _privacy_getter(
    privacy_state: PrivacyState | None,
    env: Mapping[str, object] | None,
) -> PrivacyState:
    if privacy_state is not None:
        def current() -> Mapping[str, object]:
            values = privacy_state()
            if any(
                key in values
                for key in ("offline", "reasoning_provider", "reasoning_egress_consent")
            ):
                return values
            return _env_privacy_state(values)

        return current
    live_env = os.environ if env is None else env
    return lambda: _env_privacy_state(live_env)


def _privacy_authority(
    privacy_state: PrivacyState | None,
    env: Mapping[str, object] | None,
) -> tuple[str, object]:
    """Identify the one source actually consulted by :func:`_privacy_getter`."""
    if privacy_state is not None:
        return "state", privacy_state
    return "env", os.environ if env is None else env


def _require_remote_reasoning(state: Mapping[str, object], *, provider_name: str) -> None:
    """Validate the current applied privacy state, with Offline always strongest."""
    if state.get("offline") is True:
        raise OfflineError(
            f"LLM provider {provider_name!r} needs network egress, but offline mode is on."
        )
    if str(state.get("reasoning_provider") or DEFAULT_PROVIDER).lower() != "codex":
        raise ReasoningDisabledError(
            "Remote reasoning is disabled. Select the Codex provider before using "
            "reasoning features."
        )
    if state.get("reasoning_egress_consent") is not True:
        raise EgressConsentError(
            f"LLM provider {provider_name!r} requires explicit consent before transcript "
            "text can leave this machine."
        )


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    egress: bool

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class _OwnedReasoningProcess:
    """Own a Codex parent and every descendant in its POSIX process group."""

    def __init__(self, process, *, owns_group: bool):
        self._process = process
        self._pgid = process.pid if owns_group and hasattr(process, "pid") else None
        self._tree_exited = False
        self._tree_lock = RLock()

    def __getattr__(self, name):
        return getattr(self._process, name)

    def _acquire_tree_lock(self, *, deadline: float | None = None) -> None:
        if deadline is None:
            self._tree_lock.acquire()
            return
        if not self._tree_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            raise RuntimeError(
                "Codex process ownership lock remained busy at the shutdown deadline"
            )

    def kill(self, *, deadline: float | None = None) -> None:
        self._acquire_tree_lock(deadline=deadline)
        try:
            if self._tree_exited:
                return
            if self._pgid is not None:
                try:
                    os.killpg(self._pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    return
                except ProcessLookupError:
                    # A shim may accept ``start_new_session`` but ignore it. A missing
                    # process group therefore does not prove the direct parent exited.
                    self._pgid = None
                except (AttributeError, OSError):
                    pass
            try:
                self._process.kill()
            except ProcessLookupError:
                self._tree_exited = True
        finally:
            self._tree_lock.release()

    @property
    def tree_exited(self) -> bool:
        with self._tree_lock:
            return self._tree_exited

    def tree_exited_until(self, *, deadline: float) -> bool:
        """Read exit proof without waiting beyond a shutdown owner's deadline."""
        if not self._tree_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            return False
        try:
            return self._tree_exited
        finally:
            self._tree_lock.release()

    def wait_for_group_exit(
        self,
        *,
        timeout: float = 0.25,
        forced_timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        """Keep the lease until the group is gone; kill a detached lingering child."""
        self._acquire_tree_lock(deadline=deadline)
        try:
            if self._pgid is None:
                self._tree_exited = True
                return

            def group_is_gone() -> bool:
                try:
                    os.killpg(self._pgid, 0)
                except ProcessLookupError:
                    self._pgid = None
                    self._tree_exited = True
                    return True
                except AttributeError:
                    self._pgid = None
                    self._tree_exited = True
                    return True
                except OSError:
                    # EPERM and other probe errors do not prove the group is gone.
                    return False
                return False

            def wait_phase(seconds: float) -> bool:
                phase_deadline = time.monotonic() + max(0.0, seconds)
                if deadline is not None:
                    phase_deadline = min(phase_deadline, deadline)
                while not group_is_gone():
                    remaining = phase_deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(0.01, remaining))
                return True

            try:
                if wait_phase(max(0.0, timeout)):
                    return
                try:
                    os.killpg(self._pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except ProcessLookupError:
                    self._pgid = None
                    self._tree_exited = True
                    return
                except (AttributeError, OSError):
                    try:
                        self._process.kill()
                    except (ProcessLookupError, OSError):
                        pass
                forced_budget = (
                    2.0 if forced_timeout is None else max(0.0, forced_timeout)
                )
                if wait_phase(forced_budget):
                    return
                raise RuntimeError(
                    "Codex process group did not exit after SIGKILL within "
                    f"{forced_budget:g}s"
                )
            finally:
                if self._pgid is None:
                    self._tree_exited = True
        finally:
            self._tree_lock.release()

    def terminate_and_wait(self, *, timeout: float) -> None:
        """Stop a live tree during engine shutdown and reap its direct parent."""
        deadline = time.monotonic() + max(0.0, timeout)
        self.terminate_and_wait_until(deadline=deadline)

    def terminate_and_wait_until(self, *, deadline: float) -> None:
        """Stop and reap this tree without extending an owner's absolute deadline."""
        self.kill(deadline=deadline)
        try:
            self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            try:
                self.kill(deadline=deadline)
            except (OSError, RuntimeError):
                pass
            raise RuntimeError(
                "Codex parent process did not exit within the shutdown timeout"
            ) from exc
        # ``shutdown(timeout=...)`` owns one deadline across parent reaping and
        # descendant confirmation. Never introduce a new per-tree grace period.
        self.wait_for_group_exit(
            timeout=max(0.0, deadline - time.monotonic()),
            forced_timeout=0.0,
            deadline=deadline,
        )


class _RetainedEgressLease:
    """Idempotent owner for exactly one process-retained lease reference."""

    def __init__(self, lease):
        self._lease = lease
        self._lock = RLock()
        self._released = False

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return True
            self._lease.release()
            self._released = True
            return True

    def release_until(self, *, deadline: float) -> bool:
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            return False
        try:
            if self._released:
                return True
            release_until = getattr(self._lease, "release_until", None)
            if not callable(release_until):
                return False
            if not release_until(deadline=deadline):
                return False
            self._released = True
            return True
        finally:
            self._lock.release()


class ReasoningProcessRegistry:
    """Own every Codex process tree admitted for one engine policy."""

    def __init__(self):
        self._lock = RLock()
        self._active: set[_OwnedReasoningProcess] = set()
        self._leases: dict[_OwnedReasoningProcess, object] = {}
        self._fallback: list[tuple[_OwnedReasoningProcess, object | None]] = []
        # Spool's engine runs on CPython, where a reference assignment/read is
        # atomic under the GIL. Keep this one-way latch lock-free: Event.set()
        # itself takes an unbounded condition lock and belongs outside shutdown.
        self._closing = False
        self._last_shutdown_error: BaseException | None = None

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active) + len(self._fallback)

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def last_shutdown_error(self) -> BaseException | None:
        return self._last_shutdown_error

    def spawn(
        self,
        factory: Callable[[], _OwnedReasoningProcess],
        *,
        lease=None,
    ) -> _OwnedReasoningProcess:
        """Linearize process creation/registration against shutdown."""
        if self._closing:
            raise ProviderUnavailableError("remote reasoning is shutting down")
        retained = lease is not None
        retained_lease = None
        ownership_transferred = False
        if lease is not None:
            # Callers normally already own the policy lock through launch_admission.
            # Retain before taking the registry lock to preserve policy -> registry
            # ordering even for direct registry users.
            lease.retain_for_process()
            retained_lease = _RetainedEgressLease(lease)
        try:
            with self._lock:
                if self._closing:
                    raise ProviderUnavailableError("remote reasoning is shutting down")
                process = None
                registered = False
                try:
                    # Keep creation + registration under one registry boundary so
                    # shutdown can never miss a process created concurrently.
                    process = factory()
                    if self._closing:
                        raise ProviderUnavailableError(
                            "remote reasoning is shutting down"
                        )
                    self._active.add(process)
                    registered = True
                    if retained_lease is not None:
                        self._leases[process] = retained_lease
                    ownership_transferred = retained
                    return process
                except BaseException:
                    if registered:
                        self._active.discard(process)
                        self._leases.pop(process, None)
                    if process is not None:
                        try:
                            process.terminate_and_wait(timeout=2.0)
                        except BaseException as cleanup_error:
                            # Keep the retained policy reference: an unconfirmed live
                            # process must continue blocking Offline even if registration
                            # itself failed. The identity-based fallback also keeps it
                            # available to every later shutdown retry.
                            self._fallback.append((process, retained_lease))
                            ownership_transferred = retained
                            raise ReasoningDrainError(
                                "could not drain Codex process after registration failed"
                            ) from cleanup_error
                        if not process.tree_exited:
                            self._fallback.append((process, retained_lease))
                            ownership_transferred = retained
                            raise ReasoningDrainError(
                                "Codex process exit remained unconfirmed after registration failed"
                            )
                    raise
        except BaseException:
            # Never acquire the policy lock while the registry lock is held.
            # Closing/factory/registration failures return the speculative retain;
            # fallback ownership keeps it until a later confirmed drain.
            if retained and not ownership_transferred:
                retained_lease.release()
            raise

    def release(
        self,
        process: _OwnedReasoningProcess,
        *,
        deadline: float | None = None,
    ) -> bool:
        def acquire_registry() -> bool:
            if deadline is None:
                self._lock.acquire()
                return True
            return self._lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
            )

        if not acquire_registry():
            return False
        try:
            tracked = False
            lease = None
            try:
                tracked = process in self._active or process in self._leases
                lease = self._leases.get(process)
            except TypeError:
                pass
            for candidate, fallback_lease in self._fallback:
                if candidate is process:
                    tracked = True
                    lease = fallback_lease
                    break
            if not tracked:
                return True
            if lease is None:
                try:
                    self._active.discard(process)
                    self._leases.pop(process, None)
                except TypeError:
                    pass
                self._fallback = [
                    entry for entry in self._fallback if entry[0] is not process
                ]
                return True
        finally:
            self._lock.release()

        # The retained owner is idempotent. If policy or registry contention
        # exhausts this attempt's deadline, the process entry remains available
        # for a later retry without decrementing the same reference twice.
        if deadline is None:
            lease.release()
        elif not lease.release_until(deadline=deadline):
            return False

        if not acquire_registry():
            return False
        try:
            try:
                if self._leases.get(process) is lease:
                    self._active.discard(process)
                    self._leases.pop(process, None)
            except TypeError:
                pass
            self._fallback = [
                entry
                for entry in self._fallback
                if not (entry[0] is process and entry[1] is lease)
            ]
            return True
        finally:
            self._lock.release()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Reject new launches, kill all registered trees, and reap their parents."""
        deadline = time.monotonic() + max(0.0, timeout)
        # Latch intent before any contended ownership lock. Even an attempt that
        # exhausts its budget must reject every fresh launch until a later retry.
        self._closing = True
        self._last_shutdown_error = None
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            error = ReasoningDrainError(
                "could not acquire the reasoning registry before the shutdown deadline"
            )
            self._last_shutdown_error = error
            raise error
        try:
            active = tuple(self._active) + tuple(
                process for process, _lease in self._fallback
            )
        finally:
            self._lock.release()
        for process in active:
            try:
                if isinstance(process, _OwnedReasoningProcess):
                    process.kill(deadline=deadline)
                else:
                    process.kill()
            except (OSError, RuntimeError) as exc:
                # Never invoke logging handlers on the bounded shutdown path.
                self._last_shutdown_error = exc
        for process in active:
            try:
                if isinstance(process, _OwnedReasoningProcess):
                    process.terminate_and_wait_until(deadline=deadline)
                else:
                    process.terminate_and_wait(
                        timeout=max(0.0, deadline - time.monotonic())
                    )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self._last_shutdown_error = exc
            else:
                # A failed wait must not make a live tree disappear from ownership.
                # A later shutdown call can retry every entry that remains tracked.
                if isinstance(process, _OwnedReasoningProcess):
                    tree_exited = process.tree_exited_until(deadline=deadline)
                else:
                    tree_exited = process.tree_exited
                if tree_exited:
                    self.release(process, deadline=deadline)
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            error = ReasoningDrainError(
                "could not inspect the reasoning registry before the shutdown deadline"
            )
            self._last_shutdown_error = error
            raise error
        try:
            remaining = len(self._active) + len(self._fallback)
        finally:
            self._lock.release()
        if remaining:
            error = ReasoningDrainError(
                f"could not confirm exit for {remaining} Codex process tree(s)"
            )
            if self._last_shutdown_error is None:
                self._last_shutdown_error = error
            raise error


_reasoning_registries_lock = RLock()
_reasoning_registries: WeakKeyDictionary[NetworkPolicy, ReasoningProcessRegistry] = (
    WeakKeyDictionary()
)


def reasoning_process_registry(policy: NetworkPolicy) -> ReasoningProcessRegistry:
    """Return the one process registry shared by every provider on ``policy``."""
    with _reasoning_registries_lock:
        registry = _reasoning_registries.get(policy)
        if registry is None:
            registry = ReasoningProcessRegistry()
            _reasoning_registries[policy] = registry
        return registry


def _spawn_reasoning_process(argv: list[str], **kwargs) -> _OwnedReasoningProcess:
    """Start Codex in a new POSIX session, with a fake/non-POSIX fallback."""
    owns_group = os.name == "posix"
    try:
        process = subprocess.Popen(argv, start_new_session=owns_group, **kwargs)
    except TypeError:
        process = subprocess.Popen(argv, **kwargs)
        owns_group = False
    return _OwnedReasoningProcess(process, owns_group=owns_group)


def _communicate_reasoning_process(
    process: _OwnedReasoningProcess,
    *,
    input_text: str,
    timeout: float,
) -> tuple[str, str]:
    """Communicate, reap the parent, and confirm tree exit before returning."""
    def bounded_abort() -> None:
        deadline = time.monotonic() + 2.0
        process.kill()
        try:
            process.communicate(
                timeout=max(0.0, deadline - time.monotonic())
            )
        except BaseException:
            process.terminate_and_wait(
                timeout=max(0.0, deadline - time.monotonic())
            )
        else:
            process.wait_for_group_exit(
                timeout=max(0.0, deadline - time.monotonic()),
                forced_timeout=0.0,
            )

    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        bounded_abort()
        raise
    except BaseException:
        bounded_abort()
        raise
    process.wait_for_group_exit()
    return stdout or "", stderr or ""


class CodexProvider:
    """Bridge to the user's Codex CLI (ChatGPT/Codex subscription).

    Runs ``codex exec`` non-interactively in a **read-only sandbox** (so the agent
    can never touch the filesystem) and feeds the prompt over stdin — transcripts can
    be large. The final message on stdout is returned verbatim; the caller parses it.
    """

    name = "codex"
    egress = True

    def __init__(self, *, network_policy: NetworkPolicy,
                 privacy_state: PrivacyState | None = None,
                 env: Mapping[str, object] | None = None,
                 process_registry: ReasoningProcessRegistry | None = None,
                 bin: str | None = None, model: str | None = None,
                 timeout: int | None = None, cwd: str | None = None,
                 reasoning: str | None = None):
        self.network_policy = network_policy
        self._privacy_authority_kind, self._privacy_authority_source = (
            _privacy_authority(privacy_state, env)
        )
        self.privacy_state = _privacy_getter(privacy_state, env)
        shared_registry = reasoning_process_registry(network_policy)
        if process_registry is not None and process_registry is not shared_registry:
            raise ValueError(
                "CodexProvider requires the shared reasoning process registry for its "
                "network policy"
            )
        self.process_registry = shared_registry
        self.bin = bin or CODEX_BIN
        self.model = model if model is not None else CODEX_MODEL
        self.timeout = timeout if timeout is not None else CODEX_TIMEOUT
        self.cwd = cwd
        self.reasoning = CODEX_REASONING if reasoning is None else reasoning

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        _require_remote_reasoning(self.privacy_state(), provider_name=self.name)
        try:
            with self.network_policy.egress("codex_reasoning") as lease:
                # Re-read after lease admission: a queued caller must not execute using
                # the provider/consent snapshot that was true when it was submitted.
                _require_remote_reasoning(self.privacy_state(), provider_name=self.name)
                return self._complete_leased(prompt, system=system, lease=lease)
        except NetworkPolicyError as exc:
            raise OfflineError(
                "LLM provider 'codex' needs network egress, but offline mode is on."
            ) from exc

    def _complete_leased(
        self, prompt: str, *, system: str | None = None, lease
    ) -> str:
        if shutil.which(self.bin) is None:
            raise ProviderUnavailableError(
                f"the Codex CLI ({self.bin!r}) was not found on PATH. Install it "
                "(`npm i -g @openai/codex` / `brew install codex`) and sign in with your "
                "ChatGPT/Codex account (`codex login`), set SPOOL_CODEX_BIN to its path, "
                "or choose another LLM provider via SPOOL_LLM_PROVIDER."
            )
        full = prompt if not system else f"{system}\n\n{prompt}"
        # Run in an empty scratch dir so the agent has nothing to read; --ephemeral keeps
        # no session files; read-only sandbox blocks any FS write; -o captures *just* the
        # final message (vs the noisy event log on stdout). Prompt goes over stdin (`-`).
        scratch = self.cwd or tempfile.mkdtemp(prefix="spool-codex-")
        out_path: str | None = None
        try:
            out_fd, out_path = tempfile.mkstemp(prefix="spool-codex-out-", suffix=".txt")
            os.close(out_fd)
            argv = [
                self.bin, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--ephemeral", "--ignore-user-config", "--color", "never",
                "-C", scratch, "-o", out_path,
            ]
            if self.model:
                argv += ["-m", self.model]
            if self.reasoning:
                argv += ["-c", f"model_reasoning_effort={self.reasoning}"]
            argv += ["-"]

            try:
                # The final live read and process creation linearize against every
                # settings patch on the shared policy lock. Communication remains
                # outside this short guard while the egress lease stays active.
                with lease.launch_admission():
                    _require_remote_reasoning(
                        self.privacy_state(), provider_name=self.name
                    )
                    proc = self.process_registry.spawn(
                        lambda: _spawn_reasoning_process(
                            argv,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            cwd=scratch,
                        ),
                        lease=lease,
                    )
            except FileNotFoundError as e:  # race: vanished between which() and spawn
                raise ProviderUnavailableError(
                    f"the Codex CLI ({self.bin!r}) could not be run: {e}"
                ) from e
            try:
                stdout, stderr = _communicate_reasoning_process(
                    proc, input_text=full, timeout=self.timeout
                )
            finally:
                if proc.tree_exited:
                    self.process_registry.release(proc)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed (rc={proc.returncode}): {stderr.strip()[-500:]}"
                )
            try:
                with open(out_path) as f:
                    answer = f.read()
            except OSError:
                answer = ""
            return answer or stdout
        finally:
            if out_path is not None:
                self._cleanup(out_path)
            if not self.cwd:
                try:
                    shutil.rmtree(scratch)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _log.warning(
                        "could not remove Codex scratch directory %s: %s",
                        scratch,
                        exc,
                    )

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.warning("could not remove Codex output temp %s: %s", path, exc)


class NoneProvider:
    """Explicitly disabled reasoning provider used by the privacy-safe default."""

    name = "none"
    egress = False

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        raise ReasoningDisabledError(
            "Remote reasoning is disabled. Select the Codex provider and explicitly "
            "consent to transcript-text egress before using reasoning features."
        )


class CallableProvider:
    """Wraps an arbitrary ``fn(prompt, *, system) -> str``.

    Used for the ``agent`` provider (the MCP layer injects the driving agent's own
    sampling) and for tests. Defaults to ``egress=False`` — the engine performs no
    network I/O itself, so it is allowed in offline-mode.
    """

    def __init__(self, fn: Callable[..., str], *, name: str = "agent", egress: bool = False):
        self._fn = fn
        self.name = name
        self.egress = egress

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return self._fn(prompt, system=system)


def get_provider(
    provider: "str | LLMProvider | None" = None,
    *,
    env: Mapping[str, object] | None = None,
    network_policy: NetworkPolicy | None = None,
    privacy_state: PrivacyState | None = None,
) -> LLMProvider:
    """Resolve a provider.

    ``provider`` may be an :class:`LLMProvider` instance (returned as-is — this is how
    the MCP layer injects the agent's own LLM), or a name string. ``None`` uses the
    configured default (``SPOOL_LLM_PROVIDER`` env, else ``none``).
    """
    if provider is not None and not isinstance(provider, str):
        return provider
    e = env if env is not None else os.environ
    name = (provider or e.get("SPOOL_LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    if name == "none":
        return NoneProvider()
    if name == "codex":
        if network_policy is None:
            raise ValueError("network_policy is required for the Codex provider")
        return CodexProvider(
            network_policy=network_policy,
            privacy_state=privacy_state,
            env=e,
        )
    raise ProviderUnavailableError(
        f"unknown LLM provider {name!r}. Built-in providers: 'none', 'codex'. The "
        "'agent' provider is injected by the MCP layer — pass it as an instance, not a name."
    )


def complete(
    prompt: str,
    *,
    system: str | None = None,
    provider: "str | LLMProvider | None" = None,
    env: Mapping[str, object] | None = None,
    network_policy: NetworkPolicy | None = None,
    privacy_state: PrivacyState | None = None,
) -> str:
    """Run one completion, leasing every engine-owned egress provider explicitly."""
    p = get_provider(
        provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )
    if not getattr(p, "egress", False):
        return p.complete(prompt, system=system)

    # Codex owns the direct boundary itself so even callers that invoke the provider
    # instance directly cannot bypass the live checks or the shared lease.
    if isinstance(p, CodexProvider):
        if network_policy is not None and network_policy is not p.network_policy:
            if network_policy.offline:
                raise OfflineError(
                    "LLM provider 'codex' needs network egress, but offline mode is on."
                )
            raise ProviderUnavailableError(
                "the supplied Codex provider is not bound to the shared network policy"
            )
        if privacy_state is not None or env is not None:
            authority_kind, authority_source = _privacy_authority(privacy_state, env)
            if (
                authority_kind != p._privacy_authority_kind
                or authority_source is not p._privacy_authority_source
            ):
                _require_remote_reasoning(
                    _privacy_getter(privacy_state, env)(), provider_name=p.name
                )
                raise ProviderUnavailableError(
                    "the supplied Codex provider is not bound to the shared privacy state"
                )
        return p.complete(prompt, system=system)

    if network_policy is None:
        raise ValueError("network_policy is required for an egress provider")
    if network_policy.offline:
        raise OfflineError(
            f"LLM provider {p.name!r} needs network egress, but offline mode is on."
        )
    state = _privacy_getter(privacy_state, env)
    _require_remote_reasoning(state(), provider_name=p.name)
    raise ProviderUnavailableError(
        f"egress provider {p.name!r} is disabled until it exposes an atomic launch boundary"
    )
