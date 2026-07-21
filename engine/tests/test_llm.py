"""Tests for clip.llm — the pluggable moment-finding LLM provider layer.

Remote reasoning is disabled by default. The opt-in ``codex`` provider shells out to the user's Codex CLI.
We never invoke the real CLI here — ``shutil.which`` and ``subprocess.Popen`` are
mocked and we assert the ``codex exec`` argv/stdin contract + the offline guard.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import signal
import shutil as stdlib_shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from clip import llm
from network_policy import NetworkPolicy
from settings import SettingsStore


class _FakeProc:
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def communicate(self, input=None, timeout=None):
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode


def _codex(*, policy=None, state=None, **kwargs):
    return llm.CodexProvider(
        network_policy=policy or NetworkPolicy(),
        privacy_state=lambda: dict(state or _privacy()),
        **kwargs,
    )


# --- provider resolution -------------------------------------------------

def test_get_provider_defaults_to_none():
    p = llm.get_provider(env={})
    assert isinstance(p, llm.NoneProvider)
    assert p.name == "none" and p.egress is False
    with pytest.raises(llm.ReasoningDisabledError):
        p.complete("hi")


def test_get_provider_passes_through_an_instance():
    """The MCP layer injects the agent's own LLM as a provider instance."""
    sentinel = llm.CallableProvider(lambda prompt, system=None: "x", name="agent")
    assert llm.get_provider(sentinel) is sentinel


def test_get_provider_reads_env_default():
    assert isinstance(
        llm.get_provider(
            env={"SPOOL_LLM_PROVIDER": "codex", "SPOOL_LLM_EGRESS_CONSENT": "1"},
            network_policy=NetworkPolicy(),
        ),
        llm.CodexProvider,
    )


def test_get_provider_unknown_name_raises():
    with pytest.raises(llm.ProviderUnavailableError, match="unknown LLM provider"):
        llm.get_provider("ollama-9000", env={})


# --- codex bridge --------------------------------------------------------

def _write_o(argv, text):
    """Mimic codex writing its final message to the --output-last-message file."""
    with open(argv[argv.index("-o") + 1], "w") as f:
        f.write(text)


def test_codex_builds_read_only_exec_argv(monkeypatch):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        _write_o(argv, "RESULT")
        proc = _FakeProc(returncode=0)

        def communicate(input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return "", ""

        proc.communicate = communicate
        return proc

    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", fake_popen)

    out = _codex().complete("find clips", system="you are a producer")

    assert out == "RESULT"  # read from the -o file, not the noisy stdout log
    argv = captured["argv"]
    assert argv[0] == "codex" and argv[1] == "exec"
    # read-only sandbox so the agent can never touch the filesystem
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv and "--ephemeral" in argv and "-o" in argv
    assert "model_reasoning_effort=low" in argv  # cheap-by-default (SPOOL_CODEX_REASONING)
    # prompt goes over stdin (transcripts can be large); system is prepended
    assert "find clips" in captured["input"]
    assert "you are a producer" in captured["input"]


def test_codex_includes_model_flag_when_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        _write_o(argv, "x")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(llm.subprocess, "Popen", fake_popen)
    _codex(model="gpt-5-codex").complete("hi")
    argv = captured["argv"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5-codex"


def test_codex_missing_cli_raises_unavailable(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: None)
    with pytest.raises(llm.ProviderUnavailableError, match="Codex CLI"):
        _codex().complete("hi")


def test_codex_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    monkeypatch.setattr(
        llm.subprocess,
        "Popen",
        lambda argv, **kw: _FakeProc(returncode=2, stderr="not signed in"),
    )
    with pytest.raises(RuntimeError, match="not signed in"):
        _codex().complete("hi")


# --- offline guard -------------------------------------------------------

@pytest.mark.parametrize("val,offline", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False)])
def test_is_offline_parsing(val, offline):
    assert llm.is_offline({"SPOOL_OFFLINE": val}) is offline


