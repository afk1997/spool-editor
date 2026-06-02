"""Tests for clip.llm — the pluggable moment-finding LLM provider layer.

The default provider is the *codex bridge*: it shells out to the user's Codex CLI.
We never invoke the real CLI here — ``shutil.which`` and ``subprocess.run`` are
mocked and we assert the ``codex exec`` argv/stdin contract + the offline guard.
"""
from __future__ import annotations

import pytest

from clip import llm


class _FakeProc:
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- provider resolution -------------------------------------------------

def test_get_provider_defaults_to_codex():
    p = llm.get_provider(env={})
    assert isinstance(p, llm.CodexProvider)
    assert p.name == "codex" and p.egress is True


def test_get_provider_passes_through_an_instance():
    """The MCP layer injects the agent's own LLM as a provider instance."""
    sentinel = llm.CallableProvider(lambda prompt, system=None: "x", name="agent")
    assert llm.get_provider(sentinel) is sentinel


def test_get_provider_reads_env_default():
    assert isinstance(llm.get_provider(env={"SPOOL_LLM_PROVIDER": "codex"}), llm.CodexProvider)


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

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        _write_o(argv, "RESULT")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    out = llm.CodexProvider().complete("find clips", system="you are a producer")

    assert out == "RESULT"  # read from the -o file, not the noisy stdout log
    argv = captured["argv"]
    assert argv[0] == "codex" and argv[1] == "exec"
    # read-only sandbox so the agent can never touch the filesystem
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv and "--ephemeral" in argv and "-o" in argv
    # prompt goes over stdin (transcripts can be large); system is prepended
    assert "find clips" in captured["kw"]["input"]
    assert "you are a producer" in captured["kw"]["input"]


def test_codex_includes_model_flag_when_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")

    def fake_run(argv, **kw):
        captured["argv"] = argv
        _write_o(argv, "x")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    llm.CodexProvider(model="gpt-5-codex").complete("hi")
    argv = captured["argv"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5-codex"


def test_codex_missing_cli_raises_unavailable(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: None)
    with pytest.raises(llm.ProviderUnavailableError, match="Codex CLI"):
        llm.CodexProvider().complete("hi")


def test_codex_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    monkeypatch.setattr(llm.subprocess, "run", lambda argv, **kw: _FakeProc(returncode=2, stderr="not signed in"))
    with pytest.raises(RuntimeError, match="not signed in"):
        llm.CodexProvider().complete("hi")


# --- offline guard -------------------------------------------------------

@pytest.mark.parametrize("val,offline", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False)])
def test_is_offline_parsing(val, offline):
    assert llm.is_offline({"SPOOL_OFFLINE": val}) is offline


def test_complete_blocks_egress_provider_when_offline(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: "/usr/local/bin/codex")
    with pytest.raises(llm.OfflineError, match="offline"):
        llm.complete("hi", provider="codex", env={"SPOOL_OFFLINE": "1"})


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
