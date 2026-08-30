"""Small provider boundary for optional transcript moment-finding.

Spool stays local by default. A user may explicitly enable the Codex CLI bridge,
which sends transcript text (never media) to Codex for moment suggestions. The CLI
runs ephemerally from an empty temporary working directory; Spool's broader agent and
mutation surfaces remain disabled.
"""
from __future__ import annotations

from contextlib import nullcontext
import os
import shutil
import subprocess
import tempfile
from typing import Callable, Mapping, Protocol, runtime_checkable


DEFAULT_PROVIDER = "none"
CODEX_BIN = os.environ.get("SPOOL_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("SPOOL_CODEX_MODEL") or None
CODEX_TIMEOUT = int(os.environ.get("SPOOL_CODEX_TIMEOUT", "180"))
_TRUE = {"1", "true", "yes", "on"}


class OfflineError(RuntimeError):
    error_category = "offline_network_disabled"


class ProviderUnavailableError(RuntimeError):
    """Raised when a configured provider cannot run."""


class EgressConsentError(ProviderUnavailableError):
    error_category = "egress_consent_required"

    def __init__(self) -> None:
        super().__init__(
            "Enable Codex moment suggestions in Settings to allow transcript-text egress."
        )


class ReasoningDisabledError(ProviderUnavailableError):
    error_category = "reasoning_provider_required"

    def __init__(self) -> None:
        super().__init__("No reasoning provider is enabled.")


class RemoteReasoningUnavailableError(ProviderUnavailableError):
    """Compatibility error used by the intentionally disabled general agent surface."""

    error_category = "remote_reasoning_unavailable"

    def __init__(self) -> None:
        super().__init__("The general remote agent is unavailable.")


def is_offline(env: Mapping[str, object] | None = None) -> bool:
    current = os.environ if env is None else env
    return str(current.get("SPOOL_OFFLINE") or "").strip().lower() in _TRUE


PrivacyState = Callable[[], Mapping[str, object]]


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    egress: bool

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class CodexProvider:
    """Run one non-interactive Codex completion in an empty scratch directory."""

    name = "codex"
    egress = True

    def __init__(
        self,
        *,
        bin: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.bin = bin or CODEX_BIN
        self.model = model if model is not None else CODEX_MODEL
        self.timeout = timeout if timeout is not None else CODEX_TIMEOUT

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if shutil.which(self.bin) is None:
            raise ProviderUnavailableError(
                f"Codex CLI {self.bin!r} was not found. Install @openai/codex and run "
                "`codex login`, or disable Codex moment suggestions in Settings."
            )
        full_prompt = prompt if not system else f"{system}\n\n{prompt}"
        with tempfile.TemporaryDirectory(prefix="spool-codex-") as scratch:
            argv = [
                self.bin,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--color",
                "never",
                "-C",
                scratch,
            ]
            if self.model:
                argv += ["-m", self.model]
            argv.append("-")
            try:
                result = subprocess.run(
                    argv,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=scratch,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise ProviderUnavailableError(f"Codex CLI could not complete: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[-500:]
            raise ProviderUnavailableError(
                f"Codex CLI failed with exit code {result.returncode}: {detail}"
            )
        return result.stdout


class NoneProvider:
    name = "none"
    egress = False

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        del prompt, system
        raise ReasoningDisabledError()


class CallableProvider:
    """Wrap a deterministic local completion used by tests and local integrations."""

    def __init__(
        self,
        fn: Callable[..., str],
        *,
        name: str = "agent",
        egress: bool = False,
    ) -> None:
        self._fn = fn
        self.name = name
        self.egress = egress

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return self._fn(prompt, system=system)


def _provider_egress(provider: LLMProvider) -> bool:
    try:
        value = provider.egress
    except Exception as exc:
        raise ProviderUnavailableError(
            "LLM provider egress metadata must be the literal bool True or False"
        ) from exc
    if value is not True and value is not False:
        raise ProviderUnavailableError(
            "LLM provider egress metadata must be the literal bool True or False"
        )
    return value


def get_provider(
    provider: "str | LLMProvider | None" = None,
    *,
    env: Mapping[str, object] | None = None,
    network_policy: object | None = None,
    privacy_state: PrivacyState | None = None,
) -> LLMProvider:
    del network_policy, privacy_state
    if provider is not None and not isinstance(provider, str):
        _provider_egress(provider)
        return provider
    current = os.environ if env is None else env
    configured = provider if provider is not None else current.get("SPOOL_LLM_PROVIDER")
    name = str(configured or DEFAULT_PROVIDER).strip().lower()
    if name == "none":
        return NoneProvider()
    if name == "codex":
        return CodexProvider()
    raise ProviderUnavailableError(
        f"unknown LLM provider {name!r}. Built-in providers: 'none', 'codex'."
    )


def _privacy_values(
    env: Mapping[str, object], privacy_state: PrivacyState | None
) -> tuple[str, bool, bool]:
    if privacy_state is not None:
        values = privacy_state()
        provider = str(values.get("reasoning_provider") or "none").strip().lower()
        consent = values.get("reasoning_egress_consent") is True
        offline = values.get("offline") is True
        return provider, consent, offline
    provider = str(env.get("SPOOL_LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    consent = str(env.get("SPOOL_LLM_EGRESS_CONSENT") or "").strip().lower() in _TRUE
    return provider, consent, is_offline(env)


def complete(
    prompt: str,
    *,
    system: str | None = None,
    provider: "str | LLMProvider | None" = None,
    env: Mapping[str, object] | None = None,
    network_policy: object | None = None,
    privacy_state: PrivacyState | None = None,
) -> str:
    """Complete once, enforcing provider selection, consent, and Offline mode."""

    current = os.environ if env is None else env
    resolved = get_provider(provider, env=current)
    egress = _provider_egress(resolved)
    if not egress:
        return resolved.complete(prompt, system=system)

    selected, consent, settings_offline = _privacy_values(current, privacy_state)
    policy_offline = bool(
        getattr(network_policy, "offline", False) if network_policy is not None else False
    )
    if settings_offline or is_offline(current) or policy_offline:
        raise OfflineError("Offline mode blocks remote reasoning.")
    if selected != resolved.name:
        raise ReasoningDisabledError()
    if not consent:
        raise EgressConsentError()

    lease = (
        network_policy.egress("codex_reasoning")
        if network_policy is not None
        else nullcontext()
    )
    with lease:
        return resolved.complete(prompt, system=system)
