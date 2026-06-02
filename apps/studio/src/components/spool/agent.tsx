"use client";

import { useEffect, useRef, useState } from "react";
import { useSpool, type AgentMessage } from "./context";
import { Btn, Icon, Progress, Thumb } from "@spool/ui";

/* 1:1 port of the demo's agent.jsx — AgentPanel, ElicitationCard, ToolTrace — wired to the
 * live agent loop: messages come from real `/agent` turns, elicitation = the agent's
 * `clarify` question, and "Make clips" runs real render pipelines. */

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

function ElicitationCard({ msg, onAnswer }: { msg: AgentMessage; onAnswer: (msg: AgentMessage, answer: unknown) => void }) {
  const ctx = useSpool();
  const multi = (msg.options ?? []) as { id: string; title: string; sub?: string; score?: number; def?: boolean }[];
  const [picked, setPicked] = useState<string[]>(msg.kind === "multiselect" ? multi.filter((o) => o.def).map((o) => o.id) : []);
  const answered = msg.answered;
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const enumOpts = (msg.options ?? []) as string[];

  return (
    <div className="elicit">
      <div className="ehead"><Icon name="alert" size={15} style={{ color: "var(--warn)" }} /><span className="tag">{msg.tag || "agent needs you"}</span></div>
      <div className="ebody">
        <div style={{ fontSize: 13.5, fontWeight: 600 }}>{msg.q}</div>
        {msg.kind === "multiselect" && multi.map((o) => (
          <div key={o.id} className={"pickrow" + (picked.includes(o.id) ? " sel" : "")} onClick={() => !answered && toggle(o.id)}>
            <div className="checkbox">{picked.includes(o.id) && <Icon name="check" size={12} />}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row" style={{ gap: 8 }}><span style={{ fontWeight: 600, fontSize: 13 }}>{o.title}</span><span className="spacer" />{o.score != null && <span className="mono" style={{ fontSize: 10.5, color: "var(--text-dim)" }}>★ {o.score}</span>}</div>
              <div style={{ fontSize: 11.5, color: "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.sub}</div>
            </div>
          </div>
        ))}
        {msg.kind === "enum" && (
          <div className="kbar">{enumOpts.map((o) => (
            <button key={o} className={"chip" + (answered && msg.answer === o ? " solid" : "")} style={{ cursor: answered ? "default" : "pointer", height: 34, padding: "0 13px" }} disabled={answered} onClick={() => onAnswer(msg, o)}>{o}</button>
          ))}</div>
        )}
        {msg.kind === "roi" && (
          <div className="row" style={{ gap: 12 }}>
            <div style={{ width: 130, aspectRatio: "16/9", borderRadius: 8, overflow: "hidden", position: "relative", flex: "none" }}>
              <Thumb seed="reframe" kind="" label={false} />
              <div style={{ position: "absolute", left: "6%", top: "18%", width: "40%", height: "64%", border: "2px solid var(--roi-l)", borderRadius: 4 }} />
              <div style={{ position: "absolute", left: "54%", top: "16%", width: "40%", height: "66%", border: "2px solid var(--roi-r)", borderRadius: 4 }} />
            </div>
            <div style={{ fontSize: 12.5, color: "var(--text-dim)" }}>Detected 2 speakers. <span style={{ color: "var(--roi-l)" }}>Cyan</span> = left, <span style={{ color: "var(--roi-r)" }}>magenta</span> = right. Adjust the boxes?</div>
          </div>
        )}
      </div>
      {!answered ? (
        <div className="efoot">
          {msg.kind === "multiselect" && <><Btn variant="primary" size="sm" icon="check" onClick={() => onAnswer(msg, picked)} disabled={picked.length === 0}>Use {picked.length} clip{picked.length !== 1 ? "s" : ""}</Btn><Btn variant="ghost" size="sm" onClick={() => onAnswer(msg, [])}>None</Btn></>}
          {msg.kind === "roi" && <><Btn variant="primary" size="sm" icon="check" onClick={() => onAnswer(msg, "ok")}>Looks right</Btn><Btn variant="ghost" size="sm" icon="crop" onClick={() => { ctx.nav("reframe"); onAnswer(msg, "edit"); }}>Adjust →</Btn></>}
          {msg.kind === "confirm" && <><Btn variant="primary" size="sm" icon="check" onClick={() => onAnswer(msg, "yes")}>{msg.yes || "Confirm"}</Btn><Btn variant="ghost" size="sm" onClick={() => onAnswer(msg, "no")}>Cancel</Btn></>}
        </div>
      ) : (
        <div className="efoot" style={{ color: "var(--ok)", fontSize: 12, fontWeight: 600 }}><Icon name="check" size={14} /> Answered</div>
      )}
    </div>
  );
}

export function AgentPanel() {
  const ctx = useSpool();
  const [text, setText] = useState("");
  const [showSlash, setShowSlash] = useState(false);
  const streamRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight; }, [ctx.agentMessages]);

  const send = () => { if (!text.trim()) return; ctx.askAgent(text.trim()); setText(""); setShowSlash(false); };
  const slashCmds: [string, string][] = [["/make_shorts", "make 3 shorts from the latest source"], ["/clip_from_url", "download a URL and clip it"], ["/tighten", "tighten the current clip"]];

  return (
    <div className={"agent" + (ctx.agentOpen ? "" : " hidden")}>
      <div className="agent-head">
        <span className="dotpulse" />
        <b style={{ fontSize: 13.5 }}>Agent</b>
        {ctx.working && <span className="chip info" style={{ marginLeft: 4 }}><Icon name="spinner" size={12} style={{ animation: "spin 1s linear infinite" }} />working</span>}
        <span className="spacer" />
        <button className="iconbtn" title="Undo last agent action" onClick={() => ctx.pushToast({ icon: "undo", tone: "info", title: "Reverted last agent action" })}><Icon name="undo" size={15} /></button>
        <button className="iconbtn" onClick={ctx.toggleAgent}><Icon name="x" size={16} /></button>
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
          if (m.role === "elicit") return <div key={i} className="msg"><ElicitationCard msg={m} onAnswer={ctx.answerElicit} /></div>;
          return <div key={i} className="msg"><div className="who"><Icon name="sparkles" size={12} style={{ color: "var(--accent)" }} />spool</div><div className="bubble">{m.text}</div></div>;
        })}
      </div>

      <div className="agent-foot">
        {showSlash && (
          <div className="card" style={{ padding: 6, marginBottom: 8 }}>
            {slashCmds.map(([c, d]) => <div key={c} className="pitem" style={{ padding: "7px 9px" }} onClick={() => { setText(c + " "); setShowSlash(false); }}><span className="mono" style={{ color: "var(--accent)", fontSize: 12.5 }}>{c}</span><span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{d}</span></div>)}
          </div>
        )}
        <div className="agent-input">
          <textarea rows={1} placeholder="Ask the agent, or type / for commands…" value={text}
            onChange={(e) => { setText(e.target.value); setShowSlash(e.target.value.startsWith("/") && e.target.value.length < 2); }}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <div className="row" style={{ gap: 6 }}>
            <button className="iconbtn" style={{ width: 28, height: 28 }} onClick={() => setShowSlash((s) => !s)}><Icon name="slash" size={14} /></button>
            <span className="spacer" />
            <button className="iconbtn" style={{ width: 30, height: 30, background: "var(--accent)", color: "var(--accent-ink)" }} onClick={send}><Icon name="arrowR" size={16} /></button>
          </div>
        </div>
      </div>
    </div>
  );
}
