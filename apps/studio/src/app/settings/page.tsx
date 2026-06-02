"use client";

import { useEffect, useRef, useState } from "react";
import type { EngineSettings } from "@spool/api-client";
import { useSpool } from "@/components/spool/context";
import { useEngine, useEngineQuery } from "@/lib/engine-context";
import { SettingCard, Row } from "@/components/spool/panels";
import { Icon, Switch, Seg } from "@spool/ui";

/* S14 Settings — 1:1 port of the demo (07), now with the designed-but-stubbed controls wired
 * to the REAL engine settings store (GET/PATCH /settings) + the models endpoints. Every knob
 * invokes a real path: model switch + fast/quality + default preset are HOT; render
 * concurrency + MCP transport are honestly labelled "applies on restart". Auto-detected or
 * unbacked demo affordances (encoder selector, MCP enable/port/auth, proxy resolution) stay
 * read-only facts — never a control that does nothing. Live facts come from /doctor. */

const PRESETS = ["tiktok", "reels", "shorts", "youtube", "linkedin", "x"];

export default function SettingsScreen() {
  const ctx = useSpool();
  const client = useEngine();
  const doctor = useEngineQuery((c) => c.doctor());
  const settingsQ = useEngineQuery((c) => c.getSettings(), []);
  const modelsQ = useEngineQuery((c) => c.listModels(), []);
  const [sec, setSec] = useState("Models");

  // Local editable copy, seeded once from the server, reconciled from each PATCH response.
  const [s, setS] = useState<EngineSettings | null>(null);
  if (s === null && settingsQ.data) setS(settingsQ.data);

  // Poll the model list while a download is in flight so the progress + installed state update.
  const installing = modelsQ.data?.install_progress?.downloading ? modelsQ.data.install_progress : null;
  useEffect(() => {
    if (!installing) return;
    const t = setInterval(() => modelsQ.reload(), 1500);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!installing]);

  const save = (patch: Partial<EngineSettings>) => {
    setS((cur) => (cur ? { ...cur, ...patch } : cur)); // optimistic
    client.updateSettings(patch)
      .then((next) => setS(next))
      .catch(() => { ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't save setting" }); client.getSettings().then(setS).catch(() => {}); });
  };

  // The concurrency slider streams values while dragging (and on each keyboard arrow); debounce
  // the persist so we PATCH once on settle, not per tick — and so keyboard users still save.
  const concurrencyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onConcurrency = (v: number) => {
    setS((cur) => (cur ? { ...cur, clip_workers: v } : cur));
    if (concurrencyTimer.current) clearTimeout(concurrencyTimer.current);
    concurrencyTimer.current = setTimeout(() => save({ clip_workers: v }), 400);
  };

  const models = modelsQ.data?.models ?? [];
  const installedLabels = models.filter((m) => m.is_installed).map((m) => m.label);
  const pickModel = (name: string) => {
    const m = models.find((x) => x.name === name);
    if (!m || m.is_active) return;
    if (m.is_installed) {
      client.useModel(name)
        .then(() => { modelsQ.reload(); ctx.pushToast({ icon: "check", tone: "ok", title: "Active model set", body: m.label }); })
        .catch(() => ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't switch model" }));
    } else {
      client.installModel(name)
        .then(() => { modelsQ.reload(); ctx.pushToast({ icon: "download", tone: "info", title: `Downloading ${m.label}`, body: `${(m.size_bytes / 1e6).toFixed(0)} MB · becomes active when ready` }); })
        .catch((e: { code?: string }) => ctx.pushToast({ icon: "alert", tone: "warn", title: e?.code === "busy" ? "Another download is running" : "Couldn't start the download" }));
    }
  };

  const tools = doctor.data?.tools ?? {};
  const machine = (doctor.data?.machine ?? {}) as { free_disk_gb?: number; gpu?: string; cpu_cores?: number };
  const encoders = doctor.data?.encoders ?? [];
  const encoder = encoders.some((e) => e.includes("videotoolbox")) ? "VideoToolbox" : encoders.some((e) => e.includes("nvenc")) ? "NVENC" : encoders[0] || "x264";
  const ver = (k: string) => (tools[k]?.version || "").split(/[-+ ]/)[0] || "—";
  const mono = (t: string, color = "var(--text-dim)") => <span className="mono" style={{ fontSize: 12, color }}>{t}</span>;
  const sections: [string, string][] = [["General", "settings"], ["Models", "cpu"], ["Hardware", "drive"], ["Integrations", "link"], ["MCP server", "terminal"], ["Privacy", "shield"], ["Storage", "folder"], ["About", "help"]];

  return (
    <div className="mainpad fadein">
      <div className="eyebrow" style={{ marginBottom: 6 }}>Settings</div>
      <h1 style={{ fontSize: 30, marginBottom: 22 }}>Settings</h1>
      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 28, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {sections.map(([sName, ic]) => (
            <button key={sName} onClick={() => setSec(sName)} className="row" style={{ gap: 10, padding: "9px 12px", borderRadius: 9, border: 0, background: sec === sName ? "var(--bg-3)" : "transparent", color: sec === sName ? "var(--text)" : "var(--text-dim)", cursor: "pointer", fontSize: 13.5, fontWeight: 500, fontFamily: "inherit", textAlign: "left" }}><Icon name={ic} size={16} />{sName}</button>
          ))}
        </div>
        <div style={{ maxWidth: 620 }}>
          {sec === "Models" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Whisper transcription">
                <Row l="Active model"
                  r={<Seg value={modelsQ.data?.active ?? ""} onChange={pickModel} options={models.map((m) => ({ value: m.name, label: m.label }))} />}
                  sub={installing ? `Downloading ${installing.name} — ${installing.total ? Math.round((installing.received / installing.total) * 100) : 0}%` : "Click a model to make it active; an un-downloaded model downloads first. The next transcribe uses it."} />
                <Row l="Downloaded models" r={mono(installedLabels.join(", ") || "none yet")} />
                <Row l="Engine" r={mono(`whisper.cpp ${ver("whisper_cpp")} · on-device`)} />
              </SettingCard>
              <SettingCard title="Moment-finding LLM">
                <Row l="Provider" r={mono("Codex CLI bridge")} sub="Your ChatGPT/Codex subscription — no API key, no GPU (SPOOL_LLM_PROVIDER)." />
                <Row l="Egress" r={mono("transcript text only", "var(--ok)")} sub="Media never leaves your machine; offline mode disables the bridge." />
              </SettingCard>
            </div>
          )}
          {sec === "Hardware" && (
            <SettingCard title="Performance & hardware">
              <Row l="Encoder" r={mono(encoder)} sub={`auto-detected: ${encoders.join(", ") || "probing…"}`} />
              <Row l="GPU" r={mono(machine.gpu ?? "—")} />
              <Row l="CPU cores" r={mono(String(machine.cpu_cores ?? "—"))} />
              <Row l="Render concurrency"
                r={<input type="range" min={1} max={8} step={1} value={s?.clip_workers ?? 2}
                  onChange={(e) => onConcurrency(+e.target.value)}
                  style={{ width: 180, accentColor: "var(--accent)" }} />}
                sub={`${s?.clip_workers ?? 2} parallel render${(s?.clip_workers ?? 2) === 1 ? "" : "s"} · applies on restart`} />
              <Row l="Mode"
                r={<Seg value={(s?.fast_default ?? true) ? "fast" : "quality"} onChange={(v) => save({ fast_default: v === "fast" })} options={[{ value: "fast", label: "Fast" }, { value: "quality", label: "Quality" }]} />}
                sub="Fast uses the hardware encoder; Quality is slower with a higher bitrate. Applies to new renders." />
            </SettingCard>
          )}
          {sec === "MCP server" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="MCP server">
                <Row l="Transport"
                  r={<Seg value={s?.mcp_transport ?? "stdio"} onChange={(v) => save({ mcp_transport: v })} options={[{ value: "stdio", label: "stdio" }, { value: "streamable-http", label: "HTTP" }]} />}
                  sub="stdio for Claude Desktop / Code; HTTP for headless or self-host. Applies on restart." />
                <Row l="Drives" r={mono("the same engine + queue as the UI", "var(--ok)")} sub="Manual mode and agent mode never diverge (one API → one engine → one job store)." />
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
          {sec === "Storage" && (
            <SettingCard title="Storage">
              <Row l="Library root" r={mono("~/Spool")} />
              <Row l="Free disk" r={mono(`${machine.free_disk_gb ?? "—"} GB`)} />
              <Row l="Renders" r={mono("engine/downloads/clips/<clip>/renders/")} sub="Each clip's rendered .mp4 (downloadable from the Editor → Export tab)." />
            </SettingCard>
          )}
          {sec === "General" && (
            <SettingCard title="General">
              <Row l="Default platform preset"
                r={<Seg value={s?.default_preset ?? "tiktok"} onChange={(v) => save({ default_preset: v })} options={PRESETS} />}
                sub="The export preset used when a render — or the agent — doesn't name a platform. Applies immediately." />
              <Row l="Appearance" r={mono("Light")} sub="Spool is light-only by design (the paper aesthetic)." />
            </SettingCard>
          )}
          {["Integrations", "About"].includes(sec) && (
            <SettingCard title={sec}>
              <div style={{ color: "var(--text-faint)", fontSize: 13.5, padding: "10px 0", lineHeight: 1.6 }}>{sec === "Integrations" ? "yt-dlp cookies and publish accounts arrive with Publish (Phase 4)." : "Spool — local-first clip studio, built on the open-source trove + clipify foundation (credited in the README)."}</div>
              {sec === "About" && <button className="btn ghost sm" onClick={() => ctx.nav("onboarding")}><Icon name="scan" size={15} /> Re-run Dependency Doctor</button>}
            </SettingCard>
          )}
        </div>
      </div>
    </div>
  );
}
