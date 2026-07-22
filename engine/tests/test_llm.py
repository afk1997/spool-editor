"""Focused tests for the Phase 0 reasoning boundary."""
from __future__ import annotations

import ast
import inspect

import pytest

from clip import llm


EXPECTED_MESSAGE = (
    "Remote reasoning is unavailable in Phase 0 until a supported zero-tool "
    "transport ships."
)


def _assert_stable_unavailable(call) -> None:
    with pytest.raises(llm.RemoteReasoningUnavailableError) as denied:
        call()
    assert denied.value.error_category == "remote_reasoning_unavailable"
    assert str(denied.value) == EXPECTED_MESSAGE


class _ExplodingPolicy:
    @property
    def offline(self):
        raise AssertionError("policy was inspected")

    def egress(self, _reason):
        raise AssertionError("egress lease was requested")


def test_none_is_the_only_named_provider_and_completion_is_unavailable():
    provider = llm.get_provider(env={})
    assert isinstance(provider, llm.NoneProvider)
    assert provider.name == "none"
    assert provider.egress is False
    _assert_stable_unavailable(lambda: provider.complete("private transcript"))


@pytest.mark.parametrize("name", ["codex", "CoDeX", " CoDeX "])
def test_codex_factory_is_stably_unavailable_before_policy_access(name):
    _assert_stable_unavailable(
        lambda: llm.get_provider(name, network_policy=_ExplodingPolicy())
    )


def test_hostile_environment_codex_is_normalized_and_denied():
    _assert_stable_unavailable(
        lambda: llm.get_provider(
            env={
                "SPOOL_LLM_PROVIDER": " CoDeX ",
                "SPOOL_LLM_EGRESS_CONSENT": "true",
                "SPOOL_OFFLINE": "0",
            },
            network_policy=_ExplodingPolicy(),
        )
    )


def test_unknown_named_provider_stays_distinguishable():
    with pytest.raises(llm.ProviderUnavailableError, match="unknown LLM provider"):
        llm.get_provider("ollama-9000", env={})


def test_codex_constructor_and_compatibility_complete_are_fail_closed():
    _assert_stable_unavailable(
        lambda: llm.CodexProvider(
            network_policy=_ExplodingPolicy(),
            privacy_state=lambda: (_ for _ in ()).throw(
                AssertionError("privacy state was inspected")
            ),
            bin=object(),
        )
    )

    compatibility_instance = object.__new__(llm.CodexProvider)
    _assert_stable_unavailable(
        lambda: compatibility_instance.complete("private transcript")
    )


def test_local_callable_provider_is_preserved_and_receives_only_completion_inputs():
    captured = []
    local = llm.CallableProvider(
        lambda prompt, system=None: captured.append((prompt, system)) or "done",
        name="deterministic-local",
        egress=False,
    )

    assert llm.get_provider(local) is local
    assert llm.complete(
        "the prompt",
        system="the system",
        provider=local,
        env={"SPOOL_LLM_PROVIDER": "CoDeX"},
        network_policy=_ExplodingPolicy(),
    ) == "done"
    assert captured == [("the prompt", "the system")]


def test_callable_remote_complete_is_directly_fail_closed():
    calls = []
    remote = llm.CallableProvider(
        lambda *_args, **_kwargs: calls.append("complete"),
        name="remote-test",
        egress=True,
    )

    _assert_stable_unavailable(lambda: remote.complete("private transcript"))
    assert calls == []


def test_provider_factory_rejects_remote_instance_before_returning_it():
    calls = []
    remote = llm.CallableProvider(
        lambda *_args, **_kwargs: calls.append("complete"),
        name="remote-test",
        egress=True,
    )

    _assert_stable_unavailable(lambda: llm.get_provider(remote))
    assert calls == []


def test_completion_rejects_custom_remote_before_provider_or_lease_use():
    events = []

    class RemoteProvider:
        name = "remote-custom"
        egress = True

        def complete(self, *_args, **_kwargs):
            events.append("complete")

    _assert_stable_unavailable(
        lambda: llm.complete(
            "private transcript",
            provider=RemoteProvider(),
            network_policy=_ExplodingPolicy(),
        )
    )
    assert events == []


@pytest.mark.parametrize("egress", [None, 0, 1, "false", object()])
def test_opaque_egress_metadata_is_rejected_before_provider_use(egress):
    calls = []

    class OpaqueProvider:
        name = "opaque"

        def complete(self, *_args, **_kwargs):
            calls.append("complete")

    provider = OpaqueProvider()
    if egress is not None:
        provider.egress = egress

    with pytest.raises(llm.ProviderUnavailableError, match="egress metadata"):
        llm.complete("private transcript", provider=provider)
    assert calls == []


def test_broken_egress_property_is_rejected_before_provider_use():
    class BrokenProvider:
        name = "broken"

        @property
        def egress(self):
            raise RuntimeError("metadata unavailable")

        def complete(self, *_args, **_kwargs):
            raise AssertionError("provider completion was invoked")

    with pytest.raises(llm.ProviderUnavailableError, match="egress metadata"):
        llm.get_provider(BrokenProvider())


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("TRUE", True), (" yes ", True), ("0", False), ("", False)],
)
def test_is_offline(value, expected):
    assert llm.is_offline({"SPOOL_OFFLINE": value}) is expected


def test_phase_zero_module_contains_no_cli_auth_or_process_registry_machinery():
    tree = ast.parse(inspect.getsource(llm))
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert imported.isdisjoint({"shutil", "signal", "stat", "subprocess", "tempfile"})
    assert not hasattr(llm, "ReasoningProcessRegistry")
    assert not hasattr(llm, "reasoning_process_registry")
    assert not any(name.startswith("CODEX_") for name in vars(llm))
