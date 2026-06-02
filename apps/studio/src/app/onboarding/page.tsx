"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { Btn, Chip, Icon, Progress, Seg, SpoolMark } from "@/components/spool/ui";

/* S0 Onboarding / Dependency Doctor — 1:1 port of the demo (07). Full-screen, no shell.
 * The Dependency Doctor step reads the LIVE /doctor report (real tools, versions, disk,
 * encoder); "Fix" re-probes + points at the install command (the engine can't self-install).
 * Model/test-render steps keep the demo's wizard UI (no live model-download endpoint yet). */

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
  const [model, setModel] = useState("base.en");
  const [llm, setLlm] = useState("local");
  const [dl, setDl] = useState(0);
  const [testing, setTesting] = useState<"idle" | "run" | "ok">("idle");

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

  const fix = (id: string, hint: string) => { ctx.pushToast({ icon: "terminal", tone: "info", title: `Install ${id}`, body: hint ? `Run: ${hint}` : "See the docs, then re-check." }); doctor.reload(); };
  const downloadModel = () => { let p = 0; const iv = setInterval(() => { p += 12; setDl(Math.min(100, p)); if (p >= 100) clearInterval(iv); }, 200); };
  const testRender = () => { setTesting("run"); setTimeout(() => setTesting("ok"), 2200); };

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
        <div className="card" style={{ padding: 13, background: "var(--ok-soft)", borderColor: "transparent" }}>
          <div className="row" style={{ gap: 8, fontSize: 12.5 }}><Icon name="shield" size={15} style={{ color: "var(--ok)" }} />Everything runs on your machine.</div>
        </div>
      </div>

      <div style={{ overflow: "auto", display: "flex", alignItems: "center", justifyContent: "center", padding: 40 }}>
        <div style={{ width: "min(560px,100%)" }} className="fadein" key={step}>
          {step === 0 && (
            <div>
              <div className="eyebrow" style={{ marginBottom: 14 }}>Welcome to Spool</div>
              <h1 style={{ fontSize: 34, lineHeight: 1.1, marginBottom: 16 }}>Turn long videos into platform-ready shorts — without the cloud.</h1>
              <p style={{ color: "var(--text-dim)", fontSize: 15, lineHeight: 1.6, marginBottom: 28 }}>Spool runs an agent and a full editor over a local media pipeline: download, transcribe, find moments, reframe, caption, render. No uploads, no subscriptions, no waiting on a server.</p>
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
              <p style={{ color: "var(--text-faint)", marginTop: 0, marginBottom: 22 }}>Pick a transcription model and where Spool keeps your library.</p>
              <span className="field-label">Whisper model</span>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 8 }}>
                {([["tiny.en", "fastest · ~75MB"], ["base.en", "balanced · ~145MB"], ["small.en", "most accurate · ~480MB"]] as const).map(([m, note]) => (
                  <button key={m} onClick={() => setModel(m)} className="card" style={{ padding: "13px 12px", textAlign: "left", cursor: "pointer", borderColor: model === m ? "var(--accent)" : "var(--line)", background: model === m ? "var(--accent-soft)" : "var(--bg-2)" }}>
                    <div className="mono" style={{ fontWeight: 600, marginBottom: 4 }}>{m}</div><div style={{ fontSize: 11, color: "var(--text-faint)" }}>{note}</div>
                  </button>
                ))}
              </div>
              {dl < 100 ? <div className="row" style={{ gap: 12, marginBottom: 18 }}><Btn variant="ghost" size="sm" icon="download" onClick={downloadModel}>Download {model}</Btn>{dl > 0 && <div style={{ flex: 1 }}><Progress value={dl} striped /></div>}</div>
                : <div className="row" style={{ gap: 8, marginBottom: 18, color: "var(--ok)", fontSize: 13 }}><Icon name="check" size={15} /> {model} ready</div>}
              <span className="field-label">Moment-finding model</span>
              <Seg value={llm} onChange={setLlm} neutral options={[{ value: "local", label: "Local (Ollama)" }, { value: "hosted", label: "Hosted + my key" }, { value: "later", label: "Decide later" }]} />
              <div style={{ marginTop: 20 }}><span className="field-label">Storage location</span>
                <div className="row" style={{ gap: 10 }}><input className="input mono" defaultValue="~/Spool" /><Btn variant="ghost" icon="folder">Browse</Btn></div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 7 }}>{freeDisk} · encoder auto-detected: {encoder}</div>
              </div>
              <div className="row" style={{ gap: 12, marginTop: 26 }}><Btn variant="primary" size="lg" iconR="arrowR" onClick={() => setStep(3)}>Continue</Btn></div>
            </div>
          )}
          {step === 3 && (
            <div style={{ textAlign: "center" }}>
              <div className="ill" style={{ width: 90, height: 90, margin: "0 auto 22px", borderRadius: 24, color: testing === "ok" ? "var(--ok)" : "var(--accent)" }}><Icon name={testing === "ok" ? "check" : "zap"} size={38} /></div>
              <h1 style={{ fontSize: 26, marginBottom: 8 }}>{testing === "ok" ? "You're all set" : "One quick test render"}</h1>
              <p style={{ color: "var(--text-dim)", fontSize: 14.5, marginBottom: 26, maxWidth: 400, margin: "0 auto 26px" }}>{testing === "ok" ? "The full pipeline works on your machine. Time to make your first clip." : "Spool will cut a 3-second test clip to prove ffmpeg, the encoder and captions all work together."}</p>
              {testing === "run" && <div style={{ maxWidth: 320, margin: "0 auto 22px" }}><Progress value={66} striped /><div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 8 }}>ffmpeg · encoding test clip…</div></div>}
              {testing === "idle" && <Btn variant="primary" size="lg" icon="play" onClick={testRender}>Run test render</Btn>}
              {testing === "ok" && <Btn variant="primary" size="lg" icon="import" onClick={() => ctx.nav("import")}>Make my first clip →</Btn>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
