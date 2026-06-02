"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { MediaCard, ClipCard } from "@/components/spool/cards";
import { Btn, Icon, Progress } from "@/components/spool/ui";

/* HomeScreen — 1:1 port of the demo (03), wired to live data via useSpool. */
export default function Home() {
  const ctx = useSpool();
  const [prompt, setPrompt] = useState("");
  const recent = ctx.sources.slice(0, 4);
  const recentClips = ctx.clips.filter((c) => c.status === "ready").slice(0, 5);
  const active = ctx.jobs.filter((j) => j.status === "running");
  const submit = () => { if (!prompt.trim()) return; ctx.askAgent(prompt.trim()); setPrompt(""); ctx.openAgent(); };

  return (
    <div className="mainpad fadein">
      <div style={{ marginBottom: 6 }} className="eyebrow">Welcome back</div>
      <h1 style={{ fontSize: 34, marginBottom: 24 }}>What are we clipping today?</h1>

      <div className="panel" style={{ padding: 20, marginBottom: 34, background: "linear-gradient(135deg, var(--bg-1), var(--bg-2))" }}>
        <div className="agent-input" style={{ marginBottom: 16, padding: "12px 14px" }}>
          <div className="row" style={{ gap: 10 }}>
            <Icon name="sparkles" size={18} style={{ color: "var(--accent)", flex: "none" }} />
            <input className="input" style={{ border: 0, background: "transparent", padding: 0, height: 26, fontSize: 15 }}
              placeholder="Tell the agent what to clip…  e.g. “grab 3 funny moments from my last import”"
              value={prompt} onChange={(e) => setPrompt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
            <Btn variant="primary" size="sm" icon="arrowR" onClick={submit}>Run</Btn>
          </div>
        </div>
        <div className="row" style={{ gap: 12 }}>
          <Btn variant="primary" size="lg" icon="import" onClick={() => ctx.nav("import")}>Import / Paste URL</Btn>
          <Btn variant="ghost" size="lg" icon="scissors" onClick={() => ctx.nav("library")}>Make clips</Btn>
          <div className="spacer" />
          <div className="kbar">
            {ctx.recipes.slice(0, 3).map((r) => (
              <button key={r} className="chip" style={{ cursor: "pointer", height: 30 }} onClick={() => { ctx.askAgent(r); ctx.openAgent(); }}>
                <Icon name="zap" size={13} />{r}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="sectionhead"><h2>Recent projects</h2><span className="sub">{ctx.sources.length} sources</span><span className="spacer" /><button className="btn subtle sm" onClick={() => ctx.nav("library")}>View all <Icon name="arrowR" size={14} /></button></div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 16, marginBottom: 36 }}>
        {recent.length === 0 && <div style={{ color: "var(--text-faint)", fontSize: 13 }}>No sources yet — import a video to begin.</div>}
        {recent.map((s) => <MediaCard key={s.id} s={s} onOpen={() => ctx.nav("project", { id: s.id })} />)}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 340px", gap: 24 }}>
        <div>
          <div className="sectionhead"><h2>Recent clips</h2><span className="spacer" /><button className="btn subtle sm" onClick={() => ctx.nav("clips")}>Library <Icon name="arrowR" size={14} /></button></div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
            {recentClips.length === 0 && <div style={{ color: "var(--text-faint)", fontSize: 13 }}>No clips yet.</div>}
            {recentClips.slice(0, 3).map((c) => <ClipCard key={c.id} c={c} />)}
          </div>
        </div>
        <div>
          <div className="sectionhead"><h2>Queue</h2><span className="spacer" /><button className="btn subtle sm" onClick={() => ctx.nav("queue")}>Open</button></div>
          <div className="panel" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 13 }}>
            {active.length === 0 && <div style={{ color: "var(--text-faint)", fontSize: 13, padding: "14px 4px" }}>No active jobs.</div>}
            {active.map((j) => (
              <div key={j.id} style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                <div className="row" style={{ gap: 8 }}>
                  <Icon name={j.type === "transcribe" ? "type" : "film"} size={14} style={{ color: "var(--accent)" }} />
                  <span style={{ fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.label}</span>
                  <span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>{Math.round(j.prog)}%</span>
                </div>
                <Progress value={j.prog} striped />
                <div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>{j.stage} · ETA {j.eta}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
