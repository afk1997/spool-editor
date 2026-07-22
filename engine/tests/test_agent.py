"""Tests for clip.agent.plan with a deterministic non-egress provider."""
from __future__ import annotations

import json

import pytest

from clip import agent, llm
from network_policy import NetworkPolicy


def _provider(response, capture=None):
    def fn(prompt, system=None):
        if capture is not None:
            capture["prompt"] = prompt
            capture["system"] = system
        return response
    return llm.CallableProvider(fn, name="fake")


def test_find_moments_action(tmp_path):
    out = agent.plan("find the funny bits", provider=_provider(
        json.dumps({"action": "find_moments", "mode": "funny", "count": 3, "reply": "On it"})))
    assert out == {"action": "find_moments", "mode": "funny", "count": 3, "reply": "On it"}


def test_make_clip_action_normalizes_defaults():
    resp = json.dumps({"action": "make_clip", "reply": "Rendering",
                       "clips": [{"start": 2, "end": 12, "title": "Hook"}]})
    out = agent.plan("clip the intro", provider=_provider(resp))
    assert out["action"] == "make_clip"
    c = out["clips"][0]
    assert c["start"] == 2.0 and c["end"] == 12.0
    assert c["aspect"] == "9:16" and c["mode"] == "pan" and c["style"] == "opus" and c["preset"] == "tiktok"


def test_make_clip_with_no_valid_range_becomes_clarify():
    resp = json.dumps({"action": "make_clip", "clips": [{"start": 9, "end": 3}], "reply": "x"})
    out = agent.plan("clip it", provider=_provider(resp))
    assert out["action"] == "clarify" and out["question"]


def test_clarify_action_carries_options():
    resp = json.dumps({"action": "clarify", "question": "Which aspect?",
                       "options": ["9:16", "1:1"], "reply": "Quick q"})
    out = agent.plan("make a clip", provider=_provider(resp))
    assert out["action"] == "clarify"
    assert out["question"] == "Which aspect?" and out["options"] == ["9:16", "1:1"]


def test_reply_action():
    out = agent.plan("hi", provider=_provider(json.dumps({"action": "reply", "reply": "Hello!"})))
    assert out == {"action": "reply", "reply": "Hello!"}


def test_parses_fenced_json():
    resp = "Sure!\n```json\n" + json.dumps({"action": "reply", "reply": "ok"}) + "\n```"
    assert agent.plan("x", provider=_provider(resp))["reply"] == "ok"


def test_unparseable_falls_back_to_reply():
    out = agent.plan("x", provider=_provider("I think you should clip the funny part."))
    assert out["action"] == "reply" and "funny part" in out["reply"]


def test_transcript_lines_reach_the_model():
    cap = {}
    agent.plan("clip it", transcript_lines=[(0.0, 5.0, "hello world")], provider=_provider(
        json.dumps({"action": "reply", "reply": "ok"}), cap))
    assert "hello world" in cap["prompt"]
    assert "find_moments" in cap["system"] and "make_clip" in cap["system"]


def test_phase_zero_remote_plan_is_stably_unavailable():
    with pytest.raises(llm.RemoteReasoningUnavailableError) as denied:
        agent.plan(
            "x",
            provider=" CoDeX ",
            env={
                "SPOOL_OFFLINE": "1",
                "SPOOL_LLM_PROVIDER": "CoDeX",
                "SPOOL_LLM_EGRESS_CONSENT": "1",
            },
            network_policy=NetworkPolicy(offline=True),
        )

    assert denied.value.error_category == "remote_reasoning_unavailable"
    assert str(denied.value) == (
        "Remote reasoning is unavailable in Phase 0 until a supported zero-tool "
        "transport ships."
    )
