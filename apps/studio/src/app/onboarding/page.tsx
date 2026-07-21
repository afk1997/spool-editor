"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { Btn, Chip, Icon, SpoolMark } from "@spool/ui";

/* S0 Onboarding / Dependency Doctor — 1:1 port of the demo (07). Full-screen, no shell.
 * Every step reflects the LIVE /doctor report (real tools, versions, disk, encoder); "Fix"
 * re-probes + points at the install command (the engine can't self-install). The Models +
 * "all set" steps show the real detected state — model download/switching is the Phase-2
 * settings surface, so the demo's fake download/test-render animations are gone (no dummy). */

const TOOL_META: Record<string, { name: string; note: string; hint: string }> = {
  ffmpeg: { name: "ffmpeg", note: "encode / decode · VideoToolbox + libx264", hint: "brew install ffmpeg" },
  python: { name: "Python", note: "runtime for the engine & skill scripts", hint: "install Python 3.11+" },
  whisper_cpp: { name: "whisper.cpp", note: "on-device transcription", hint: "pip install pywhispercpp" },
  yt_dlp: { name: "yt-dlp", note: "URL downloader", hint: "pip install -U yt-dlp" },
};
const trimVer = (v: string | null) => (v ? v.split(/[-+ ]/)[0] : "—");

