"""Focused tests for the optional Codex moment-finding bridge."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from clip import llm
from network_policy import NetworkPolicy


def _enabled_state():
    return {
        "offline": False,
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    }


def test_none_remains_the_default_and_does_not_reason():
    provider = llm.get_provider(env={})
    assert isinstance(provider, llm.NoneProvider)
    with pytest.raises(llm.ReasoningDisabledError):
        llm.complete("private transcript", provider=provider, env={})


def test_codex_requires_explicit_transcript_egress_consent(monkeypatch):
    calls = []
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(llm.EgressConsentError) as denied:
        llm.complete(
            "private transcript",
            provider="codex",
            env={"SPOOL_LLM_PROVIDER": "codex"},
            privacy_state=lambda: {
                "offline": False,
                "reasoning_provider": "codex",
                "reasoning_egress_consent": False,
            },
        )

    assert denied.value.error_category == "egress_consent_required"
    assert calls == []


def test_offline_blocks_codex_before_process_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(llm.OfflineError) as denied:
        llm.complete(
            "private transcript",
            provider="codex",
            env={"SPOOL_OFFLINE": "1", "SPOOL_LLM_PROVIDER": "codex"},
            privacy_state=_enabled_state,
            network_policy=NetworkPolicy(offline=True),
        )

    assert denied.value.error_category == "offline_network_disabled"
    assert calls == []


def test_codex_runs_in_an_ephemeral_scratch_directory_and_reads_prompt_from_stdin(
    monkeypatch,
):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='[{"start":1,"end":12}]\n', stderr="")

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    policy = NetworkPolicy()

    result = llm.complete(
        "TRANSCRIPT TEXT",
        system="RETURN JSON",
        provider="codex",
        env={"SPOOL_LLM_PROVIDER": "codex"},
        privacy_state=_enabled_state,
        network_policy=policy,
    )

    assert result == '[{"start":1,"end":12}]\n'
    assert captured["input"] == "RETURN JSON\n\nTRANSCRIPT TEXT"
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert "--sandbox" in captured["argv"]
    assert "read-only" in captured["argv"]
    assert "--ephemeral" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert "--ignore-rules" in captured["argv"]
    assert captured["argv"][-1] == "-"
    scratch = captured["argv"][captured["argv"].index("-C") + 1]
    assert captured["cwd"] == scratch
    assert policy.active_leases == 0


def test_local_callable_provider_still_works_without_egress():
    captured = []
    local = llm.CallableProvider(
        lambda prompt, system=None: captured.append((prompt, system)) or "done",
        name="deterministic-local",
        egress=False,
    )

    assert llm.complete(
        "the prompt",
        system="the system",
        provider=local,
        env={"SPOOL_OFFLINE": "1"},
        network_policy=NetworkPolicy(offline=True),
    ) == "done"
    assert captured == [("the prompt", "the system")]


def test_unknown_named_provider_stays_distinguishable():
    with pytest.raises(llm.ProviderUnavailableError, match="unknown LLM provider"):
        llm.get_provider("mystery-cloud", env={})


@pytest.mark.parametrize("egress", [None, 0, 1, "false", object()])
def test_opaque_egress_metadata_is_rejected(egress):
    class OpaqueProvider:
        name = "opaque"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("completion should not run")

    provider = OpaqueProvider()
    if egress is not None:
        provider.egress = egress

    with pytest.raises(llm.ProviderUnavailableError, match="egress metadata"):
        llm.complete("private transcript", provider=provider)
