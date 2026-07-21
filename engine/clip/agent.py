"""Natural-language clip assistant — the studio Agent panel's brain (spec §2 agent mode).

Maps a user chat message (+ optional transcript text) through the pluggable LLM layer.
Remote providers such as Codex are strictly text-only: they receive no local tool catalog
and can never dispatch or observe a local tool. Explicitly non-egress injected providers
retain the Phase 0 read-only inspection loop over the same API surface the UI uses.

The legacy :func:`plan` helper only parses a single structured action. :func:`run_agent`
owns the text-only remote path and the separately gated local inspection loop. Both are
provider-injectable, so they are unit-testable with the LLM mocked — no Codex required.
"""
from __future__ import annotations

import json
import os  # noqa: E401 — kept here so the import block is contiguous
import re
import subprocess
import time

from network_policy import NetworkPolicy

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
    network_policy: NetworkPolicy | None = None,
    privacy_state: llm.PrivacyState | None = None,
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
    reply = llm.complete(
        prompt,
        system=_SYSTEM,
        provider=provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )
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


# ---------------------------------------------------------------------------
# Tool-using agent for explicitly non-egress providers — a bounded ReAct loop
# ---------------------------------------------------------------------------
# The single-shot `plan` above only knows 4 actions. For an explicitly non-egress provider,
# `run_agent` drives the shared read-only catalog (clip.agent_tools → the same /api/v1 surface
# the UI/MCP/CLI use). Each step emits one tool/clarify/final JSON object; the engine executes
# an allowed read, feeds back a bounded observation, and loops. Egress providers branch away
# before this protocol and never receive the catalog or an observation.

from . import agent_tools  # noqa: E402  (kept next to run_agent for locality)

_MAX_STEPS = 8
_OBS_CHARS = 3200          # truncate each tool observation so the re-sent transcript stays bounded
                           # (big enough for a ~20-item list result — too-tight truncation made the
                           #  model misread a full list as empty)

_LOOP_SYSTEM = (
    "You are Spool's read-only clip inspection agent. Spool turns long videos into short vertical "
    "clips, fully on the user's machine. Your TOOLS can inspect downloads, transcripts, the render "
    "queue, recipes, watches, brand kits, models, storage, and capabilities. Phase 0 does not let "
    "you start, edit, cancel, delete, install, or export anything. If the user asks for a change, "
    "explain that Agent changes are disabled until the approval and undo contract ships.\n\n"
    "Work in steps. Each step, reply with EXACTLY ONE minified JSON object — no prose, no fences:\n"
    '- {"tool":"<name>","args":{...}} — run a tool. I will reply with its JSON result so you can continue.\n'
    '- {"clarify":{"question":"...","options":["..."],"kind":"enum|confirm"}} — ask the user a question when you need a human decision.\n'
    '- {"final":{"reply":"..."}} — stop and answer the user in words (use the tool results you gathered).\n\n'
    "Rules: Prefer READ tools (list_*, get_*, search_*) to answer 'what/which/status' questions — "
    "e.g. the render queue is list_clip_jobs, downloaded videos/sources are list_jobs. REPORT WHAT THE "
    "TOOL ACTUALLY RETURNS — count and name/title/id the items; never claim something is empty when the "
    "result has entries. If the user says 'queue' ambiguously, the render queue is list_clip_jobs and "
    "downloaded videos are list_jobs — check the one they mean (or both). Keep going until you can give "
    "a useful factual answer; don't ask the user for something a read tool can fetch. Always finish "
    "with a {\"final\":...} once you have enough.\n\n"
    "TOOLS:\n" + agent_tools.catalog_prompt()
)

_REMOTE_SYSTEM = (
    "You are Spool's text-only clip assistant. Answer using only the user's message and "
    "any transcript context included in the prompt. You have no access to local files, "
    "sources, queues, watches, models, storage, application state, or local tools. Never "
    "claim that you inspected local state. If the requested fact is not present in the "
    "provided text, explain that you cannot inspect it. Reply in plain text and do not "
    "emit a tool call or a JSON tool request."
)

REMOTE_AGENT_TOOLS_DISABLED_ERROR = {
    "error": "remote_agent_tools_disabled",
    "message": (
        "Remote Agent tools are disabled. Codex receives only your message and "
        "attached transcript text; it cannot inspect local app state."
    ),
}


def _default_agent_provider(
    env: dict | None,
    network_policy: NetworkPolicy | None,
    privacy_state: llm.PrivacyState | None,
):
    """Give the text-only Codex assistant more conversational reasoning effort than
    moment extraction (``SPOOL_AGENT_REASONING``, default ``medium``). For any
    non-Codex configured provider, return None so the normal resolver handles it."""
    e = env if env is not None else os.environ
    name = (e.get("SPOOL_LLM_PROVIDER") or llm.DEFAULT_PROVIDER).lower()
    if name == "codex":
        if network_policy is None:
            raise ValueError("network_policy is required for the Codex agent provider")
        return llm.CodexProvider(
            network_policy=network_policy,
            privacy_state=privacy_state,
            env=e,
            reasoning=(e.get("SPOOL_AGENT_REASONING") or "medium"),
        )
    return None


