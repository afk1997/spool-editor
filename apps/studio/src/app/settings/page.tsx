"use client";

import { useEffect, useRef, useState } from "react";
import type { EngineSettings } from "@spool/api-client";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { describeActionError } from "@/lib/action-error";
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
  const client = ctx.client;
  const doctor = useEngineQuery((c) => c.doctor());
  const modelsQ = useEngineQuery((c) => c.listModels(), []);
  const storageQ = useEngineQuery((c) => c.storage(), []);
  const [sec, setSec] = useState("Models");
  const pendingSettingsRef = useRef<Set<keyof EngineSettings>>(new Set());
  const [pendingSettings, setPendingSettings] = useState<ReadonlySet<keyof EngineSettings>>(() => new Set());
  const [settingsSaveError, setSettingsSaveError] = useState<string | null>(null);
  const [concurrencyDraft, setConcurrencyDraft] = useState<number | null>(null);
  const concurrencyDraftRef = useRef<number | null>(null);
  const concurrencyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const modelMutationRef = useRef(false);
  const [modelMutating, setModelMutating] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (concurrencyTimer.current) clearTimeout(concurrencyTimer.current);
    };
  }, []);
  const s = ctx.settings;
  const settingDisabled = (...keys: (keyof EngineSettings)[]) =>
    !ctx.settingsReady || keys.some((key) => pendingSettings.has(key));
  const modelInstallBlock = !ctx.settingsReady || !s
    ? ctx.settingsLoading
      ? "Checking privacy settings. New model downloads are unavailable; installed models remain available."
      : "Privacy settings are unavailable. New model downloads are unavailable; installed models remain available."
    : s?.offline
      ? "Offline mode blocks new model downloads. Installed models remain available."
      : null;

  // Poll the model list while a download is in flight so the progress + installed state update.
  const installing = modelsQ.data?.install_progress?.downloading ? modelsQ.data.install_progress : null;
  useEffect(() => {
    if (!installing) return;
    const t = setInterval(() => modelsQ.reload(), 1500);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!installing]);

  const save = (patch: Partial<EngineSettings>) => {
    const keys = Object.keys(patch) as (keyof EngineSettings)[];
    if (!ctx.settingsReady || keys.length === 0 || keys.some((key) => pendingSettingsRef.current.has(key))) return null;
    const pending = new Set(pendingSettingsRef.current);
    keys.forEach((key) => pending.add(key));
    pendingSettingsRef.current = pending;
    setPendingSettings(pending);
    setSettingsSaveError(null);
    const work = ctx.updateSettings(patch)
      .then(() => undefined)
      .catch((error: unknown) => {
        if (!mounted.current) return;
        const failure = describeActionError(error);
        setSettingsSaveError(failure.message);
        ctx.pushToast({
          icon: "alert",
          tone: "warn",
          title: "Couldn't save setting",
          body: `${failure.code}: ${failure.message}`,
        });
      })
      .finally(() => {
        const next = new Set(pendingSettingsRef.current);
        keys.forEach((key) => next.delete(key));
        pendingSettingsRef.current = next;
        if (mounted.current) setPendingSettings(next);
      });
    return work;
  };

  const onConcurrency = (v: number) => {
    if (settingDisabled("clip_workers")) return;
    concurrencyDraftRef.current = v;
    setConcurrencyDraft(v);
    if (concurrencyTimer.current) clearTimeout(concurrencyTimer.current);
    concurrencyTimer.current = setTimeout(() => {
      concurrencyTimer.current = null;
      const next = concurrencyDraftRef.current;
      if (next == null) return;
      if (next === s?.clip_workers) {
        concurrencyDraftRef.current = null;
        if (mounted.current) setConcurrencyDraft(null);
        return;
      }
      const work = save({ clip_workers: next });
      if (!work) return;
      void work.finally(() => {
        concurrencyDraftRef.current = null;
        if (mounted.current) setConcurrencyDraft(null);
      });
    }, 400);
  };

  const models = modelsQ.data?.models ?? [];
  const modelInstallDescriptionId = modelInstallBlock && models.some((model) => !model.is_installed)
    ? "model-install-status"
    : undefined;
  const installedLabels = models.filter((m) => m.is_installed).map((m) => m.label);
  const pickModel = (name: string) => {
    const m = models.find((x) => x.name === name);
    if (!m || m.is_active || modelMutationRef.current || (!m.is_installed && modelInstallBlock)) return;
    modelMutationRef.current = true;
    setModelMutating(true);
    void (async () => {
      try {
        if (m.is_installed) await client.useModel(name);
        else await client.installModel(name);
        if (!mounted.current) return;
        modelsQ.reload();
        ctx.pushToast(m.is_installed
          ? { icon: "check", tone: "ok", title: "Active model set", body: m.label }
          : { icon: "download", tone: "info", title: `Downloading ${m.label}`, body: `${(m.size_bytes / 1e6).toFixed(0)} MB · becomes active when ready` });
      } catch (error) {
        if (mounted.current) {
          const failure = describeActionError(error);
          ctx.pushToast({ icon: "alert", tone: "warn", title: m.is_installed ? "Couldn't switch model" : "Couldn't install model", body: `${failure.code}: ${failure.message}` });
        }
      } finally {
        modelMutationRef.current = false;
        if (mounted.current) setModelMutating(false);
      }
    })();
  };

  const tools = doctor.data?.tools ?? {};
  const machine = (doctor.data?.machine ?? {}) as { free_disk_gb?: number; gpu?: string; cpu_cores?: number };
  const encoders = doctor.data?.encoders ?? [];
  const encoder = encoders.some((e) => e.includes("videotoolbox")) ? "VideoToolbox" : encoders.some((e) => e.includes("nvenc")) ? "NVENC" : encoders[0] || "x264";
  const ver = (k: string) => (tools[k]?.version || "").split(/[-+ ]/)[0] || "—";
  const mono = (t: string, color = "var(--text-dim)") => <span className="mono" style={{ fontSize: 12, color }}>{t}</span>;
  const storageRoot = typeof storageQ.data?.download_dir === "string" && storageQ.data.download_dir.trim()
    ? storageQ.data.download_dir
    : null;
  const sections: [string, string][] = [["General", "settings"], ["Models", "cpu"], ["Hardware", "drive"], ["Integrations", "link"], ["MCP server", "terminal"], ["Privacy", "shield"], ["Storage", "folder"], ["About", "help"]];
  const providerName = !ctx.settingsReady ? "not loaded" : s?.reasoning_provider === "codex" ? "Codex" : "None";
  const egressState = !ctx.settingsReady
    ? "settings unavailable"
    : s?.offline
      ? "blocked by Offline"
      : s?.reasoning_provider !== "codex"
        ? "disabled"
        : s.reasoning_egress_consent
          ? "consented"
          : "consent required";
  const privacyReadiness = ctx.settingsReady
    ? null
    : ctx.settingsLoading
      ? "Loading privacy settings…"
      : "Privacy settings are unavailable. Check the engine connection and try again.";

  return (
    <div className="mainpad fadein">
      <div className="eyebrow" style={{ marginBottom: 6 }}>Settings</div>
      <h1 style={{ fontSize: 30, marginBottom: 22 }}>Settings</h1>
      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 28, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {sections.map(([sName, ic]) => (
            <button key={sName} aria-pressed={sec === sName} onClick={() => setSec(sName)} className="row" style={{ gap: 10, padding: "9px 12px", borderRadius: 9, border: 0, background: sec === sName ? "var(--bg-3)" : "transparent", color: sec === sName ? "var(--text)" : "var(--text-dim)", cursor: "pointer", fontSize: 13.5, fontWeight: 500, fontFamily: "inherit", textAlign: "left" }}><Icon name={ic} size={16} />{sName}</button>
          ))}
        </div>
        <div style={{ maxWidth: 620 }}>
          {settingsSaveError && (
            <div role="alert" className="mono" style={{ marginBottom: 16, borderLeft: "3px solid var(--warn)", padding: "10px 12px", color: "var(--warn)", background: "var(--warn-soft)", borderRadius: 8, fontSize: 12, lineHeight: 1.55 }}>
              <b>Couldn&rsquo;t save settings.</b> {settingsSaveError}
            </div>
          )}
          {sec === "Models" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Whisper transcription">
                <Row l="Active model"
                  r={<Seg value={modelsQ.data?.active ?? ""} onChange={pickModel} disabled={modelMutating} options={models.map((m) => ({ value: m.name, label: m.label, disabled: !m.is_installed && !!modelInstallBlock, ariaDescribedBy: !m.is_installed ? modelInstallDescriptionId : undefined }))} />}
                  sub={installing
                    ? `Downloading ${installing.name} — ${installing.total ? Math.round((installing.received / installing.total) * 100) : 0}%`
                    : modelInstallBlock && models.some((model) => !model.is_installed)
                      ? modelInstallBlock
                      : "Click a model to make it active; an un-downloaded model downloads first. The next transcribe uses it."}
                  subId={modelInstallDescriptionId} />
                <Row l="Downloaded models" r={mono(installedLabels.join(", ") || "none yet")} />
                <Row l="Engine" r={mono(`whisper.cpp ${ver("whisper_cpp")} · on-device`)} />
              </SettingCard>
              <SettingCard title="Moment-finding LLM">
                <Row l="Provider" r={mono(providerName)} sub="Choose the remote reasoning provider in Privacy. None disables moment-finding reasoning." />
                <Row l="Codex text egress" r={mono(egressState, egressState === "consented" ? "var(--warn)" : "var(--text-dim)")} sub="Codex remote reasoning sends your message and any attached transcript text only after explicit consent; media files and local app state are not sent." />
              </SettingCard>
            </div>
          )}
          {sec === "Hardware" && (
            <SettingCard title="Performance & hardware">
              <Row l="Encoder" r={mono(encoder)} sub={`auto-detected: ${encoders.join(", ") || "probing…"}`} />
              <Row l="GPU" r={mono(machine.gpu ?? "—")} />
              <Row l="CPU cores" r={mono(String(machine.cpu_cores ?? "—"))} />
              <Row l="Render concurrency"
                r={<input type="range" aria-label="Render concurrency" min={1} max={8} step={1} value={concurrencyDraft ?? s?.clip_workers ?? 2}
                  onChange={(e) => onConcurrency(+e.target.value)}
                  disabled={settingDisabled("clip_workers")}
                  style={{ width: 180, accentColor: "var(--accent)" }} />}
                sub={`${concurrencyDraft ?? s?.clip_workers ?? 2} parallel render${(concurrencyDraft ?? s?.clip_workers ?? 2) === 1 ? "" : "s"} · ${concurrencyDraft == null ? "applies on restart" : "pending save"}`} />
              <Row l="Mode"
                r={<Seg value={(s?.fast_default ?? true) ? "fast" : "quality"} onChange={(v) => save({ fast_default: v === "fast" })} disabled={settingDisabled("fast_default")} options={[{ value: "fast", label: "Fast" }, { value: "quality", label: "Quality" }]} />}
                sub="Fast uses the hardware encoder; Quality is slower with a higher bitrate. Applies to new renders." />
            </SettingCard>
          )}
          {sec === "MCP server" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="MCP server">
                <Row l="Transport"
                  r={<Seg value={s?.mcp_transport ?? "stdio"} onChange={(v) => save({ mcp_transport: v })} disabled={settingDisabled("mcp_transport")} options={[{ value: "stdio", label: "stdio" }, { value: "streamable-http", label: "HTTP" }]} />}
                  sub="stdio for Claude Desktop / Code; HTTP for headless or self-host. Applies on restart." />
                <Row l="MCP Phase 0 access" r={mono("read-only local inspection", "var(--warn)")} sub="MCP can inspect its local read allowlist. Mutation requests are rejected by the runtime safety fuse." />
              </SettingCard>
              <SettingCard title="Mutation schemas · writes disabled">
                <div className="mono" style={{ fontSize: 11.5, color: "var(--warn)", lineHeight: 1.6, marginBottom: 10 }}>These schemas remain discoverable for client compatibility. Calling one returns <b>agent_mutation_disabled</b>; no write is executed.</div>
                <div className="kbar">{["find_moments", "cut_clip", "reframe_clip", "caption_clip", "render_clip", "render_pipeline"].map((t) => <span key={t} className="chip mono" style={{ fontSize: 11 }}>{t}</span>)}</div>
                <div style={{ marginTop: 14 }} className="card"><div className="mono" style={{ padding: 12, fontSize: 11.5, color: "rgba(255,255,255,0.66)", background: "#0E1013", borderRadius: "var(--radius)" }}>{'{ "mcpServers": { "spool": { "command": "spool-mcp" } } }'}</div></div>
              </SettingCard>
            </div>
          )}
          {sec === "Privacy" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Privacy">
                {privacyReadiness && <div role="status" className="mono" style={{ color: "var(--text-faint)", fontSize: 11.5, marginBottom: 12 }}>{privacyReadiness}</div>}
                <Row
                  l="Offline mode"
                  r={<Switch label="Offline mode" on={!!s?.offline} disabled={settingDisabled("offline")} onClick={() => save({ offline: !s?.offline })} />}
                  sub="Blocks all non-loopback network access, including URL downloads, remote models, watches, and Codex. Local media work remains available." />
                <Row
                  l="Reasoning provider"
                  r={<Seg
                    value={s?.reasoning_provider ?? "none"}
                    disabled={settingDisabled("reasoning_provider", "reasoning_egress_consent")}
                    onChange={(provider) => {
                      if (provider === "none" || provider === "codex") save({ reasoning_provider: provider });
                    }}
                    options={[{ value: "none", label: "None" }, { value: "codex", label: "Codex" }]}
                  />}
                  sub="None disables moment-finding reasoning. Codex is remote and requires explicit consent." />
                <Row
                  l="Codex Agent access"
                  r={mono("message + transcript only", "var(--warn)")}
                  sub="Codex cannot inspect your library, queues, watches, models, storage, files, or other local app state, and it cannot run local Agent tools." />
                {s?.reasoning_provider === "codex" && (
                  <Row
                    l="Codex text consent"
                    r={<Switch
                      label="Allow your message and any attached transcript text to leave this machine for Codex"
                      on={s.reasoning_egress_consent}
                      disabled={settingDisabled("reasoning_provider", "reasoning_egress_consent")}
                      onClick={() => save({ reasoning_egress_consent: !s.reasoning_egress_consent })}
                    />}
                    sub="When enabled, the text you send and any attached transcript text are sent to Codex for remote reasoning. Media files and local app state are not sent." />
                )}
              </SettingCard>
              <SettingCard title="What leaves your machine">
                <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.9 }}>
                  <div>URL import → the site you paste · <span style={{ color: "var(--warn)" }}>network download (you start it)</span></div>
                  <div>whisper · <span style={{ color: "var(--ok)" }}>on-device</span></div>
                  <div>remote reasoning · {ctx.settingsReady
                    ? s?.offline
                      ? <span style={{ color: "var(--warn)" }}>blocked by Offline · no current egress</span>
                      : s?.reasoning_provider === "codex" && s.reasoning_egress_consent
                        ? <span style={{ color: "var(--warn)" }}>your message + attached transcript text → Codex (consented)</span>
                        : s?.reasoning_provider === "codex"
                          ? "Codex selected · message and transcript egress blocked until consent"
                          : "disabled"
                    : "settings unavailable"}</div>
                </div>
              </SettingCard>
            </div>
          )}
          {sec === "Storage" && (
            <SettingCard title="Storage">
              <Row l="Library root" r={mono(storageRoot ?? "Unavailable")} sub={storageRoot ? "Reported by the connected engine." : "The connected engine did not report a storage path."} />
              <Row l="Free disk" r={mono(`${machine.free_disk_gb ?? "—"} GB`)} />
              <Row l="Renders" r={mono("Library-managed")} sub="Each rendered .mp4 is managed with the library media and downloadable from the Editor → Export tab." />
            </SettingCard>
          )}
          {sec === "General" && (
            <SettingCard title="General">
              <Row l="Default platform preset"
                r={<Seg value={s?.default_preset ?? "tiktok"} onChange={(v) => save({ default_preset: v })} disabled={settingDisabled("default_preset")} options={PRESETS} />}
                sub="The export preset used when a render doesn't name a platform. Applies immediately." />
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