def test_complete_blocks_egress_provider_when_offline(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    policy = NetworkPolicy(offline=True)
    with pytest.raises(llm.OfflineError, match="offline"):
        llm.complete("hi", provider="codex", env={
            "SPOOL_OFFLINE": "1",
            "SPOOL_LLM_PROVIDER": "codex",
            "SPOOL_LLM_EGRESS_CONSENT": "1",
        }, network_policy=policy)


def test_complete_blocks_egress_without_explicit_consent():
    calls = []
    remote = llm.CallableProvider(
        lambda prompt, system=None: calls.append(prompt) or "should not run",
        name="remote",
        egress=True,
    )

    with pytest.raises(llm.EgressConsentError, match="consent"):
        llm.complete(
            "hi",
            provider=remote,
            env={"SPOOL_LLM_PROVIDER": "codex"},
            network_policy=NetworkPolicy(),
        )

    assert calls == []


def test_complete_fails_closed_for_opaque_egress_with_explicit_consent():
    calls = []
    remote = llm.CallableProvider(
        lambda prompt, system=None: calls.append(prompt) or "ok",
        name="remote",
        egress=True,
    )

    with pytest.raises(llm.ProviderUnavailableError, match="atomic launch boundary"):
        llm.complete(
            "hi",
            provider=remote,
            env={"SPOOL_LLM_PROVIDER": "codex", "SPOOL_LLM_EGRESS_CONSENT": "1"},
            network_policy=NetworkPolicy(),
        )
    assert calls == []


def test_complete_allows_local_provider_when_offline():
    """A non-egress (injected/local) provider still works offline."""
    local = llm.CallableProvider(lambda prompt, system=None: "ok", name="agent", egress=False)
    assert llm.complete("hi", provider=local, env={"SPOOL_OFFLINE": "1"}) == "ok"


def test_callable_provider_forwards_prompt_and_system():
    seen = {}

    def fn(prompt, system=None):
        seen.update(prompt=prompt, system=system)
        return "done"

    p = llm.CallableProvider(fn, name="agent")
    assert p.complete("the prompt", system="the system") == "done"
    assert seen == {"prompt": "the prompt", "system": "the system"}
    assert p.egress is False  # injected agent LLM: no engine-side egress


# --- Phase 0 direct network boundary ------------------------------------

def _privacy(*, offline=False, provider="codex", consent=True):
    return {
        "offline": offline,
        "reasoning_provider": provider,
        "reasoning_egress_consent": consent,
    }


@pytest.mark.parametrize(
    ("state", "error_type", "error_category"),
    [
        (_privacy(offline=True), llm.OfflineError, "offline_network_disabled"),
        (_privacy(provider="none"), llm.ReasoningDisabledError, "reasoning_provider_required"),
        (_privacy(consent=False), llm.EgressConsentError, "egress_consent_required"),
    ],
)
def test_direct_codex_boundary_rejects_live_privacy_state_before_cli_discovery(
    monkeypatch, state, error_type, error_category,
):
    policy = NetworkPolicy(offline=state["offline"])
    discoveries = []
    monkeypatch.setattr(llm.shutil, "which", lambda _bin: discoveries.append(_bin))

    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=lambda: dict(state),
    )
    with pytest.raises(error_type) as denied:
        provider.complete("transcript")

    assert denied.value.error_category == error_category
    assert discoveries == []
    assert policy.active_leases == 0


def test_arbitrary_egress_provider_never_enters_an_opaque_completion():
    policy = NetworkPolicy()
    calls = []

    def remote(prompt, *, system=None):
        calls.append((prompt, system, policy.active_leases))
        return "ok"

    provider = llm.CallableProvider(remote, name="remote-test", egress=True)
    with pytest.raises(llm.ProviderUnavailableError, match="atomic launch boundary"):
        llm.complete(
            "transcript",
            system="producer",
            provider=provider,
            network_policy=policy,
            privacy_state=lambda: _privacy(),
        )
    assert calls == []
    assert policy.active_leases == 0


