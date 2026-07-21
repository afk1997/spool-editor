"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { MediaCard, ClipCard } from "@/components/spool/cards";
import { Btn, Icon, Progress } from "@spool/ui";

function isHttpUrlBatch(text: string): boolean {
  const tokens = text.split(/\s+/).filter(Boolean);
  return tokens.length > 0 && tokens.every((token) => {
    try {
      const url = new URL(token);
      return url.protocol === "http:" || url.protocol === "https:";
    } catch {
      return false;
    }
  });
}

/* HomeScreen — 1:1 port of the demo (03), wired to live data via useSpool. */
export default function Home() {
  const ctx = useSpool();
  const router = useRouter();
  const [prompt, setPrompt] = useState("");
  const recent = ctx.sources.slice(0, 4);
  const recentClips = ctx.clips.filter((c) => c.status === "ready").slice(0, 5);
  const active = ctx.jobs.filter((j) => j.status === "running");
  const submit = () => { const text = prompt.trim(); if (!text) return; if (isHttpUrlBatch(text)) router.push("/import?url=" + encodeURIComponent(text)); else { if (ctx.working) return; ctx.askAgent(text); ctx.openAgent(); } setPrompt(""); };
  // If the box holds a URL, carry it to /import pre-filled; otherwise just open Import.
  const goImport = () => { const t = prompt.trim(); if (isHttpUrlBatch(t)) { router.push("/import?url=" + encodeURIComponent(t)); setPrompt(""); } else ctx.nav("import"); };

  return (
    <div className="mainpad fadein">
      <div style={{ marginBottom: 6 }} className="eyebrow">Welcome back</div>
      <h1 style={{ fontSize: 34, marginBottom: 24 }}>Import media or ask Codex a question</h1>

      <div className="panel" style={{ padding: 20, marginBottom: 34, background: "linear-gradient(135deg, var(--bg-1), var(--bg-2))" }}>
        <div className="agent-input" style={{ marginBottom: 16, padding: "12px 14px" }}>
          <div className="row" style={{ gap: 10 }}>
            <Icon name="sparkles" size={18} style={{ color: "var(--accent)", flex: "none" }} />
            <input className="input" style={{ border: 0, background: "transparent", padding: 0, height: 26, fontSize: 15 }}
              placeholder="Paste a URL, or ask Codex a question…"
              value={prompt} disabled={ctx.working} onChange={(e) => setPrompt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
            <Btn variant="primary" size="sm" icon="arrowR" onClick={submit} disabled={ctx.working}>{ctx.working ? "Answering…" : "Ask"}</Btn>
          </div>
        </div>
        <div className="row" style={{ gap: 12 }}>
          <Btn variant="primary" size="lg" icon="import" onClick={goImport}>Import / Paste URL</Btn>
          <Btn variant="ghost" size="lg" icon="film" onClick={() => ctx.nav("library")}>Open library</Btn>
          <div className="spacer" />
          <span className="mono" style={{ color: "var(--text-faint)", fontSize: 11 }}>Codex sees only the message you send here—not local app state</span>
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
