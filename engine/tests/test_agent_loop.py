"""Tests for clip.agent.run_agent — the bounded ReAct tool-loop (the in-app agent's real brain).

The LLM is mocked (a scripted CallableProvider, no codex) and the TroveClient is a tiny fake, so
the loop's mechanics — tool dispatch, observation feedback, trace, job collection, clarify, error
recovery, step budget — are tested without a server or a model.
"""
from __future__ import annotations

import json

from clip import agent, agent_tools, llm


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


def test_loop_collects_started_jobs_and_uses_review_gate():
    c = _FakeClient()
    out = agent.run_agent(
        "make a clip of the intro",
        client=c,
        provider=_provider(
            json.dumps({"tool": "make_clips", "args": {"source_id": "src1", "start": 0, "end": 18}}),
            json.dumps({"final": {"reply": "Cut + reframed — it's in your review queue."}}),
        ),
        elapsed=iter([0.0, 1.0, 1.0]).__next__,
    )
    # make_clips → render_pipeline with stop_after=reframe (the honest review gate, no export)
    call = next(c for c in c.calls if c[0] == "render_pipeline")
    assert call[1]["stop_after"] == "reframe"
    assert out["jobs"] == [{"id": "cj9", "kind": "pipeline", "clip_id": "c9", "status": "queued"}]


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
    assert agent_tools.catalog_prompt().count("\n") >= 40                  # full surface, not a toy


# ---- confirmation gate: exports + destructive config must not run un-confirmed ----
# The plan is steered by an UNTRUSTED transcript (whisper of arbitrary media), so a
# prompt-injection payload must never reach a delete/export tool without a human go-ahead.

class _DeleteClient(_FakeClient):
    """Records delete_recipe calls so we can assert NOTHING ran behind the gate."""

    def delete_recipe(self, rid):
        self.calls.append(("delete_recipe", rid))
        return {"ok": True}


def test_gated_tool_returns_confirm_instead_of_running():
    c = _DeleteClient()
    out = agent.run_agent(
        "delete my recipe",
        client=c,
        provider=_provider(json.dumps({"tool": "delete_recipe", "args": {"recipe_id": "r1"}})),
    )
    assert out["action"] == "confirm"
    assert out["pending"] == {"tool": "delete_recipe", "args": {"recipe_id": "r1"}}
    assert out["kind"] == "confirm"
    assert [x for x in c.calls if x[0] == "delete_recipe"] == []     # NOTHING ran


def test_confirmed_tool_runs_once():
    c = _DeleteClient()
    out = agent.run_agent(
        "delete my recipe",
        client=c,
        provider=_provider(
            json.dumps({"tool": "delete_recipe", "args": {"recipe_id": "r1"}}),
            json.dumps({"final": {"reply": "deleted"}}),
        ),
        confirmed_tool="delete_recipe",
        elapsed=iter([0.0, 1.0, 1.0]).__next__,
    )
    assert out["action"] == "reply"
    assert [x for x in c.calls if x[0] == "delete_recipe"] == [("delete_recipe", "r1")]


def test_confirmation_is_single_use():
    # One confirmation buys exactly ONE call; a model that tries a SECOND delete is re-gated.
    c = _DeleteClient()
    out = agent.run_agent(
        "delete my recipes",
        client=c,
        provider=_provider(
            json.dumps({"tool": "delete_recipe", "args": {"recipe_id": "r1"}}),
            json.dumps({"tool": "delete_recipe", "args": {"recipe_id": "r2"}}),
        ),
        confirmed_tool="delete_recipe",
        elapsed=iter([0.0, 1.0, 1.0]).__next__,
    )
    assert [x for x in c.calls if x[0] == "delete_recipe"] == [("delete_recipe", "r1")]  # first ran
    assert out["action"] == "confirm"                                # second re-gated
    assert out["pending"]["args"] == {"recipe_id": "r2"}


def test_confirm_required_covers_exports_and_destructive_config():
    assert {"render_clip", "render_pipeline", "delete_recipe", "delete_watch",
            "delete_brand_kit", "remove_model", "update_settings"} <= set(agent_tools.CONFIRM_REQUIRED)
    # make_clips lands in the review queue (writes, NOT exports) → must stay UN-gated.
    assert "make_clips" not in agent_tools.CONFIRM_REQUIRED
