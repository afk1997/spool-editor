"""Natural-language clip assistant — the studio Agent panel's brain (spec §2 agent mode).

Maps a user chat message (+ the source transcript) to ONE structured action via the
pluggable LLM provider (default: the codex bridge), which the engine then executes with
the very same clip tools the UI uses (the golden rule). A ``clarify`` action is the spec's
**elicitation** — surfaced in the studio as an inline card rather than a blocking prompt.

This module only *plans* (LLM call + parse → action dict); the api_v1 ``/agent`` route
*executes* (submits clip jobs via the ClipJobManager). Pure + provider-injectable, so it's
unit-testable with the LLM mocked — no codex needed in tests.
"""
from __future__ import annotations

import json
import re

from . import llm

_ACTIONS = {"find_moments", "make_clip", "clarify", "reply"}

_SYSTEM = (
    "You are Spool's clip assistant. The user turns long videos into short vertical clips. "
    "Reply with EXACTLY ONE action as a single minified JSON object — no prose, no fences:\n"
    '- {"action":"find_moments","mode":"funny|insightful|hot-take|story|how-to|q&a","count":N,"reply":"..."}\n'
    "    — to discover clip-worthy moments in the transcript.\n"
    '- {"action":"make_clip","clips":[{"start":S,"end":E,"aspect":"9:16|16:9|1:1|4:5",'
    '"mode":"pan|split|center","style":"opus|karaoke|minimal",'
    '"preset":"tiktok|reels|shorts|youtube|linkedin|x","title":"..."}],"reply":"..."}\n'
    "    — to render specific moment(s); take start/end from the transcript timestamps.\n"
    '- {"action":"clarify","question":"...","options":["...","..."],"reply":"..."}\n'
    "    — when you need a human decision (which moment? which aspect? caption style?) first.\n"
    '- {"action":"reply","reply":"..."}\n'
    "    — to answer in words only.\n"
    "Always include a short human-facing \"reply\". Defaults: aspect 9:16, mode pan, "
    "style opus, preset tiktok."
)


def plan(
    message: str,
    *,
    transcript_lines: "list[tuple[float, float, str]] | None" = None,
    provider: "str | llm.LLMProvider | None" = None,
    env: dict | None = None,
) -> dict:
    """Decide the next action for a user message. Returns a normalized action dict
    ``{action, reply, ...}``. Propagates :class:`~clip.llm.OfflineError` /
    ``ProviderUnavailableError`` from the provider; never raises on bad model output
    (falls back to a ``reply``)."""
    parts = [f"User: {message.strip()}"]
    if transcript_lines:
        body = "\n".join(f"[{s:.2f}–{e:.2f}] {t}" for s, e, t in transcript_lines)
        parts.append("Transcript (each line is [start–end seconds] text):\n\n" + body)
    prompt = "\n\n".join(parts)
    reply = llm.complete(prompt, system=_SYSTEM, provider=provider, env=env)
    return _normalize(_parse_obj(reply), fallback=reply)


def _parse_obj(text: str) -> dict | None:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    a, b = text.find("{"), text.rfind("}")
    for cand in (text, fenced.group(1).strip() if fenced else None,
                 text[a:b + 1] if a != -1 and b > a else None):
        if not cand:
            continue
        try:
            val = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(val, dict):
            return val
    return None


def _normalize(data: dict | None, *, fallback: str) -> dict:
    if not isinstance(data, dict) or data.get("action") not in _ACTIONS:
        # Unparseable / unknown → treat the raw text as a plain reply (graceful).
        text = (fallback or "").strip()
        return {"action": "reply", "reply": text[:800] or "Sorry — I didn't understand that."}

    action = data["action"]
    reply = str(data.get("reply") or "").strip()
    out: dict = {"action": action, "reply": reply}

    if action == "find_moments":
        out["mode"] = str(data.get("mode") or "funny")
        try:
            out["count"] = max(1, min(25, int(data.get("count", 5))))
        except (TypeError, ValueError):
            out["count"] = 5
        out["reply"] = reply or "Scanning the transcript for moments…"
    elif action == "make_clip":
        clips = []
        for c in data.get("clips") or []:
            if not isinstance(c, dict):
                continue
            try:
                start, end = float(c["start"]), float(c["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start or start < 0:
                continue
            clips.append({
                "start": start, "end": end,
                "aspect": str(c.get("aspect") or "9:16"),
                "mode": str(c.get("mode") or "pan"),
                "style": str(c.get("style") or "opus"),
                "preset": str(c.get("preset") or "tiktok"),
                "title": str(c.get("title") or "").strip(),
            })
        out["clips"] = clips
        if not clips:  # model picked make_clip but gave no valid range → ask instead
            return {"action": "clarify",
                    "reply": reply or "Which moment should I clip?",
                    "question": "Which moment should I clip? Give me a start and end.",
                    "options": []}
        out["reply"] = reply or f"Rendering {len(clips)} clip(s)…"
    elif action == "clarify":
        out["question"] = str(data.get("question") or reply or "Could you clarify?")
        opts = data.get("options") or []
        out["options"] = [str(o) for o in opts if str(o).strip()] if isinstance(opts, list) else []
        out["reply"] = reply or out["question"]
    else:  # reply
        out["reply"] = reply or "Okay."
    return out
