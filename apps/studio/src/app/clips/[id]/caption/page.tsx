"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { Btn, Icon, Thumb } from "@spool/ui";

/* S8 Caption Studio — opus / karaoke / minimal presets (REAL: Burn applies the chosen style
 * via the engine), previewed over the clip's REAL transcript words (words.json sliced to the
 * clip window). Fine styling, match-from-image and per-word whisper fixes are the Phase-2
 * surface — the engine burns the preset today, so those controls would be fabricated now. */

interface CapStyle { font: string; size: number; weight: number; fill: string; hl: string; outline: number; allcaps: boolean; words: number; pos: number }
const CAP_PRESETS: Record<string, CapStyle> = {
  opus: { font: "var(--font-caption)", size: 30, weight: 900, fill: "#ffffff", hl: "#FFE94D", outline: 3, allcaps: true, words: 3, pos: 62 },
  karaoke: { font: "var(--font-display)", size: 26, weight: 700, fill: "#ffffff", hl: "#37E2A0", outline: 2, allcaps: false, words: 4, pos: 70 },
  minimal: { font: "var(--font-ui)", size: 22, weight: 700, fill: "#ffffff", hl: "#ffffff", outline: 0, allcaps: false, words: 5, pos: 80 },
};

export default function CaptionScreen() {
  const ctx = useSpool();
  const id = String(useParams().id);
  const clip = ctx.clips.find((c) => c.id === id);
  const source = clip ? ctx.sources.find((s) => s.id === clip.src) : undefined;
  const doc = useEngineQuery((c) => (source?.transcriptId ? c.getTranscriptDoc(source.transcriptId) : Promise.resolve(undefined)), [source?.transcriptId]);

  // the clip's real words, sliced to its source-time window (fallback: the clip title)
  const live = doc.data?.words ?? [];
  const lo = clip?.start ?? 0, hi = clip?.end ?? Infinity;
  const realWords = live.filter((x) => !x.deleted && x.start != null && x.start >= lo && x.start <= hi).map((x) => x.w);
  const hasReal = realWords.length > 0;
  const words = hasReal ? realWords : (clip?.title || "your captions here").split(" ");

  const [preset, setPreset] = useState("opus");
  const cfg = CAP_PRESETS[preset];
  const [hot, setHot] = useState(0);
  useEffect(() => { const iv = setInterval(() => setHot((h) => (h + 1) % Math.max(1, words.length)), 480); return () => clearInterval(iv); }, [words.length]);

  const start = Math.floor(hot / cfg.words) * cfg.words;
  const shown = words.slice(start, start + cfg.words);

  const burn = () => {
    ctx.client.caption(id, { style: preset }).then(() => ctx.client.render(id, { preset: clip?.platform || "tiktok" }).catch(() => {})).catch(() => {});
    ctx.pushToast({ icon: "zap", tone: "info", title: "Burning captions", body: `${preset} · track it in the queue` });
    ctx.nav("queue");
  };

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1240 }}>
      <button className="btn subtle sm" style={{ marginBottom: 12, paddingLeft: 0 }} onClick={() => ctx.nav("editor", { id })}><Icon name="chevL" size={15} /> Editor</button>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Caption Studio</div><h1 style={{ fontSize: 28 }}>Style the captions</h1></div>
        <span className="spacer" />
        <Btn variant="primary" icon="zap" onClick={burn}>Burn captions</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "380px 1fr", gap: 24, alignItems: "start" }}>
        <div style={{ position: "sticky", top: 0 }}>
          <div style={{ aspectRatio: "9/16", borderRadius: "var(--radius)", overflow: "hidden", position: "relative", border: "1px solid var(--line-str)" }}>
            <Thumb seed={id} vertical kind="" label={false} />
            <div style={{ position: "absolute", left: "6%", right: "6%", top: cfg.pos + "%", textAlign: "center", lineHeight: 1.12 }}>
              {shown.map((w, i) => {
                const isHot = start + i === hot;
                return <span key={i} className={"cap-word" + (isHot ? " hot" : "")} style={{
                  fontFamily: cfg.font, fontSize: cfg.size, fontWeight: cfg.weight, margin: "0 4px",
                  color: isHot ? cfg.hl : cfg.fill, textTransform: cfg.allcaps ? "uppercase" : "none",
                  WebkitTextStroke: cfg.outline ? `${cfg.outline}px #000` : "0", paintOrder: "stroke fill",
                  textShadow: cfg.outline ? "none" : "0 2px 8px rgba(0,0,0,0.8)", transition: "color .12s" }}>{w} </span>;
              })}
            </div>
          </div>
          <div className="kbar" style={{ marginTop: 14, justifyContent: "center" }}>
            {["opus", "karaoke", "minimal"].map((p) => <button key={p} className={"chip" + (preset === p ? " solid" : "")} style={{ cursor: "pointer", height: 30, textTransform: "capitalize" }} onClick={() => setPreset(p)}>{p}</button>)}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="card" style={{ padding: 16 }}>
            <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Caption text · from the transcript</div><span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>{hasReal ? `${words.length} words` : ""}</span></div>
            {hasReal ? (
              <div className="kbar">{words.map((w, i) => <span key={i} className="chip" style={{ height: 28, background: i === hot ? "var(--accent)" : "var(--bg-3)", color: i === hot ? "var(--accent-ink)" : "var(--text-dim)" }}>{w}</span>)}</div>
            ) : (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>{doc.loading ? "Loading the clip's transcript…" : "This clip's source isn't transcribed yet — transcribe it to caption from real words."}</div>
            )}
          </div>

          <div className="card" style={{ padding: 16 }}>
            <div className="row"><div className="eyebrow">Fine styling · match-from-image · per-word fixes</div><span className="spacer" /><span className="chip warn">Phase 2</span></div>
            <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 10, lineHeight: 1.6 }}>The three presets above are what the engine burns today (real opus / karaoke / minimal ASS styles). Per-control styling (size, outline, color, position), matching a style from a dropped screenshot, and correcting individual Whisper words arrive in Phase 2.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
