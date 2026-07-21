"""Tests for clip.agent.run_agent — the bounded ReAct tool-loop (the in-app agent's real brain).

The LLM is mocked (a scripted CallableProvider, no codex) and the TroveClient is a tiny fake, so
the loop's mechanics — tool dispatch, observation feedback, trace, job collection, clarify, error
recovery, step budget — are tested without a server or a model.
"""
from __future__ import annotations

import json

import pytest

from clip import agent, agent_tools, llm
from network_policy import NetworkPolicy


def _provider(*responses):
    """A CallableProvider that returns the given JSON strings in order (last repeats)."""
    seq = list(responses)
    state = {"i": 0}

    def fn(prompt, *, system=None):
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return llm.CallableProvider(fn)


class _FakeClient:
    """Stands in for TroveClient — records calls, returns canned shapes the tools expect."""

    def __init__(self):
        self.calls = []

    def list_clip_jobs(self, **kw):
        self.calls.append(("list_clip_jobs", kw))
        return {"clip_jobs": [{"id": "j1", "kind": "produce", "status": "done"}], "count": 1}

    def list_jobs(self, **kw):
        self.calls.append(("list_jobs", kw))
        return {"jobs": [{"id": "src1", "title": "A talk", "status": "done"}], "count": 1}

    def render_pipeline(self, source_id, **kw):
        self.calls.append(("render_pipeline", {"source_id": source_id, **kw}))
        return {"id": "cj9", "kind": "pipeline", "clip_id": "c9", "status": "queued"}


def test_loop_runs_a_read_tool_then_answers():
    c = _FakeClient()
    out = agent.run_agent(
        "what's in the render queue?",
        client=c,
        provider=_provider(
            json.dumps({"tool": "list_clip_jobs", "args": {"limit": 10}}),
            json.dumps({"final": {"reply": "There is 1 job (produce) and it's done."}}),
        ),
        elapsed=iter([0.0, 5.0, 5.0]).__next__,   # deterministic trace timing
    )
    assert out["action"] == "reply"
    assert "1 job" in out["reply"]
    # the read tool actually ran against the client, and shows in the trace
    assert ("list_clip_jobs", {"kind": "", "status": "", "limit": 10}) in c.calls
    assert [t["name"] for t in out["tools"]] == ["list_clip_jobs"]
    assert out["tools"][0]["ok"] is True
    assert out["jobs"] == []          # a read tool starts no job


def test_loop_rejects_a_write_instead_of_starting_a_job():
    c = _FakeClient()
    out = agent.run_agent(
        "make a clip of the intro",
        client=c,
        provider=_provider(json.dumps({
            "tool": "make_clips",
            "args": {"source_id": "src1", "start": 0, "end": 18},
        })),
    )
    assert out == {
        "error": "agent_mutation_disabled",
        "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
    }
    assert c.calls == []


def test_loop_clarify_carries_kind_and_options():
    out = agent.run_agent(
        "clip it",
        client=_FakeClient(),
        provider=_provider(json.dumps({"clarify": {"question": "Which moment?", "options": ["intro", "outro"], "kind": "enum"}})),
    )
    assert out["action"] == "clarify"
    assert out["question"] == "Which moment?" and out["options"] == ["intro", "outro"]
    assert out["kind"] == "enum"


def test_loop_recovers_from_an_unknown_tool():
    c = _FakeClient()
    out = agent.run_agent(
        "do the thing",
        client=c,
        provider=_provider(
            json.dumps({"tool": "frobnicate", "args": {}}),       # hallucinated → loop tells the model
            json.dumps({"tool": "list_jobs", "args": {}}),         # recovers with a real tool
            json.dumps({"final": {"reply": "You have 1 source: A talk."}}),
        ),
        elapsed=iter([0.0, 0.0, 1.0, 1.0]).__next__,
    )
    assert "A talk" in out["reply"]
    assert [t["name"] for t in out["tools"]] == ["list_jobs"]      # the bad tool isn't traced as run


