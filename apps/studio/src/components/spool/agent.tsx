"use client";

import { useEffect, useRef, useState } from "react";
import { useSpool, type AgentMessage } from "./context";
import { Icon, Progress } from "@spool/ui";

/* The Phase 0 agent is an inspection surface. Its prompt can ask about current local state, but
 * it does not advertise recipes, mutation commands, confirmations, or fabricated output. */

function ToolTrace({ tools }: { tools: NonNullable<AgentMessage["tools"]> }) {
  return (
    <details className="trace">
      <summary>
        <Icon name="terminal" size={13} /> {`ran ${tools.length} tool${tools.length > 1 ? "s" : ""} · ${tools.reduce((a, t) => a + (t.ms || 0), 0)}ms`}
        <span className="spacer" />
        <Icon name="chevD" size={13} />
      </summary>
      <div className="tracebody">
        {tools.map((t, i) => (
          <div key={i} className="traceline">
            <Icon name="check" size={12} style={{ color: "var(--ok)" }} />
            <span className="tk">{t.name}</span>
            <span style={{ color: "var(--text-faint)" }}>{t.arg}</span>
            <span className="ms">{t.ms}ms</span>
          </div>
        ))}
      </div>
    </details>
  );
}

function ClarificationCard({ msg, onAnswer }: { msg: AgentMessage; onAnswer: (msg: AgentMessage, answer: unknown) => void }) {
  const choices = (msg.options ?? []).map((option) => typeof option === "string"
    ? { value: option, label: option, description: undefined as string | undefined, selected: msg.answer === option }
    : { value: option.id, label: option.title, description: option.sub, selected: Array.isArray(msg.answer) ? msg.answer.includes(option.id) : msg.answer === option.id });
  const [picked, setPicked] = useState<string[]>(() => (msg.options ?? []).flatMap((option) =>
    typeof option === "string" || !option.def ? [] : [option.id]));
  const multi = msg.kind === "multiselect";

  return (
    <div className="msg"><div className="bubble">
      <div style={{ fontWeight: 600, marginBottom: 8 }}>{msg.q || "What should I inspect?"}</div>
      <div className="kbar">
        {choices.map((choice) => {
          const selected = multi ? picked.includes(choice.value) : choice.selected;
          return (
            <button type="button" key={choice.value} className={"chip" + (selected ? " solid" : "")} aria-pressed={selected} disabled={msg.answered}
              title={choice.description} onClick={() => multi
                ? setPicked((current) => current.includes(choice.value) ? current.filter((value) => value !== choice.value) : [...current, choice.value])
                : onAnswer(msg, choice.value)}>
              {choice.label}
            </button>
          );
        })}
        {multi && (
          <button type="button" className="btn primary sm" disabled={msg.answered || picked.length === 0} onClick={() => onAnswer(msg, picked)}>Answer</button>
        )}
      </div>
    </div></div>
  );
}

export function AgentPanel() {
  const ctx = useSpool();
  const [text, setText] = useState("");
  const streamRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight; }, [ctx.agentMessages]);

  const send = () => { if (ctx.working || !text.trim()) return; ctx.askAgent(text.trim()); setText(""); };

  return (
    <div className={"agent" + (ctx.agentOpen ? "" : " hidden")}>
      <div className="agent-head">
        <span className="dotpulse" />
        <b style={{ fontSize: 13.5 }}>Agent · read-only</b>
        {ctx.working && <span className="chip info" style={{ marginLeft: 4 }}><Icon name="spinner" size={12} style={{ animation: "spin 1s linear infinite" }} />working</span>}
        <span className="spacer" />
        <button className="iconbtn" aria-label="Close agent panel" onClick={ctx.toggleAgent}><Icon name="x" size={16} /></button>
      </div>

      <div className="agent-stream" ref={streamRef}>
        {ctx.agentMessages.map((m, i) => {
          if (m.role === "user") return <div key={i} className="msg user"><div className="bubble">{m.text}</div></div>;
          if (m.role === "trace" && m.tools) return <div key={i} className="msg"><ToolTrace tools={m.tools} /></div>;
          if (m.role === "working") return (
            <div key={i} className="msg"><div className="bubble" style={{ width: "100%" }}>
              <div className="row" style={{ gap: 9, marginBottom: 8, fontSize: 12.5 }}><Icon name={m.icon || "film"} size={14} style={{ color: "var(--accent)" }} />{m.label}<span className="spacer" /><span className="mono" style={{ fontSize: 11 }}>{m.prog}%</span></div>
              <Progress value={m.prog ?? 0} striped={(m.prog ?? 0) < 100} tone={(m.prog ?? 0) >= 100 ? "ok" : ""} />
            </div></div>
          );
          if (m.role === "elicit" && m.confirmFor) return (
            <div key={i} className="msg"><div className="bubble">
              <div style={{ fontWeight: 600, marginBottom: 4 }}>{m.q || "This request needs an action."}</div>
              <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Agent mutations are unavailable in Phase 0. Inspection remains available.</div>
            </div></div>
          );
          if (m.role === "elicit") return <ClarificationCard key={i} msg={m} onAnswer={ctx.answerElicit} />;
          return <div key={i} className="msg"><div className="who"><Icon name="sparkles" size={12} style={{ color: "var(--accent)" }} />spool</div><div className="bubble">{m.text}</div></div>;
        })}
      </div>

      <div className="agent-foot">
        <div className="agent-input">
          <textarea rows={1} placeholder="Ask about sources, clips, transcripts, or queue status…" value={text} disabled={ctx.working}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <div className="row" style={{ gap: 6 }}>
            <span style={{ fontSize: 10.5, color: "var(--text-faint)" }}>Inspection only</span>
            <span className="spacer" />
            <button className="iconbtn" aria-label="Send question" disabled={ctx.working || !text.trim()} style={{ width: 30, height: 30, background: "var(--accent)", color: "var(--accent-ink)" }} onClick={send}><Icon name="arrowR" size={16} /></button>
          </div>
        </div>
      </div>
    </div>
  );
}