def test_supplied_codex_rejects_a_different_call_level_policy_before_discovery(
    monkeypatch,
):
    owned_policy = NetworkPolicy()
    shared_policy = NetworkPolicy()
    state = _privacy()
    discoveries = []
    provider = llm.CodexProvider(
        network_policy=owned_policy,
        privacy_state=lambda: dict(state),
    )
    monkeypatch.setattr(
        llm.shutil, "which", lambda binary: discoveries.append(binary) or binary
    )
    monkeypatch.setattr(
        llm.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a mismatched policy must stop before process launch")
        ),
    )

    with pytest.raises(llm.ProviderUnavailableError, match="shared network policy"):
        llm.complete(
            "transcript",
            provider=provider,
            network_policy=shared_policy,
        )

    assert discoveries == []
    assert owned_policy.active_leases == 0
    assert shared_policy.active_leases == 0


def test_supplied_codex_rejects_a_different_call_level_privacy_source(
    monkeypatch,
):
    policy = NetworkPolicy()
    provider_state = lambda: _privacy()
    call_state = lambda: _privacy()
    discoveries = []
    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=provider_state,
    )
    monkeypatch.setattr(
        llm.shutil, "which", lambda binary: discoveries.append(binary) or binary
    )
    monkeypatch.setattr(
        llm.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a mismatched privacy source must stop before process launch")
        ),
    )

    with pytest.raises(llm.ProviderUnavailableError, match="shared privacy state"):
        llm.complete(
            "transcript",
            provider=provider,
            network_policy=policy,
            privacy_state=call_state,
        )

    assert discoveries == []
    assert policy.active_leases == 0


def test_supplied_codex_cannot_disguise_callback_authority_as_shared_env(
    tmp_path, monkeypatch,
):
    policy = NetworkPolicy()
    shared_env = {
        "SPOOL_OFFLINE": "1",
        "SPOOL_LLM_PROVIDER": "codex",
        "SPOOL_LLM_EGRESS_CONSENT": "1",
    }
    launches = []

    def fake_popen(argv, **kwargs):
        launches.append((argv, kwargs))
        _write_o(argv, "UNSAFE")
        return _FakeProc()

    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=lambda: _privacy(),
        env=shared_env,
        cwd=str(tmp_path),
    )
    monkeypatch.setattr(llm.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(llm.subprocess, "Popen", fake_popen)

    with pytest.raises(llm.OfflineError) as denied:
        llm.complete(
            "transcript",
            provider=provider,
            env=shared_env,
            network_policy=policy,
        )

    assert denied.value.error_category == "offline_network_disabled"
    assert launches == []
    assert policy.active_leases == 0


def test_call_level_offline_policy_precedes_a_supplied_codex_policy(monkeypatch):
    owned_policy = NetworkPolicy()
    shared_policy = NetworkPolicy(offline=True)
    spawns = []
    provider = llm.CodexProvider(
        network_policy=owned_policy,
        privacy_state=lambda: _privacy(),
    )
    monkeypatch.setattr(llm.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(
        llm.subprocess,
        "Popen",
        lambda *args, **kwargs: spawns.append((args, kwargs)) or _FakeProc(),
    )

    with pytest.raises(llm.OfflineError) as denied:
        llm.complete(
            "transcript",
            provider=provider,
            network_policy=shared_policy,
        )

    assert denied.value.error_category == "offline_network_disabled"
    assert spawns == []
    assert owned_policy.active_leases == 0
    assert shared_policy.active_leases == 0


def test_offline_policy_precedes_opaque_provider_unavailability():
    calls = []
    provider = llm.CallableProvider(
        lambda prompt, system=None: calls.append(prompt) or "unsafe",
        name="opaque",
        egress=True,
    )

    with pytest.raises(llm.OfflineError) as denied:
        llm.complete(
            "transcript",
            provider=provider,
            network_policy=NetworkPolicy(offline=True),
            privacy_state=lambda: _privacy(),
        )

    assert denied.value.error_category == "offline_network_disabled"
    assert calls == []


def test_codex_rechecks_live_consent_inside_lease_before_cli_discovery(monkeypatch):
    policy = NetworkPolicy()
    reads = []
    discoveries = []

    def privacy_state():
        reads.append(policy.active_leases)
        return _privacy(consent=len(reads) == 1)

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: discoveries.append(_bin))
    provider = llm.CodexProvider(network_policy=policy, privacy_state=privacy_state)

    with pytest.raises(llm.EgressConsentError) as denied:
        provider.complete("transcript")

    assert denied.value.error_category == "egress_consent_required"
    assert reads == [0, 1]
    assert discoveries == []
    assert policy.active_leases == 0


def test_codex_normalizes_a_live_environment_style_privacy_getter(monkeypatch):
    policy = NetworkPolicy(offline=True)
    live_env = {
        "SPOOL_OFFLINE": "1",
        "SPOOL_LLM_PROVIDER": "codex",
        "SPOOL_LLM_EGRESS_CONSENT": "1",
    }
    discoveries = []
    monkeypatch.setattr(llm.shutil, "which", lambda _bin: discoveries.append(_bin))

    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=lambda: live_env,
    )
    with pytest.raises(llm.OfflineError) as denied:
        provider.complete("transcript")

    assert denied.value.error_category == "offline_network_disabled"
    assert discoveries == []