def test_loop_surfaces_a_tool_error_without_crashing():
    class _Boom(_FakeClient):
        def list_jobs(self, **kw):
            raise RuntimeError("engine down")

    out = agent.run_agent(
        "list my sources",
        client=_Boom(),
        provider=_provider(
            json.dumps({"tool": "list_jobs", "args": {}}),
            json.dumps({"final": {"reply": "I couldn't reach the engine."}}),
        ),
        elapsed=iter([0.0, 2.0, 2.0]).__next__,
    )
    assert out["tools"][0]["ok"] is False
    assert out["action"] == "reply"


def test_loop_bad_model_output_becomes_a_reply():
    out = agent.run_agent("hi", client=_FakeClient(), provider=_provider("hello there, not json"))
    assert out["action"] == "reply" and "hello there" in out["reply"]


def test_loop_step_budget_forces_a_final(monkeypatch):
    # A model that ALWAYS calls a tool would loop forever; the budget caps it and asks for a summary.
    c = _FakeClient()
    out = agent.run_agent(
        "loop",
        client=c,
        provider=_provider(
            json.dumps({"tool": "list_jobs", "args": {}}),        # repeats every step (last response repeats)
        ),
        max_steps=3,
        elapsed=(lambda: 0.0),
    )
    # tool ran exactly max_steps times, then a final reply was produced
    assert len([1 for x in c.calls if x[0] == "list_jobs"]) == 3
    assert out["action"] == "reply"


# ---- catalog parity: the agent can do everything the MCP/CLI client can ----

def test_catalog_tools_map_to_real_troveclient_methods():
    from trove_client import TroveClient
    # Every tool's run must reference a real TroveClient operation. We assert the method names the
    # catalog leans on exist, so a renamed/removed client method can't silently break the agent.
    needed = {
        "list_jobs", "get_job", "submit_download", "pause_job", "resume_job", "cancel_job",
        "dismiss_job", "storage_info", "list_transcripts", "get_transcript_status", "transcribe",
        "cancel_transcribe", "search_transcripts", "find_moments", "rank_candidates", "cut_clip",
        "reframe_clip", "caption_clip", "render_clip", "render_pipeline", "list_clip_jobs",
        "get_clip_job", "cancel_clip_job", "dismiss_clip_job", "produce", "list_recipes",
        "get_recipe", "create_recipe", "update_recipe", "delete_recipe", "list_watches", "get_watch",
        "create_watch", "update_watch", "delete_watch", "scan_watch", "list_brand_kits",
        "create_brand_kit", "update_brand_kit", "delete_brand_kit", "list_models", "install_model",
        "set_active_model", "remove_model", "capabilities",
        "get_settings", "update_settings", "edit_word", "dismiss_transcribe",
        "source_energy", "source_scenes", "source_filmstrip",
    }
    for m in needed:
        assert callable(getattr(TroveClient, m, None)), f"TroveClient.{m} missing — agent catalog would break"
    assert agent_tools.JOB_STARTING <= set(agent_tools.CATALOG)            # job-starting set is valid
    assert agent_tools.catalog_prompt().count("\n") >= 15                  # full read-only inspection surface


# ---- Phase 0 read-only classification + defense-in-depth write fuse ----

READ_ONLY_TOOL_NAMES = {
    "capabilities", "get_clip_job", "get_job", "get_recipe", "get_settings",
    "get_transcript_status", "get_watch", "list_brand_kits", "list_clip_jobs",
    "list_jobs", "list_models", "list_recipes", "list_transcripts", "list_watches",
    "rank_candidates", "search_transcripts", "source_energy", "source_scenes",
    "storage_info",
}


def test_phase_zero_catalog_is_fully_classified_and_prompt_is_read_only():
    assert set(agent_tools.READ_ONLY_TOOLS) == READ_ONLY_TOOL_NAMES
    assert set(agent_tools.CATALOG) == (
        set(agent_tools.READ_ONLY_TOOLS)
        | {name for name, tool in agent_tools.CATALOG.items() if tool.writes}
    )
    prompt = agent_tools.catalog_prompt()
    for name, tool in agent_tools.CATALOG.items():
        assert (name in agent_tools.READ_ONLY_TOOLS) is (tool.writes is False)
        assert (f"- {name}(" in prompt) is (name in agent_tools.READ_ONLY_TOOLS)


