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

import os
import shutil
import subprocess
import tempfile
from typing import Callable, Protocol, runtime_checkable

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


class EgressConsentError(OfflineError):
    """Raised when a remote provider is selected without explicit transcript-egress consent."""


class ProviderUnavailableError(RuntimeError):
    """Raised when a provider can't run (unknown name, or the Codex CLI is missing)."""


class ReasoningDisabledError(ProviderUnavailableError):
    """Raised when no reasoning provider has been selected."""


def is_offline(env: dict | None = None) -> bool:
    """True when the engine-wide offline switch (``SPOOL_OFFLINE``) is set."""
    e = env if env is not None else os.environ
    return (e.get("SPOOL_OFFLINE") or "").strip().lower() in _TRUE


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    egress: bool

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class CodexProvider:
    """Bridge to the user's Codex CLI (ChatGPT/Codex subscription).

    Runs ``codex exec`` non-interactively in a **read-only sandbox** (so the agent
    can never touch the filesystem) and feeds the prompt over stdin — transcripts can
    be large. The final message on stdout is returned verbatim; the caller parses it.
    """

    name = "codex"
    egress = True

    def __init__(self, *, bin: str | None = None, model: str | None = None,
                 timeout: int | None = None, cwd: str | None = None,
                 reasoning: str | None = None):
        self.bin = bin or CODEX_BIN
        self.model = model if model is not None else CODEX_MODEL
        self.timeout = timeout if timeout is not None else CODEX_TIMEOUT
        self.cwd = cwd
        self.reasoning = CODEX_REASONING if reasoning is None else reasoning

    def complete(self, prompt: str, *, system: str | None = None) -> str:
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
            proc = subprocess.run(
                argv, input=full, capture_output=True, text=True,
                timeout=self.timeout, cwd=scratch,
            )
        except FileNotFoundError as e:  # race: vanished between which() and run()
            raise ProviderUnavailableError(f"the Codex CLI ({self.bin!r}) could not be run: {e}") from e
        finally:
            try:
                if not self.cwd:
                    shutil.rmtree(scratch, ignore_errors=True)
            except OSError:
                pass
        if proc.returncode != 0:
            self._cleanup(out_path)
            raise RuntimeError(
                f"codex exec failed (rc={proc.returncode}): {(proc.stderr or '').strip()[-500:]}"
            )
        try:
            with open(out_path) as f:
                answer = f.read()
        except OSError:
            answer = ""
        self._cleanup(out_path)
        return answer or proc.stdout

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass


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


def get_provider(provider: "str | LLMProvider | None" = None, *, env: dict | None = None) -> LLMProvider:
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
        return CodexProvider()
    raise ProviderUnavailableError(
        f"unknown LLM provider {name!r}. Built-in providers: 'none', 'codex'. The "
        "'agent' provider is injected by the MCP layer — pass it as an instance, not a name."
    )


def complete(prompt: str, *, system: str | None = None,
             provider: "str | LLMProvider | None" = None, env: dict | None = None) -> str:
    """Run a single completion through the resolved provider, enforcing offline-mode."""
    p = get_provider(provider, env=env)
    if getattr(p, "egress", False) and is_offline(env):
        raise OfflineError(
            f"LLM provider {p.name!r} needs network egress, but offline-mode "
            "(SPOOL_OFFLINE=1) is on. Use a local provider or turn offline-mode off. "
            "Only transcript text would have been sent — media never leaves the machine."
        )
    if getattr(p, "egress", False):
        e = env if env is not None else os.environ
        if (e.get("SPOOL_LLM_EGRESS_CONSENT") or "").strip().lower() not in _TRUE:
            raise EgressConsentError(
                f"LLM provider {p.name!r} requires explicit consent before transcript "
                "text can leave this machine."
            )
    return p.complete(prompt, system=system)