def test_codex_holds_lease_until_owned_process_group_is_quiescent(monkeypatch):
    policy = NetworkPolicy()
    events = []

    class CompletedProcess:
        pid = 717171
        returncode = 0

        def __init__(self, argv, **kwargs):
            events.append(("spawn", kwargs.get("start_new_session"), policy.active_leases))
            self.argv = argv

        def communicate(self, input=None, timeout=None):
            events.append(("communicate", input, timeout, policy.active_leases))
            _write_o(self.argv, "RESULT")
            return "noise", ""

        def poll(self):
            return self.returncode

    def group_probe(pgid, sig):
        events.append(("group", pgid, sig, policy.active_leases))
        raise ProcessLookupError

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", CompletedProcess)
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess.run cannot own the Codex process tree")
        ),
    )
    monkeypatch.setattr(llm.os, "killpg", group_probe)

    assert _codex(policy=policy).complete("find clips") == "RESULT"
    assert events[0] == ("spawn", True, 1)
    assert events[1][0] == "communicate" and events[1][-1] == 1
    assert events[-1] == ("group", CompletedProcess.pid, 0, 1)
    assert policy.active_leases == 0


def test_owned_process_kill_falls_back_when_session_group_was_never_created(monkeypatch):
    calls = []

    class SessionIgnoringShim:
        pid = 727272

        def kill(self):
            calls.append("parent-kill")

    owned = llm._OwnedReasoningProcess(SessionIgnoringShim(), owns_group=True)
    monkeypatch.setattr(
        llm.os,
        "killpg",
        lambda _pgid, _sig: (_ for _ in ()).throw(ProcessLookupError),
    )

    owned.kill()

    assert calls == ["parent-kill"]


def test_codex_rechecks_consent_after_local_setup_immediately_before_spawn(
    tmp_path, monkeypatch,
):
    policy = NetworkPolicy()
    state = _privacy()
    output_paths = []
    real_mkstemp = llm.tempfile.mkstemp

    def revoke_after_output_created(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        output_paths.append(path)
        state["reasoning_egress_consent"] = False
        return fd, path

    spawns = []
    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.tempfile, "mkstemp", revoke_after_output_created)
    monkeypatch.setattr(llm.subprocess, "Popen", lambda *a, **k: spawns.append(a) or _FakeProc())

    with pytest.raises(llm.EgressConsentError) as denied:
        _codex(policy=policy, state=state, cwd=str(tmp_path)).complete("transcript")

    assert denied.value.error_category == "egress_consent_required"
    assert spawns == []
    assert output_paths and all(not Path(path).exists() for path in output_paths)
    assert policy.active_leases == 0


def _consented_settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    })
    return store


def _commit_settings(policy, store, changes):
    requested_offline = changes["offline"] if "offline" in changes else None
    with policy.transition(requested_offline):
        return store.update(changes)


