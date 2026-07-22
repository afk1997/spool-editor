"""Phase 0 reasoning boundary.

Spool currently supports deterministic, explicitly injected local providers for
tests and local integrations only.  Remote reasoning has no supported zero-tool
transport in Phase 0, so every remote route fails before authentication, network
leases, provider code, or process creation can run.
"""
from __future__ import annotations

import os
from typing import Callable, Mapping, Protocol, runtime_checkable


DEFAULT_PROVIDER = "none"
REMOTE_REASONING_UNAVAILABLE_MESSAGE = (
    "Remote reasoning is unavailable in Phase 0 until a supported zero-tool "
    "transport ships."
)
_TRUE = {"1", "true", "yes", "on"}


class OfflineError(RuntimeError):
    """Compatibility error for the stronger engine-wide Offline fuse."""

    error_category = "offline_network_disabled"


class EgressConsentError(OfflineError):
    """Compatibility error retained for callers migrating from remote reasoning."""

    error_category = "egress_consent_required"


class ProviderUnavailableError(RuntimeError):
    """Raised when an LLM provider cannot be used."""


class ReasoningDisabledError(ProviderUnavailableError):
    """Compatibility error for callers that still distinguish provider selection."""

    error_category = "reasoning_provider_required"


class RemoteReasoningUnavailableError(ProviderUnavailableError):
    """Stable Phase 0 denial for every remote reasoning attempt."""

    error_category = "remote_reasoning_unavailable"

    def __init__(self) -> None:
        super().__init__(REMOTE_REASONING_UNAVAILABLE_MESSAGE)


def is_offline(env: Mapping[str, object] | None = None) -> bool:
    """Return whether the engine-wide ``SPOOL_OFFLINE`` setting is enabled."""

    current = os.environ if env is None else env
    return str(current.get("SPOOL_OFFLINE") or "").strip().lower() in _TRUE


PrivacyState = Callable[[], Mapping[str, object]]


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    egress: bool

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class CodexProvider:
    """Fail-closed compatibility stub for the removed Codex CLI bridge."""

    name = "codex"
    egress = True

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RemoteReasoningUnavailableError()

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        del prompt, system
        raise RemoteReasoningUnavailableError()


class NoneProvider:
    """The only named Phase 0 provider: reasoning is intentionally unavailable."""

    name = "none"
    egress = False

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        del prompt, system
        raise RemoteReasoningUnavailableError()


class CallableProvider:
    """Wrap an explicitly injected deterministic provider.

    Only providers whose ``egress`` metadata is the literal ``False`` may execute.
    This keeps local tests and read-only integrations deterministic without creating
    a hidden path for remote completion.
    """

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
        if self.egress is True:
            raise RemoteReasoningUnavailableError()
        if self.egress is not False:
            raise ProviderUnavailableError(
                "LLM provider egress metadata must be the literal bool True or False"
            )
        return self._fn(prompt, system=system)


def _provider_name(value: object) -> str:
    return str(value or DEFAULT_PROVIDER).strip().lower()


def _require_non_egress(provider: LLMProvider) -> LLMProvider:
    try:
        egress = provider.egress
    except Exception as exc:
        raise ProviderUnavailableError(
            "LLM provider egress metadata must be the literal bool True or False"
        ) from exc
    if egress is True:
        raise RemoteReasoningUnavailableError()
    if egress is not False:
        raise ProviderUnavailableError(
            "LLM provider egress metadata must be the literal bool True or False"
        )
    return provider


def get_provider(
    provider: "str | LLMProvider | None" = None,
    *,
    env: Mapping[str, object] | None = None,
    network_policy: object | None = None,
    privacy_state: PrivacyState | None = None,
) -> LLMProvider:
    """Resolve a Phase 0 provider without touching policy or privacy authorities."""

    del network_policy, privacy_state
    if provider is not None and not isinstance(provider, str):
        return _require_non_egress(provider)

    current = os.environ if env is None else env
    configured = provider if provider is not None else current.get("SPOOL_LLM_PROVIDER")
    name = _provider_name(configured)
    if name == "none":
        return NoneProvider()
    if name == "codex":
        raise RemoteReasoningUnavailableError()
    raise ProviderUnavailableError(
        f"unknown LLM provider {name!r}. Phase 0 supports only the disabled 'none' "
        "provider or an explicitly injected non-egress provider."
    )


def complete(
    prompt: str,
    *,
    system: str | None = None,
    provider: "str | LLMProvider | None" = None,
    env: Mapping[str, object] | None = None,
    network_policy: object | None = None,
    privacy_state: PrivacyState | None = None,
) -> str:
    """Run only an explicitly injected provider proven to be non-egress."""

    resolved = get_provider(
        provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )
    _require_non_egress(resolved)
    return resolved.complete(prompt, system=system)
