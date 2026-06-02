"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { SettingCard, Row } from "@/components/spool/panels";
import { Icon, Switch } from "@spool/ui";

/* S14 Settings — 1:1 port of the demo (07), but every value is REAL and read-only: nothing
 * here is a fake-interactive control. Live facts come from /doctor (encoder, GPU, disk, tool
 * versions) + the locked config (codex provider, MCP tool list). Writing config (model switch,
 * concurrency, MCP transport, per-call prompts) is the Phase-2 settings surface — shown as
 * read-only "current" values, never a knob that does nothing. */

export default function SettingsScreen() {
  const ctx = useSpool();
  const doctor = useEngineQuery((c) => c.doctor());
  const [sec, setSec] = useState("Models");
  const sections: [string, string][] = [["General", "settings"], ["Models", "cpu"], ["Hardware", "drive"], ["Integrations", "link"], ["MCP server", "terminal"], ["Privacy", "shield"], ["Storage", "folder"], ["About", "help"]];

  const tools = doctor.data?.tools ?? {};
  const machine = (doctor.data?.machine ?? {}) as { free_disk_gb?: number; gpu?: string; cpu_cores?: number };
  const encoders = doctor.data?.encoders ?? [];
  const encoder = encoders.some((e) => e.includes("videotoolbox")) ? "VideoToolbox" : encoders.some((e) => e.includes("nvenc")) ? "NVENC" : encoders[0] || "x264";
  const ver = (k: string) => (tools[k]?.version || "").split(/[-+ ]/)[0] || "—";
  const mono = (t: string, color = "var(--text-dim)") => <span className="mono" style={{ fontSize: 12, color }}>{t}</span>;
  const P2 = <span className="chip warn">Phase 2</span>;

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
              <SettingCard title="Transcription">
                <Row l="Engine" r={mono(`whisper.cpp ${ver("whisper_cpp")} · on-device`)} />
                <Row l="Model management" r={P2} sub="Choosing + downloading whisper models from the UI lands in Phase 2 (the engine loads its configured model today)." />
              </SettingCard>
              <SettingCard title="Moment-finding LLM">
                <Row l="Provider" r={mono("Codex CLI bridge")} sub="Your ChatGPT/Codex subscription — no API key, no GPU (SPOOL_LLM_PROVIDER)." />
                <Row l="Egress" r={mono("transcript text only", "var(--ok)")} sub="Media never leaves your machine; offline mode disables the bridge." />
              </SettingCard>
            </div>
          )}
          {sec === "MCP server" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="MCP server">
                <Row l="Transport" r={mono("stdio")} sub="Claude Desktop / Code drive the same engine + queue as the UI." />
                <Row l="Config-from-UI" r={P2} sub="Toggling the server, ports + auth tokens move into the settings store in Phase 2." />
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
                <Row l="Offline mode" r={<Switch on={ctx.offline} onClick={ctx.toggleOffline} />} sub="Blocks network calls except explicit downloads/publishes (SPOOL_OFFLINE)." />
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
              <Row l="Encoder" r={mono(encoder)} sub={`auto-detected: ${encoders.join(", ") || "probing…"}`} />
              <Row l="GPU" r={mono(machine.gpu ?? "—")} />
              <Row l="CPU cores" r={mono(String(machine.cpu_cores ?? "—"))} />
              <Row l="Concurrency & mode" r={P2} sub="Tuning render concurrency + fast/quality mode from the UI is Phase 2." />
            </SettingCard>
          )}
          {sec === "Storage" && (
            <SettingCard title="Storage">
              <Row l="Library root" r={mono("~/Spool")} />
              <Row l="Free disk" r={mono(`${machine.free_disk_gb ?? "—"} GB`)} />
              <Row l="Renders" r={mono("engine/downloads/clips/<clip>/renders/")} sub="Each clip's rendered .mp4 (downloadable from the Editor → Export tab)." />
            </SettingCard>
          )}
          {["General", "Integrations", "About"].includes(sec) && (
            <SettingCard title={sec}>
              <div style={{ color: "var(--text-faint)", fontSize: 13.5, padding: "10px 0", lineHeight: 1.6 }}>{sec === "General" ? "Theme, language and output defaults move into the settings store in Phase 2." : sec === "Integrations" ? "yt-dlp cookies and publish accounts arrive with Publish (Phase 4)." : "Spool — local-first clip studio, built on the open-source trove + clipify foundation (credited in the README)."}</div>
              {sec === "About" && <button className="btn ghost sm" onClick={() => ctx.nav("onboarding")}><Icon name="scan" size={15} /> Re-run Dependency Doctor</button>}
            </SettingCard>
          )}
        </div>
      </div>
    </div>
  );
}