def test_revocation_that_commits_before_launch_guard_runs_zero_process(
    tmp_path, monkeypatch,
):
    policy = NetworkPolicy()
    store = _consented_settings(tmp_path)
    discovery_entered = threading.Event()
    release_discovery = threading.Event()
    spawns = []
    outcome = []

    def blocking_discovery(_bin):
        discovery_entered.set()
        assert release_discovery.wait(2), "CLI discovery was not released"
        return "/usr/local/bin/codex"

    monkeypatch.setattr(llm.shutil, "which", blocking_discovery)
    monkeypatch.setattr(
        llm.subprocess,
        "Popen",
        lambda *args, **kwargs: spawns.append((args, kwargs)) or _FakeProc(),
    )
    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=store.get,
        cwd=str(tmp_path),
    )

    def complete():
        try:
            outcome.append(provider.complete("transcript"))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=complete)
    worker.start()
    assert discovery_entered.wait(2), "provider never reached local setup"

    _commit_settings(policy, store, {"reasoning_egress_consent": False})
    release_discovery.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], llm.EgressConsentError)
    assert outcome[0].error_category == "egress_consent_required"
    assert spawns == []
    assert policy.active_leases == 0


def test_launch_guard_wins_before_revocation_without_deadlock(tmp_path, monkeypatch):
    policy = NetworkPolicy()
    store = _consented_settings(tmp_path)
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    communicate_entered = threading.Event()
    release_complete = threading.Event()
    patch_attempted = threading.Event()
    patch_acquired = threading.Event()
    patch_done = threading.Event()
    outcome = []

    class BlockingProcess(_FakeProc):
        def __init__(self, argv, **kwargs):
            super().__init__()
            self.argv = argv
            spawn_entered.set()
            assert release_spawn.wait(2), "Popen construction was not released"

        def communicate(self, input=None, timeout=None):
            communicate_entered.set()
            assert release_complete.wait(2), "Codex completion was not released"
            _write_o(self.argv, "RESULT")
            return "", ""

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", BlockingProcess)
    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=store.get,
        cwd=str(tmp_path),
    )

    real_transition = policy.transition

    @contextmanager
    def observed_transition(offline):
        patch_attempted.set()
        with real_transition(offline):
            patch_acquired.set()
            yield

    monkeypatch.setattr(policy, "transition", observed_transition)

    provider_thread = threading.Thread(
        target=lambda: outcome.append(provider.complete("transcript"))
    )
    provider_thread.start()
    patch_thread = None
    try:
        assert spawn_entered.wait(2), "provider never entered Popen"

        patch_thread = threading.Thread(
            target=lambda: (
                _commit_settings(policy, store, {"reasoning_egress_consent": False}),
                patch_done.set(),
            )
        )
        patch_thread.start()
        assert patch_attempted.wait(2), "settings patch never attempted the policy lock"
        assert patch_acquired.wait(0.1) is False

        release_spawn.set()
        assert communicate_entered.wait(2), "provider never left launch admission"
        assert patch_acquired.wait(2), "settings patch never acquired after Popen linearized"
        assert patch_done.wait(2), "settings patch deadlocked after Popen linearized"
        assert store.get()["reasoning_egress_consent"] is False
    finally:
        release_spawn.set()
        release_complete.set()
        provider_thread.join(2)
        if patch_thread is not None:
            patch_thread.join(2)

    assert not provider_thread.is_alive() and not patch_thread.is_alive()
    assert outcome == ["RESULT"]
    assert policy.active_leases == 0


def test_opaque_egress_fails_closed_without_blocking_privacy_transition():
    policy = NetworkPolicy()
    state = _privacy()
    provider_entered = threading.Event()
    release_provider = threading.Event()
    invocation_started = threading.Event()
    invocation_done = threading.Event()
    transition_done = threading.Event()
    calls = []
    outcome = []

    def remote(_prompt, *, system=None):
        calls.append((_prompt, system))
        provider_entered.set()
        assert release_provider.wait(2), "opaque provider was not released"
        return "unsafe"

    def invoke():
        invocation_started.set()
        try:
            outcome.append(llm.complete(
                "transcript",
                provider=llm.CallableProvider(remote, name="opaque", egress=True),
                network_policy=policy,
                privacy_state=lambda: dict(state),
            ))
        except Exception as exc:  # noqa: BLE001 - capture the exact boundary error
            outcome.append(exc)
        finally:
            invocation_done.set()

    def revoke():
        with policy.transition(None):
            state["reasoning_egress_consent"] = False
        transition_done.set()

    invocation_thread = threading.Thread(target=invoke)
    transition_thread = threading.Thread(target=revoke)
    invocation_thread.start()
    try:
        assert invocation_started.wait(1), "invocation thread never started"
        deadline = time.monotonic() + 1
        while (
            not provider_entered.is_set()
            and not invocation_done.is_set()
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)

        transition_thread.start()
        invocation_returned_promptly = invocation_done.wait(0.5)
        transition_returned_promptly = transition_done.wait(0.5)
    finally:
        release_provider.set()
        invocation_thread.join(2)
        if transition_thread.ident is not None:
            transition_thread.join(2)

    assert invocation_returned_promptly
    assert transition_returned_promptly
    assert not invocation_thread.is_alive() and not transition_thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], llm.ProviderUnavailableError)
    assert "atomic launch boundary" in str(outcome[0])
    assert calls == []
    assert state["reasoning_egress_consent"] is False
    assert policy.active_leases == 0


