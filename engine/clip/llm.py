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
import stat
import subprocess
import tempfile
import time
from pathlib import Path
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

_CODEX_DISABLED_FEATURES = (
    "apply_patch_freeform",
    "apply_patch_streaming_events",
    "apps",
    "apps_mcp_path_override",
    "artifact",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "child_agents_md",
    "chronicle",
    "code_mode",
    "code_mode_only",
    "codex_git_commit",
    "collaboration_modes",
    "computer_use",
    "default_mode_request_user_input",
    "elevated_windows_sandbox",
    "enable_fanout",
    "enable_mcp_apps",
    "enable_request_compression",
    "exec_permission_approvals",
    "experimental_windows_sandbox",
    "external_migration",
    "fast_mode",
    "goals",
    "guardian_approval",
    "hooks",
    "image_detail_original",
    "image_generation",
    "imagegenext",
    "in_app_browser",
    "js_repl",
    "js_repl_tools_only",
    "memories",
    "mentions_v2",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "non_prefixed_mcp_tool_names",
    "personality",
    "plugin_hooks",
    "plugin_sharing",
    "plugins",
    "prevent_idle_sleep",
    "realtime_conversation",
    "remote_compaction_v2",
    "remote_control",
    "remote_models",
    "remote_plugin",
    "request_permissions_tool",
    "request_rule",
    "responses_websocket_response_processed",
    "responses_websockets",
    "responses_websockets_v2",
    "runtime_metrics",
    "search_tool",
    "shell_snapshot",
    "shell_tool",
    "shell_zsh_fork",
    "skill_env_var_dependency_prompt",
    "skill_mcp_dependency_install",
    "sqlite",
    "standalone_web_search",
    "steer",
    "terminal_resize_reflow",
    "tool_call_mcp_elicitation",
    "tool_search",
    "tool_search_always_defer_mcp_tools",
    "tool_suggest",
    "tui_app_server",
    "unavailable_dummy_tools",
    "undo",
    "unified_exec",
    "use_legacy_landlock",
    "use_linux_sandbox_bwrap",
    "web_search_cached",
    "web_search_request",
    "workspace_dependencies",
    "workspace_owner_usage_nudge",
)
_CODEX_REVIEWED_VERSION = "codex-cli 0.136.0"
_CODEX_AUTH_FILES = ("auth.json",)
_CODEX_REMOVED_ENABLED_FEATURES = {
    ("tui_app_server", "removed", "true"),
}
_CODEX_INFERENCE_CONFIG = (
    'web_search="disabled"',
    "mcp_servers={}",
    'default_permissions="spool-inference"',
    "permissions.spool-inference.filesystem={}",
    "permissions.spool-inference.network.enabled=false",
    "skills.bundled.enabled=false",
    "skills.include_instructions=false",
    "skills.config=[]",
    "tools.experimental_request_user_input.enabled=false",
    "analytics.enabled=false",
    "feedback.enabled=false",
    "check_for_update_on_startup=false",
    "otel.log_user_prompt=false",
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
)

_TRUE = {"1", "true", "yes", "on"}
_codex_auth_lock = RLock()


def _codex_auth_file() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    source_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    for name in _CODEX_AUTH_FILES:
        candidate = source_home / name
        if candidate.is_file():
            return candidate.resolve()
    raise ProviderUnavailableError(
        f"Codex auth credential file auth.json was not found in {source_home}. "
        "Run `codex login` before enabling remote reasoning."
    )


def _snapshot_codex_auth(source: Path, *, runtime_home: str) -> tuple[bytes, int]:
    """Copy only auth.json into isolated CODEX_HOME and return its CAS snapshot."""
    try:
        snapshot = source.read_bytes()
        source_mode = stat.S_IMODE(source.stat().st_mode)
        destination = Path(runtime_home) / "auth.json"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.chmod(destination, 0o600)
    except OSError as exc:
        raise ProviderUnavailableError(
            "could not create the isolated Codex authentication snapshot"
        ) from exc
    return snapshot, source_mode


