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
from typing import Callable, Mapping, Protocol, runtime_checkable

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

    def __getattr__(self, name):
        return getattr(self._process, name)

    def kill(self) -> None:
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

    def wait_for_group_exit(self, *, timeout: float = 0.25) -> None:
        """Keep the lease until the group is gone; kill a detached lingering child."""
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
            deadline = time.monotonic() + seconds
            while not group_is_gone():
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
            return True

        if wait_phase(timeout):
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
        forced_timeout = max(2.0, timeout)
        if wait_phase(forced_timeout):
            return
        raise RuntimeError(
            f"Codex process group did not exit after SIGKILL within {forced_timeout:g}s"
        )


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
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate()
        finally:
            process.wait_for_group_exit()
        raise
    except BaseException:
        try:
            if process.poll() is None:
                process.kill()
            process.communicate()
        finally:
            process.wait_for_group_exit()
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
                 bin: str | None = None, model: str | None = None,
                 timeout: int | None = None, cwd: str | None = None,
                 reasoning: str | None = None):
        self.network_policy = network_policy
        self.privacy_state = _privacy_getter(privacy_state, env)
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
                "--ephemeral", "--color", "never", "-C", scratch, "-o", out_path,
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
                    proc = _spawn_reasoning_process(
                        argv,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=scratch,
                    )
            except FileNotFoundError as e:  # race: vanished between which() and spawn
                raise ProviderUnavailableError(
                    f"the Codex CLI ({self.bin!r}) could not be run: {e}"
                ) from e
            stdout, stderr = _communicate_reasoning_process(
                proc, input_text=full, timeout=self.timeout
            )
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
        return p.complete(prompt, system=system)

    if network_policy is None:
        raise ValueError("network_policy is required for an egress provider")
    state = _privacy_getter(privacy_state, env)
    _require_remote_reasoning(state(), provider_name=p.name)
    raise ProviderUnavailableError(
        f"egress provider {p.name!r} is disabled until it exposes an atomic launch boundary"
    )