export default function OnboardingScreen() {
  const ctx = useSpool();
  const doctor = useEngineQuery((c) => c.doctor());
  const [step, setStep] = useState(0);

  const tools = doctor.data?.tools ?? {};
  const deps = Object.entries(tools).map(([id, t]) => ({
    id, name: TOOL_META[id]?.name ?? id, note: TOOL_META[id]?.note ?? "", hint: TOOL_META[id]?.hint ?? "",
    status: t.present ? "ok" : "missing", ver: trimVer(t.version),
  }));
  const anyMissing = deps.some((d) => d.status === "missing");
  const machine = (doctor.data?.machine ?? {}) as { free_disk_gb?: number };
  const encoders = doctor.data?.encoders ?? [];
  const encoder = encoders.some((e) => e.includes("videotoolbox")) ? "VideoToolbox" : encoders.some((e) => e.includes("nvenc")) ? "NVENC" : encoders[0] || "x264";
  const freeDisk = machine.free_disk_gb != null ? `${machine.free_disk_gb} GB free on disk` : "checking disk…";
  const privacySummary = !ctx.settingsReady
    ? ctx.settingsLoading
      ? "Checking privacy settings…"
      : "Privacy settings unavailable."
    : ctx.offline
      ? "Offline mode blocks network access."
      : ctx.reasoningProvider === "codex" && ctx.reasoningEgressConsent
        ? "Codex remote reasoning is enabled."
        : "Remote reasoning is off.";
  const momentFindingSummary = !ctx.settingsReady
    ? "Privacy settings not loaded"
    : ctx.offline
      ? ctx.reasoningProvider === "codex" && ctx.reasoningEgressConsent
        ? "Codex configured with consent · no current transcript egress"
        : "Offline mode · no current transcript egress"
      : ctx.reasoningProvider !== "codex"
        ? "No remote provider selected · transcript text stays on this machine"
        : ctx.reasoningEgressConsent
          ? "Codex remote reasoning · transcript text leaves this machine with consent"
          : "Codex selected · transcript text stays on this machine until you consent";
  const momentFindingTone = !ctx.settingsReady || ctx.offline || (ctx.reasoningProvider === "codex" && !ctx.reasoningEgressConsent)
    ? "warn"
    : "ok";
  const momentFindingState = !ctx.settingsReady
    ? "checking"
    : ctx.offline
      ? "blocked by Offline"
      : ctx.reasoningProvider !== "codex"
        ? "disabled"
        : ctx.reasoningEgressConsent
          ? "enabled"
          : "consent required";

  const fix = (id: string, hint: string) => { ctx.pushToast({ icon: "terminal", tone: "info", title: `Install ${id}`, body: hint ? `Run: ${hint}` : "See the docs, then re-check." }); doctor.reload(); };

  const steps = ["Welcome", "Dependencies", "Models", "Test render"];
  const StatusDot = ({ s }: { s: string }) => <span style={{ width: 10, height: 10, borderRadius: "50%", background: s === "ok" ? "var(--ok)" : s === "warn" ? "var(--warn)" : "var(--err)", boxShadow: `0 0 8px ${s === "ok" ? "var(--ok)" : s === "warn" ? "var(--warn)" : "var(--err)"}` }} />;

  return (
    <div style={{ height: "100vh", display: "grid", gridTemplateColumns: "320px 1fr", background: "var(--bg)" }}>
      <div style={{ background: "linear-gradient(170deg, var(--bg-1), var(--bg))", borderRight: "1px solid var(--line)", padding: "40px 34px", display: "flex", flexDirection: "column" }}>
        <div className="row" style={{ gap: 11, marginBottom: 40 }}><SpoolMark size={30} /><span className="wordmark" style={{ fontSize: 27, lineHeight: 1 }}>Spool</span></div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {steps.map((s, i) => (
            <div key={s} className="row" style={{ gap: 12, padding: "10px 12px", borderRadius: 10, background: i === step ? "var(--bg-3)" : "transparent", color: i === step ? "var(--text)" : "var(--text-faint)" }}>
              <span style={{ width: 24, height: 24, borderRadius: "50%", display: "grid", placeItems: "center", fontSize: 12, fontFamily: "var(--font-mono)", background: i < step ? "var(--accent)" : i === step ? "var(--accent-soft)" : "var(--bg-3)", color: i < step ? "var(--accent-ink)" : i === step ? "var(--accent)" : "var(--text-faint)" }}>{i < step ? <Icon name="check" size={13} /> : i + 1}</span>
              {s}
            </div>
          ))}
        </div>
        <div className="spacer" />
        <div className="card" style={{ padding: 13, background: "var(--bg-2)", borderColor: "var(--line)" }}>
          <div className="row" style={{ gap: 8, fontSize: 12.5 }}><Icon name="shield" size={15} style={{ color: "var(--text-dim)" }} />{privacySummary}</div>
        </div>
      </div>

      <div style={{ overflow: "auto", display: "flex", alignItems: "center", justifyContent: "center", padding: 40 }}>
        <div style={{ width: "min(560px,100%)" }} className="fadein" key={step}>
          {step === 0 && (
            <div>
              <div className="eyebrow" style={{ marginBottom: 14 }}>Welcome to Spool</div>
              <h1 style={{ fontSize: 34, lineHeight: 1.1, marginBottom: 16 }}>Turn long videos into platform-ready shorts with local media processing.</h1>
              <p style={{ color: "var(--text-dim)", fontSize: 15, lineHeight: 1.6, marginBottom: 28 }}>URL downloads use the network. Transcription and rendering run on this machine; Codex receives transcript text only when remote reasoning is selected and consented.</p>
              <div className="row" style={{ gap: 12 }}><Btn variant="primary" size="lg" iconR="arrowR" onClick={() => setStep(1)}>Let&rsquo;s set up</Btn><Btn variant="ghost" size="lg" onClick={() => ctx.nav("home")}>Skip for now</Btn></div>
            </div>
          )}
          {step === 1 && (
            <div>
              <h1 style={{ fontSize: 26, marginBottom: 6 }}>Dependency Doctor</h1>
              <p style={{ color: "var(--text-faint)", marginTop: 0, marginBottom: 22 }}>Spool checked your machine for the tools it needs. Missing ones show the install command.</p>
              <div className="panel" style={{ overflow: "hidden", marginBottom: 22 }}>
                {doctor.loading && <div style={{ padding: "16px", color: "var(--text-faint)", fontSize: 13 }}>Probing your machine…</div>}
                {deps.map((d, i) => (
                  <div key={d.id} className="row" style={{ padding: "13px 16px", gap: 14, borderBottom: i < deps.length - 1 ? "1px solid var(--line-2)" : "none" }}>
                    <StatusDot s={d.status} />
                    <div className="grow"><div style={{ fontWeight: 600 }}>{d.name} <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)", fontWeight: 400 }}>{d.ver}</span></div><div style={{ fontSize: 12, color: "var(--text-faint)" }}>{d.note}</div></div>
                    {d.status === "ok" ? <Chip tone="ok">detected</Chip> : <Btn variant="primary" size="sm" icon="terminal" onClick={() => fix(d.id, d.hint)}>How to fix</Btn>}
                  </div>
                ))}
              </div>
              <div className="row" style={{ gap: 12 }}><Btn variant="primary" size="lg" iconR="arrowR" onClick={() => setStep(2)} disabled={anyMissing}>Continue</Btn>{anyMissing && <span style={{ fontSize: 12.5, color: "var(--warn)" }}>Install the missing tools to continue</span>}</div>
            </div>
          )}
          {step === 2 && (
            <div>
              <h1 style={{ fontSize: 26, marginBottom: 6 }}>Models & storage</h1>
              <p style={{ color: "var(--text-faint)", marginTop: 0, marginBottom: 22 }}>What Spool found on your machine. Model download/switching gets a full settings UI in Phase 2.</p>
              <div className="panel" style={{ overflow: "hidden", marginBottom: 20 }}>
                <div className="row" style={{ padding: "13px 16px", gap: 12, borderBottom: "1px solid var(--line-2)" }}><Icon name="type" size={16} style={{ color: "var(--accent)" }} /><div className="grow"><b>Transcription</b><div style={{ fontSize: 12, color: "var(--text-faint)" }}>whisper.cpp {trimVer(tools.whisper_cpp?.version ?? null)} · on-device</div></div><Chip tone={tools.whisper_cpp?.present ? "ok" : "warn"}>{tools.whisper_cpp?.present ? "ready" : "missing"}</Chip></div>
                <div className="row" style={{ padding: "13px 16px", gap: 12 }}><Icon name="sparkles" size={16} style={{ color: "var(--accent)" }} /><div className="grow"><b>Moment-finding</b><div style={{ fontSize: 12, color: "var(--text-faint)" }}>{momentFindingSummary}</div></div><Chip tone={momentFindingTone}>{momentFindingState}</Chip></div>
              </div>
              <span className="field-label">Storage location</span>
              <input className="input mono" defaultValue="~/Spool" readOnly />
              <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 7 }}>{freeDisk} · encoder auto-detected: {encoder}</div>
              <div className="row" style={{ gap: 12, marginTop: 26 }}><Btn variant="primary" size="lg" iconR="arrowR" onClick={() => setStep(3)}>Continue</Btn></div>
            </div>
          )}
          {step === 3 && (
            <div style={{ textAlign: "center" }}>
              <div className="ill" style={{ width: 90, height: 90, margin: "0 auto 22px", borderRadius: 24, color: doctor.data?.ok ? "var(--ok)" : "var(--warn)" }}><Icon name={doctor.data?.ok ? "check" : "alert"} size={38} /></div>
              <h1 style={{ fontSize: 26, marginBottom: 8 }}>{doctor.data?.ok ? "You're all set" : "Almost there"}</h1>
              <p style={{ color: "var(--text-dim)", fontSize: 14.5, marginBottom: 26, maxWidth: 420, margin: "0 auto 26px" }}>{doctor.data?.ok ? `Your machine has the full pipeline — ffmpeg, whisper.cpp, yt-dlp and the ${encoder} encoder. Time to make your first clip.` : "Fix the missing tools back in the Dependencies step, then come back."}</p>
              <Btn variant="primary" size="lg" icon="import" onClick={() => ctx.nav("import")}>Make my first clip →</Btn>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