def test_phase_zero_source_energy_agent_read_disables_durable_cache():
    calls = []

    class Client:
        def source_energy(self, source_id, **kwargs):
            calls.append((source_id, kwargs))
            return {"bars": [0.5], "buckets": 96}

    out = agent.run_agent(
        "inspect source energy",
        client=Client(),
        provider=_provider(
            json.dumps({"tool": "source_energy", "args": {"source_id": "src1"}}),
            json.dumps({"final": {"reply": "done"}}),
        ),
    )

    assert out["reply"] == "done"
    assert calls == [
        ("src1", {"start": None, "end": None, "use_cache": False})
    ]


@pytest.mark.parametrize(
    "tool_name",
    sorted(set(agent_tools.CATALOG) - READ_ONLY_TOOL_NAMES),
)
def test_phase_zero_rejects_every_mutating_tool_without_touching_the_client(tool_name):
    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"mutating client method was accessed: {name}")

    out = agent.run_agent(
        "do it",
        client=ExplodingClient(),
        provider=_provider(json.dumps({"tool": tool_name, "args": {}})),
        confirmed_tool=tool_name,
    )
    assert out == {
        "error": "agent_mutation_disabled",
        "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
    }


# ---- LLM retry + partial-result degradation ----

def test_transient_llm_failure_is_retried(monkeypatch):
    attempts = []
    script = ['{"final":{"reply":"ok"}}']

    def flaky(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("codex bridge hiccup")
        return script.pop(0)

    monkeypatch.setattr(agent.llm, "complete", flaky)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    out = agent.run_agent("hi", client=object())
    assert out["action"] == "reply" and out["reply"] == "ok"
    assert len(attempts) == 2


def test_agent_threads_explicit_policy_and_live_privacy_state_to_completion(monkeypatch):
    policy = NetworkPolicy()
    state = lambda: {
        "offline": False,
        "reasoning_provider": "codex",
        "reasoning_egress_consent": True,
    }
    captured = {}

    def fake_complete(*args, **kwargs):
        captured.update(kwargs)
        return '{"final":{"reply":"ok"}}'

    monkeypatch.setattr(agent.llm, "complete", fake_complete)
    out = agent.run_agent(
        "hi",
        client=object(),
        provider=_provider('{"final":{"reply":"unused"}}'),
        network_policy=policy,
        privacy_state=state,
    )

    assert out["reply"] == "ok"
    assert captured["network_policy"] is policy
    assert captured["privacy_state"] is state


def test_mid_loop_llm_death_finishes_with_partial_results(monkeypatch):
    class FakeClient:
        def list_jobs(self, **kw):
            return {"jobs": []}

    state = {"n": 0}

    def dying(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return '{"tool":"list_jobs","args":{}}'
        raise RuntimeError("bridge died")

    monkeypatch.setattr(agent.llm, "complete", dying)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    out = agent.run_agent("what's downloading?", client=FakeClient())
    assert out["action"] == "reply"
    assert out["tools"] and out["tools"][0]["name"] == "list_jobs"   # partial trace kept


def test_llm_dead_before_any_tool_reraises_for_the_route(monkeypatch):
    def dead(*a, **kw):
        raise RuntimeError("codex not working at all")

    monkeypatch.setattr(agent.llm, "complete", dead)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        agent.run_agent("hi", client=object())


def test_offline_error_propagates_immediately_without_retry(monkeypatch):
    attempts = []

    def offline(*a, **kw):
        attempts.append(1)
        raise agent.llm.OfflineError("offline mode on")

    monkeypatch.setattr(agent.llm, "complete", offline)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    with pytest.raises(agent.llm.OfflineError):
        agent.run_agent("hi", client=object())
    assert len(attempts) == 1     # policy, not weather — never retried