def _truncate(obj, limit: int = _OBS_CHARS) -> str:
    try:
        s = json.dumps(obj, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + f"… (truncated, {len(s)} chars)"


def _short_arg(args: dict) -> str:
    """A compact one-line arg summary for the tool trace (the studio's ToolTrace 'arg' column)."""
    if not isinstance(args, dict) or not args:
        return ""
    bits = []
    for k, v in list(args.items())[:3]:
        sv = json.dumps(v, default=str) if not isinstance(v, str) else v
        bits.append(f"{k}={sv[:24]}")
    return "· " + " ".join(bits)


def _provider_is_explicitly_non_egress(provider) -> bool:
    """Only the literal singleton False grants access to the local tool loop."""
    try:
        return getattr(provider, "egress", True) is False
    except Exception:
        return False


def _json_requests_tool(value) -> bool:
    if isinstance(value, dict):
        if "tool" in value:
            return True
        return any(_json_requests_tool(item) for item in value.values())
    if isinstance(value, list):
        return any(_json_requests_tool(item) for item in value)
    return False


def _remote_result(raw: str) -> dict:
    parsed = _parse_obj(raw)
    if _json_requests_tool(parsed):
        return dict(REMOTE_AGENT_TOOLS_DISABLED_ERROR)

    if isinstance(parsed, dict) and isinstance(parsed.get("final"), dict):
        reply = str(parsed["final"].get("reply") or "").strip()
        return _finish(reply or "Done.", [], [])
    if isinstance(parsed, dict) and parsed.get("action") == "reply":
        return _finish(str(parsed.get("reply") or "Done.").strip(), [], [])
    if isinstance(parsed, dict) and (
        "clarify" in parsed or parsed.get("action") == "clarify"
    ):
        clarification = (
            parsed.get("clarify")
            if isinstance(parsed.get("clarify"), dict)
            else parsed
        )
        options = clarification.get("options") or []
        question = str(
            clarification.get("question")
            or clarification.get("reply")
            or "Could you clarify?"
        )
        return {
            "action": "clarify",
            "reply": str(clarification.get("reply") or question),
            "question": question,
            "options": (
                [str(option) for option in options if str(option).strip()]
                if isinstance(options, list)
                else []
            ),
            "kind": (
                clarification.get("kind")
                if clarification.get("kind") in ("enum", "confirm", "multiselect")
                else "enum"
            ),
            "tools": [],
            "jobs": [],
        }
    return _finish((raw or "").strip()[:800] or "Done.", [], [])


def _complete_with_retry(
    prompt: str,
    *,
    system,
    provider,
    env,
    network_policy,
    privacy_state,
    attempts: int = 2,
) -> str:
    """llm.complete with one retry for transient bridge failures (codex crash, timeout,
    broken pipe). OfflineError is policy, not weather — re-raised immediately."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return llm.complete(
                prompt,
                system=system,
                provider=provider,
                env=env,
                network_policy=network_policy,
                privacy_state=privacy_state,
            )
        except (llm.OfflineError, llm.ReasoningDisabledError):
            raise
        except (llm.ProviderUnavailableError, RuntimeError, OSError,
                subprocess.TimeoutExpired) as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def run_agent(
    message: str,
    *,
    client,
    transcript_lines: "list[tuple[float, float, str]] | None" = None,
    elapsed=None,
    provider: "str | llm.LLMProvider | None" = None,
    env: dict | None = None,
    network_policy: NetworkPolicy | None = None,
    privacy_state: llm.PrivacyState | None = None,
    max_steps: int = _MAX_STEPS,
    confirmed_tool: "str | None" = None,
) -> dict:
    """Run one text-only completion for an egress provider or a bounded read-only
    ReAct loop for an explicitly non-egress provider. ``client`` is used only by the
    latter. ``elapsed`` is an optional ``() -> float`` ms clock for trace timing.
    ``confirmed_tool`` remains accepted compatibility baggage, but Phase 0 treats it
    as inert: no confirmation can bypass the read-only mutation fuse. Returns
    ``{reply, action, jobs[], tools[], question?, options?, kind?, pending?}``. Propagates
    OfflineError always, and ProviderUnavailableError / RuntimeError when no tools have run yet
    (a setup problem); mid-loop transient failures after at least one tool ran are retried once
    then degraded to a partial-result _finish instead of an unhandled exception."""
    clock = elapsed or (lambda: time.monotonic() * 1000.0)
    if provider is None:                              # default the in-app agent to higher reasoning
        provider = _default_agent_provider(env, network_policy, privacy_state)
    provider = llm.get_provider(
        provider,
        env=env,
        network_policy=network_policy,
        privacy_state=privacy_state,
    )

    transcript = [f"User: {message.strip()}"]
    if transcript_lines:
        body = "\n".join(f"[{s:.2f}–{e:.2f}] {t}" for s, e, t in transcript_lines)
        transcript.append("Transcript context (each line [start–end seconds] text):\n\n" + body)

    if not _provider_is_explicitly_non_egress(provider):
        prompt = "\n\n".join(transcript)
        raw = _complete_with_retry(
            prompt,
            system=_REMOTE_SYSTEM,
            provider=provider,
            env=env,
            network_policy=network_policy,
            privacy_state=privacy_state,
        )
        return _remote_result(raw)

    tools_trace: list[dict] = []
    jobs: list[dict] = []

    for _ in range(max(1, max_steps)):
        prompt = "\n\n".join(transcript) + "\n\nYour next JSON:"
        try:
            raw = _complete_with_retry(
                prompt,
                system=_LOOP_SYSTEM,
                provider=provider,
                env=env,
                network_policy=network_policy,
                privacy_state=privacy_state,
            )
        except (llm.OfflineError, llm.ReasoningDisabledError):
            raise
        except Exception:
            if not tools_trace:
                raise   # nothing ran yet → a setup problem; let the route surface it as 503
            return _finish("The model provider dropped out mid-turn after a retry; "
                           "here's what completed so far.", tools_trace, jobs)
        step = _parse_obj(raw)

        if not isinstance(step, dict):                       # unparseable → treat as the final reply
            return _finish((raw or "").strip()[:800] or "Done.", tools_trace, jobs)

        if "final" in step:
            fin = step["final"] if isinstance(step["final"], dict) else {}
            return _finish(str(fin.get("reply") or step.get("reply") or "Done.").strip(), tools_trace, jobs)

        if "clarify" in step or step.get("action") == "clarify":
            cl = step.get("clarify") if isinstance(step.get("clarify"), dict) else step
            opts = cl.get("options") or []
            return {
                "action": "clarify",
                "reply": str(cl.get("reply") or cl.get("question") or "Could you clarify?"),
                "question": str(cl.get("question") or "Could you clarify?"),
                "options": [str(o) for o in opts if str(o).strip()] if isinstance(opts, list) else [],
                "kind": cl.get("kind") if cl.get("kind") in ("enum", "confirm", "multiselect") else "enum",
                "tools": tools_trace, "jobs": jobs,
            }

        name = step.get("tool")
        tool = agent_tools.CATALOG.get(name)
        if not tool:                                         # hallucinated tool → tell the model, retry
            transcript.append(f"Assistant: {_truncate(step)}")
            transcript.append(f"Tool result: ERROR unknown tool {name!r}. Pick one from the TOOLS list.")
            continue

        if tool.name not in agent_tools.READ_ONLY_TOOLS or tool.writes:
            return dict(agent_tools.MUTATION_DISABLED_ERROR)

        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        t0 = clock()
        try:
            if not _provider_is_explicitly_non_egress(provider):
                return dict(REMOTE_AGENT_TOOLS_DISABLED_ERROR)
            result = tool.run(client, args)
            ok = True
        except Exception as e:                               # surface tool errors to the model, keep going
            result = {"error": type(e).__name__, "message": str(e)[:300]}
            ok = False
        ms = int(max(0.0, clock() - t0))

        tools_trace.append({"name": name, "arg": _short_arg(args), "ms": ms, "ok": ok})
        if ok and name in agent_tools.JOB_STARTING and isinstance(result, dict) and result.get("id"):
            jobs.append({"id": result.get("id"), "kind": result.get("kind") or name,
                         "clip_id": result.get("clip_id"), "status": result.get("status")})

        transcript.append(f"Assistant: {_truncate(step)}")
        transcript.append(f"Tool result ({name}): {_truncate(result)}")

    # Step budget exhausted → ask the model to summarize what it found into a final reply.
    summary_prompt = ("\n\n".join(transcript) +
                      "\n\nStep budget reached. Reply now with {\"final\":{\"reply\":\"...\"}} summarizing for the user.")
    try:
        raw = _complete_with_retry(
            summary_prompt,
            system=_LOOP_SYSTEM,
            provider=provider,
            env=env,
            network_policy=network_policy,
            privacy_state=privacy_state,
        )
    except (llm.OfflineError, llm.ReasoningDisabledError):
        raise
    except Exception:
        return _finish("Step budget reached and the provider dropped out — here's what I gathered.", tools_trace, jobs)
    fin = _parse_obj(raw) or {}
    reply = ""
    if isinstance(fin, dict):
        f = fin.get("final") if isinstance(fin.get("final"), dict) else fin
        reply = str(f.get("reply") or "").strip()
    return _finish(reply or (raw or "").strip()[:800] or "I gathered what I could — ask me to continue.", tools_trace, jobs)


def _finish(reply: str, tools_trace: list, jobs: list) -> dict:
    return {"action": "reply", "reply": reply or "Done.", "tools": tools_trace, "jobs": jobs}
