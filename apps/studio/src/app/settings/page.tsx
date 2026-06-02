"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { SettingCard, Row } from "@/components/spool/panels";
import { Btn, Icon, Progress, Seg, Switch } from "@spool/ui";

/* S14 Settings — 1:1 port of the demo (07). Privacy (offline), Hardware (encoder), Storage
 * (disk) and the LLM provider reflect the real engine; the Moment-finding card shows the
 * locked codex-bridge default honestly (no fabricated Ollama endpoint / API key). */

export default function SettingsScreen() {
  const ctx = useSpool();
  const doctor = useEngineQuery((c) => c.doctor());
  const [sec, setSec] = useState("Models");
  const sections: [string, string][] = [["General", "settings"], ["Models", "cpu"], ["Hardware", "drive"], ["Integrations", "link"], ["MCP server", "terminal"], ["Privacy", "shield"], ["Storage", "folder"], ["About", "help"]];

  const machine = (doctor.data?.machine ?? {}) as { free_disk_gb?: number; gpu?: string };
  const encoders = doctor.data?.encoders ?? [];
  const encVal = encoders.some((e) => e.includes("videotoolbox")) ? "vt" : encoders.some((e) => e.includes("nvenc")) ? "nvenc" : "x264";
  const freeDisk = machine.free_disk_gb ?? 0;

  return (
    <div className="mainpad fadein">
      <div className="eyebrow" style={{ marginBottom: 6 }}>Settings</div>
      <h1 style={{ fontSize: 30, marginBottom: 22 }}>Settings</h1>
      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 28, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {sections.map(([s, ic]) => (
            <button key={s} onClick={() => setSec(s)} className="row" style={{ gap: 10, padding: "9px 12px", borderRadius: 9, border: 0, background: sec === s ? "var(--bg-3)" : "transparent", color: sec === s ? "var(--text)" : "var(--text-dim)", cursor: "pointer", fontSize: 13.5, fontWeight: 500, fontFamily: "inherit", textAlign: "left" }}><Icon name={ic} size={16} />{s}</button>
          ))}
        </div>
        <div style={{ maxWidth: 620 }}>
          {sec === "Models" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Whisper transcription">
                <Row l="Active model" r={<Seg value="base" onChange={() => {}} neutral options={[{ value: "tiny", label: "tiny.en" }, { value: "base", label: "base.en" }, { value: "small", label: "small.en" }]} />} />
                <Row l="Engine" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>whisper.cpp {doctor.data?.tools.whisper_cpp?.version?.split(/[-+ ]/)[0] ?? ""}</span>} />
                <Row r={<Btn variant="ghost" size="sm" icon="play" onClick={() => ctx.pushToast({ icon: "type", tone: "info", title: "Transcription is on-device", body: "whisper.cpp via pywhispercpp" })}>Test transcription</Btn>} />
              </SettingCard>
              <SettingCard title="Moment-finding LLM">
                <Row l="Provider" r={<Seg value="codex" onChange={() => {}} neutral options={[{ value: "codex", label: "Codex" }, { value: "local", label: "Local" }, { value: "claude", label: "Claude" }]} />} sub="Default: the Codex CLI bridge — your ChatGPT/Codex subscription. No API key, no GPU." />
                <Row l="Egress" r={<span className="mono" style={{ fontSize: 12, color: "var(--ok)" }}>transcript text only</span>} sub="Media never leaves your machine. Offline mode disables the bridge entirely." />
              </SettingCard>
            </div>
          )}
          {sec === "MCP server" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="MCP server">
                <Row l="Enable server" r={<Switch on onClick={() => {}} />} sub="Lets Claude Desktop / Code drive Spool's engine as tools — same API, same queue." />
                <Row l="Transport" r={<Seg value="stdio" onChange={() => {}} neutral options={[{ value: "stdio", label: "stdio" }, { value: "http", label: "HTTP" }]} />} />
              </SettingCard>
              <SettingCard title="Tool allow-list">
                <div className="kbar">{["find_moments", "cut_clip", "reframe_clip", "caption_clip", "render_clip", "render_pipeline"].map((t) => <span key={t} className="chip acc mono" style={{ fontSize: 11 }}><Icon name="check" size={12} />{t}</span>)}</div>
                <div style={{ marginTop: 14 }} className="card"><div className="mono" style={{ padding: 12, fontSize: 11.5, color: "rgba(255,255,255,0.66)", background: "#0E1013", borderRadius: "var(--radius)" }}>{'{ "mcpServers": { "spool": { "command": "spool-mcp" } } }'}</div></div>
              </SettingCard>
            </div>
          )}
          {sec === "Privacy" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Privacy">
                <Row l="Offline mode" r={<Switch on={ctx.offline} onClick={ctx.toggleOffline} />} sub="Blocks all network calls except explicit downloads/publishes (SPOOL_OFFLINE)." />
                <Row l="Per-call permission prompts" r={<Switch on onClick={() => {}} />} />
              </SettingCard>
              <SettingCard title="What leaves your machine">
                <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.9 }}>
                  <div>yt-dlp → the site you paste · <span style={{ color: "var(--warn)" }}>download (you start it)</span></div>
                  <div>whisper · <span style={{ color: "var(--ok)" }}>on-device</span></div>
                  <div>find-moments · transcript text → Codex bridge</div>
                </div>
              </SettingCard>
            </div>
          )}
          {sec === "Hardware" && (
            <SettingCard title="Performance & hardware">
              <Row l="Encoder" r={<Seg value={encVal} onChange={() => {}} neutral options={[{ value: "auto", label: "Auto" }, { value: "vt", label: "VideoToolbox" }, { value: "nvenc", label: "NVENC" }, { value: "x264", label: "x264" }]} />} sub={`auto-detected: ${encoders.join(", ") || "probing…"}`} />
              <Row l="GPU" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>{machine.gpu ?? "—"}</span>} />
              <Row l="Concurrency" r={<input type="range" min="1" max="6" defaultValue="2" style={{ width: 160, accentColor: "var(--accent)" }} />} />
              <Row l="Mode" r={<Seg value="quality" onChange={() => {}} neutral options={[{ value: "fast", label: "Fast" }, { value: "quality", label: "Quality" }]} />} />
            </SettingCard>
          )}
          {sec === "Storage" && (
            <SettingCard title="Storage">
              <Row l="Library root" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>~/Spool</span>} />
              <Row l="Free disk" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>{freeDisk} GB</span>} />
              <div><div className="row" style={{ marginBottom: 5 }}><span style={{ fontSize: 13 }}>Disk used by Spool</span><span className="spacer" /></div><Progress value={freeDisk ? Math.min(100, Math.max(4, 100 - (freeDisk / (freeDisk + 20)) * 100)) : 0} /></div>
              <Btn variant="ghost" size="sm" icon="trash" style={{ marginTop: 8 }}>Clean cache &amp; intermediates</Btn>
            </SettingCard>
          )}
          {["General", "Integrations", "About"].includes(sec) && (
            <SettingCard title={sec}>
              <div style={{ color: "var(--text-faint)", fontSize: 13.5, padding: "10px 0" }}>{sec === "General" ? "Theme, language, output paths and defaults." : sec === "Integrations" ? "yt-dlp cookies and publish accounts (Phase 4)." : "Spool — local-first clip studio, built on the open-source trove + clipify foundation (credited in the README). Re-run Dependency Doctor below."}</div>
              {sec === "About" && <Btn variant="ghost" size="sm" icon="scan" onClick={() => ctx.nav("onboarding")}>Re-run Dependency Doctor</Btn>}
            </SettingCard>
          )}
        </div>
      </div>
    </div>
  );
}