def _reconcile_codex_auth(
    source: Path,
    *,
    runtime_home: str,
    snapshot: bytes,
    source_mode: int,
) -> None:
    """Atomically persist only a successful CLI auth.json refresh."""
    isolated = Path(runtime_home) / "auth.json"
    try:
        refreshed = isolated.read_bytes()
        if refreshed == snapshot:
            return
        if source.read_bytes() != snapshot:
            raise ProviderUnavailableError(
                "Codex auth.json changed concurrently; its isolated refresh was not "
                "persisted"
            )

        descriptor, temporary = tempfile.mkstemp(
            prefix=".auth.json.spool-",
            dir=str(source.parent),
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(refreshed)
                handle.flush()
                os.fchmod(handle.fileno(), source_mode)
                os.fsync(handle.fileno())
            os.replace(temporary, source)
            temporary = ""
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(source.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    except ProviderUnavailableError:
        raise
    except OSError as exc:
        raise ProviderUnavailableError(
            "could not persist the refreshed Codex authentication credential"
        ) from exc



def _sanitized_codex_env(*, runtime_home: str, discovered_bin: str) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(
            (os.path.dirname(os.path.abspath(discovered_bin)), os.defpath)
        ),
        "HOME": runtime_home,
        "CODEX_HOME": runtime_home,
        "TMPDIR": runtime_home,
        "TMP": runtime_home,
        "TEMP": runtime_home,
        "TERM": "dumb",
        "NO_COLOR": "1",
    }


def _codex_inference_options(*, strict_config: bool) -> tuple[str, ...]:
    options: list[str] = []
    if strict_config:
        options.append("--strict-config")
    for setting in _CODEX_INFERENCE_CONFIG:
        options.extend(("-c", setting))
    for feature in _CODEX_DISABLED_FEATURES:
        options.extend(("--disable", feature))
    return tuple(options)


def _validate_codex_cli(binary: str, *, env: Mapping[str, str]) -> None:
    def run_probe(*args: str) -> str:
        returncode, stdout, _stderr = _run_codex_probe(
            binary,
            args=args,
            env=env,
        )
        if returncode != 0:
            raise ProviderUnavailableError(
                "could not validate the installed Codex CLI"
            )
        return stdout

    version = run_probe("--version").strip()
    if version != _CODEX_REVIEWED_VERSION:
        raise ProviderUnavailableError(
            "remote reasoning requires the reviewed Codex CLI version "
            f"{_CODEX_REVIEWED_VERSION!r}; found {version or 'unknown'!r}"
        )

    # `codex features` rejects --strict-config in 0.136.0. It does accept the
    # complete config/feature override envelope, so exercise that exact denylist
    # and verify its effective states before the separately strict exec launch.
    feature_output = run_probe(
        *_codex_inference_options(strict_config=False),
        "features",
        "list",
    )
    feature_rows = []
    for line in feature_output.splitlines():
        if not line.strip():
            continue
        columns = line.split()
        if len(columns) < 3:
            raise ProviderUnavailableError(
                "installed Codex CLI returned an unrecognized feature listing"
            )
        feature_rows.append(
            (columns[0], " ".join(columns[1:-1]), columns[-1])
        )
    feature_names = [row[0] for row in feature_rows]
    unsafe_states = {
        row
        for row in feature_rows
        if row[2] != "false" and row not in _CODEX_REMOVED_ENABLED_FEATURES
    }
    if (
        len(feature_names) != len(set(feature_names))
        or set(feature_names) != set(_CODEX_DISABLED_FEATURES)
        or unsafe_states
        or not _CODEX_REMOVED_ENABLED_FEATURES.issubset(set(feature_rows))
    ):
        raise ProviderUnavailableError(
            "installed Codex CLI feature state differs from the reviewed "
            "inference-only boundary"
        )


def _run_codex_probe(
    binary: str,
    *,
    args: tuple[str, ...],
    env: Mapping[str, str],
) -> tuple[int, str, str]:
    """Run a no-prompt capability probe in an owned, bounded process group."""
    try:
        process = _spawn_reasoning_process(
            [binary, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env),
            cwd=env["HOME"],
        )
        stdout, stderr = _communicate_reasoning_process(
            process,
            input_text="",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise ProviderUnavailableError(
            "could not validate the installed Codex CLI"
        ) from exc
    return process.returncode, stdout, stderr


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

    Runs ``codex exec`` as an inference-only process: a reviewed CLI version and
    complete feature denylist are enforced, model filesystem/network permissions
    are empty, HOME/TMP are isolated, and the child environment is sanitized. The
    CODEX_HOME contains only a private auth.json snapshot; successful credential
    refreshes are reconciled atomically while user config, AGENTS instructions,
    skills, plugins, rules, and executable tools stay absent. Prompts go over stdin
    and the final response is read from an output file.
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
        # Retained as a backwards-compatible keyword only. Running anywhere a
        # caller supplies could expose project metadata to trusted Codex core
        # before model permissions apply, so every invocation ignores it.
        del cwd
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
        discovered_bin = shutil.which(self.bin)
        if discovered_bin is None:
            raise ProviderUnavailableError(
                f"the Codex CLI ({self.bin!r}) was not found on PATH. Install it "
                "(`npm i -g @openai/codex` / `brew install codex`) and sign in with your "
                "ChatGPT/Codex account (`codex login`), set SPOOL_CODEX_BIN to its path, "
                "or choose another LLM provider via SPOOL_LLM_PROVIDER."
            )
        resolved_bin = os.path.realpath(discovered_bin)
        full = prompt if not system else f"{system}\n\n{prompt}"
        # HOME, CODEX_HOME, TMP, -C, and the subprocess cwd all point at one fresh
        # directory. Only auth.json is copied in under a serialized refresh lock;
        # strict config, the zero-filesystem permission profile, and a pinned feature
        # set keep project/global instructions, plugins, tools, and model-spawned
        # processes out of scope.
        probe_home: str | None = None
        runtime_home: str | None = None
        working_dir: str | None = None
        out_path: str | None = None
        try:
            probe_home = tempfile.mkdtemp(prefix="spool-codex-probe-")
            os.chmod(probe_home, 0o700)
            probe_env = _sanitized_codex_env(
                runtime_home=probe_home,
                discovered_bin=discovered_bin,
            )
            _validate_codex_cli(resolved_bin, env=probe_env)
            try:
                shutil.rmtree(probe_home)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning(
                    "could not remove Codex capability-probe directory %s: %s",
                    probe_home,
                    exc,
                )
            else:
                probe_home = None

            runtime_home = tempfile.mkdtemp(prefix="spool-codex-")
            os.chmod(runtime_home, 0o700)
            working_dir = runtime_home
            with _codex_auth_lock:
                auth_source = _codex_auth_file()
                auth_snapshot, auth_mode = _snapshot_codex_auth(
                    auth_source,
                    runtime_home=runtime_home,
                )
                codex_env = _sanitized_codex_env(
                    runtime_home=runtime_home,
                    discovered_bin=discovered_bin,
                )
                out_fd, out_path = tempfile.mkstemp(
                    prefix="spool-codex-out-",
                    suffix=".txt",
                )
                os.close(out_fd)
                argv = [
                    resolved_bin,
                    "exec",
                    *_codex_inference_options(strict_config=True),
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--color",
                    "never",
                    "-C",
                    working_dir,
                    "-o",
                    out_path,
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
                                cwd=working_dir,
                                env=codex_env,
                            ),
                            lease=lease,
                        )
                except FileNotFoundError as e:  # race: vanished after discovery
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
                        f"codex exec failed (rc={proc.returncode}): "
                        f"{stderr.strip()[-500:]}"
                    )
                _reconcile_codex_auth(
                    auth_source,
                    runtime_home=runtime_home,
                    snapshot=auth_snapshot,
                    source_mode=auth_mode,
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
            if runtime_home is not None:
                try:
                    shutil.rmtree(runtime_home)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _log.warning(
                        "could not remove Codex scratch directory %s: %s",
                        runtime_home,
                        exc,
                    )
            if probe_home is not None:
                try:
                    shutil.rmtree(probe_home)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _log.warning(
                        "could not remove Codex capability-probe directory %s: %s",
                        probe_home,
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