def test_codex_logs_output_temp_cleanup_failure(tmp_path, monkeypatch, caplog):
    policy = NetworkPolicy()
    real_unlink = llm.os.unlink
    output_paths = []

    def fake_popen(argv, **kwargs):
        _write_o(argv, "RESULT")
        return _FakeProc()

    def failed_unlink(path):
        output_paths.append(path)
        raise OSError("permission denied")

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(llm.os, "unlink", failed_unlink)

    try:
        with caplog.at_level(logging.WARNING, logger=llm.__name__):
            assert _codex(policy=policy, cwd=str(tmp_path)).complete("transcript") == "RESULT"
        assert "could not remove Codex output temp" in caplog.text
        assert "permission denied" in caplog.text
    finally:
        for path in output_paths:
            try:
                real_unlink(path)
            except FileNotFoundError:
                pass


def test_codex_logs_scratch_cleanup_failure(monkeypatch, caplog):
    policy = NetworkPolicy()
    real_rmtree = stdlib_shutil.rmtree
    scratch_paths = []

    def fake_popen(argv, **kwargs):
        scratch_paths.append(kwargs["cwd"])
        _write_o(argv, "RESULT")
        return _FakeProc()

    def failed_rmtree(path, **kwargs):
        raise OSError("scratch busy")

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(llm.shutil, "rmtree", failed_rmtree)

    try:
        with caplog.at_level(logging.WARNING, logger=llm.__name__):
            assert _codex(policy=policy).complete("transcript") == "RESULT"
        assert "could not remove Codex scratch directory" in caplog.text
        assert "scratch busy" in caplog.text
    finally:
        for path in scratch_paths:
            real_rmtree(path, ignore_errors=True)


@pytest.mark.parametrize("failure", ["spawn", "timeout", "nonzero"])
def test_codex_removes_output_temp_on_every_failure(tmp_path, monkeypatch, failure):
    policy = NetworkPolicy()
    output_paths = []
    real_mkstemp = llm.tempfile.mkstemp

    def capture_output(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        output_paths.append(path)
        return fd, path

    class FailureProcess(_FakeProc):
        def __init__(self):
            super().__init__(returncode=2 if failure == "nonzero" else 0, stderr="boom")
            self._first = True

        def communicate(self, input=None, timeout=None):
            if failure == "timeout" and self._first:
                self._first = False
                raise llm.subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
            return "", self.stderr

        def kill(self):
            self.returncode = -9

    def spawn(*args, **kwargs):
        if failure == "spawn":
            raise FileNotFoundError("vanished")
        return FailureProcess()

    monkeypatch.setattr(llm.shutil, "which", lambda _bin: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.tempfile, "mkstemp", capture_output)
    monkeypatch.setattr(llm.subprocess, "Popen", spawn)

    error = (
        llm.ProviderUnavailableError
        if failure == "spawn"
        else llm.subprocess.TimeoutExpired
        if failure == "timeout"
        else RuntimeError
    )
    with pytest.raises(error):
        _codex(policy=policy, cwd=str(tmp_path)).complete("transcript")

    assert output_paths and all(not Path(path).exists() for path in output_paths)
    assert policy.active_leases == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_codex_timeout_kills_real_descendant_before_releasing_lease(
    tmp_path, monkeypatch,
):
    policy = NetworkPolicy()
    child_pid_file = tmp_path / "child.pid"
    bridge = tmp_path / "codex-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(os.environ['SPOOL_TEST_CHILD_PID'], 'w') as f:\n"
        "    f.write(str(child.pid)); f.flush(); os.fsync(f.fileno())\n"
        "sys.stdin.read()\n"
        "time.sleep(60)\n"
    )
    bridge.chmod(0o755)
    monkeypatch.setenv("SPOOL_TEST_CHILD_PID", str(child_pid_file))

    child_pid = None
    try:
        with pytest.raises(llm.subprocess.TimeoutExpired):
            _codex(policy=policy, bin=str(bridge), timeout=3.0).complete("transcript")
        for _ in range(100):
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
                break
            time.sleep(0.01)
        assert child_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert policy.active_leases == 0
    finally:
        if child_pid is None and child_pid_file.exists():
            child_pid = int(child_pid_file.read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_reasoning_registry_shutdown_kills_live_group_and_rejects_new_launches(
    tmp_path, monkeypatch,
):
    policy = NetworkPolicy()
    registry = llm.reasoning_process_registry(policy)
    bridge_pid_file = tmp_path / "bridge.pid"
    bridge = tmp_path / "codex-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "with open(os.environ['SPOOL_TEST_BRIDGE_PID'], 'w') as f:\n"
        "    f.write(str(os.getpid())); f.flush(); os.fsync(f.fileno())\n"
        "sys.stdin.read()\n"
        "time.sleep(60)\n"
    )
    bridge.chmod(0o755)
    monkeypatch.setenv("SPOOL_TEST_BRIDGE_PID", str(bridge_pid_file))
    provider = llm.CodexProvider(
        network_policy=policy,
        privacy_state=lambda: _privacy(),
        process_registry=registry,
        bin=str(bridge),
        timeout=60,
    )
    outcome = []
    worker = threading.Thread(
        target=lambda: _capture_completion(outcome, provider),
    )
    bridge_pid = None
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not bridge_pid_file.exists():
            time.sleep(0.01)
        assert bridge_pid_file.exists(), "Codex bridge never launched"
        bridge_pid = int(bridge_pid_file.read_text())
        assert registry.active_count == 1
        assert policy.active_leases == 1

        registry.shutdown(timeout=3)
        worker.join(5)

        assert not worker.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
        assert registry.active_count == 0
        assert registry.closing is True
        assert policy.active_leases == 0
        with pytest.raises(ProcessLookupError):
            os.kill(bridge_pid, 0)

        spawns = []
        monkeypatch.setattr(
            llm.subprocess,
            "Popen",
            lambda *args, **kwargs: spawns.append((args, kwargs)) or _FakeProc(),
        )
        with pytest.raises(llm.ProviderUnavailableError, match="shutting down"):
            provider.complete("another transcript")
        assert spawns == []
    finally:
        if worker.is_alive():
            registry.shutdown(timeout=3)
            worker.join(5)
        if bridge_pid is not None:
            try:
                os.killpg(bridge_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _capture_completion(outcome, provider):
    try:
        outcome.append(provider.complete("transcript"))
    except BaseException as exc:
        outcome.append(exc)


def test_reasoning_registry_retains_a_tree_until_exit_is_confirmed():
    registry = llm.ReasoningProcessRegistry()

    class UnconfirmedProcess:
        tree_exited = False

        def __init__(self):
            self.attempts = 0

        def kill(self):
            pass

        def terminate_and_wait(self, *, timeout):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("group still alive")
            self.tree_exited = True

    process = UnconfirmedProcess()
    registry.spawn(lambda: process)

    registry.shutdown(timeout=0)
    assert registry.active_count == 1

    registry.shutdown(timeout=0)
    assert process.attempts == 2
    assert registry.active_count == 0


def test_codex_provider_rejects_a_registry_outside_its_shared_policy_boundary():
    policy = NetworkPolicy()
    rogue_registry = llm.ReasoningProcessRegistry()

    with pytest.raises(ValueError, match="shared reasoning process registry"):
        llm.CodexProvider(
            network_policy=policy,
            privacy_state=lambda: _privacy(),
            process_registry=rogue_registry,
        )
