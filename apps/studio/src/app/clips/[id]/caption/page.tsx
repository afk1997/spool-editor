"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { SpoolApiError } from "@spool/api-client";
import { Btn, Icon, Switch } from "@spool/ui";

/* S8 Caption Studio — 1:1 port of the demo (05), fully wired (Phase 2). Fine styling
 * (font / size / weight / outline / fill / active-word color / all-caps / position /
 * words-per-line) maps to the REAL ASS via captioner overrides; "Match from image" pulls
 * an accent color from a dropped screenshot; the preview overlays the live style on the
 * clip's real reframed video + real transcript words. Burn POSTs style + overrides. */

interface CapStyle { font: string; size: number; weight: number; outline: number; fill: string; highlight: string | null; allcaps: boolean; position: number; words: number }
const PRESETS: Record<string, CapStyle> = {
  opus: { font: "Arial Black", size: 100, weight: 900, outline: 8, fill: "#ffffff", highlight: "#FFE94D", allcaps: true, position: 15, words: 3 },
  karaoke: { font: "Arial Black", size: 110, weight: 700, outline: 6, fill: "#ffffff", highlight: "#37E2A0", allcaps: false, position: 12, words: 4 },
  minimal: { font: "Helvetica", size: 70, weight: 700, outline: 4, fill: "#ffffff", highlight: null, allcaps: false, position: 9, words: 6 },
};
const FONTS = [
  { label: "Impact", css: "Impact, 'Arial Black', sans-serif", ass: "Impact" },
  { label: "Arial Black", css: "'Arial Black', sans-serif", ass: "Arial Black" },
  { label: "Helvetica", css: "Helvetica, Arial, sans-serif", ass: "Helvetica" },
  { label: "Georgia", css: "Georgia, serif", ass: "Georgia" },
];
const FILLS = ["#ffffff", "#FFE94D", "#37E2A0", "#000000"];
const HLS = ["#FFE94D", "#37E2A0", "#6AA9FF", "#FF6B9D", "#FF5C5C", "#C77DFF"];
const rgbHex = (r: number, g: number, b: number) => "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");

function Slider({ label, value, min, max, step, fmt, onChange }: { label: string; value: number; min: number; max: number; step: number; fmt: (v: number) => string; onChange: (v: number) => void }) {
  return (
    <div>
      <div className="row" style={{ marginBottom: 4 }}><span style={{ fontSize: 12.5 }}>{label}</span><span className="spacer" /><span className="mono" style={{ fontSize: 11.5, color: "var(--text-dim)" }}>{fmt(value)}</span></div>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(+e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} />
    </div>
  );
}

function Swatches({ colors, value, onPick, allowNone }: { colors: string[]; value: string | null; onPick: (c: string | null) => void; allowNone?: boolean }) {
  return (
    <div className="row" style={{ gap: 7, flexWrap: "wrap" }}>
      {colors.map((c) => (
        <button key={c} onClick={() => onPick(c)} title={c} style={{ width: 24, height: 24, borderRadius: 6, background: c, cursor: "pointer", border: value?.toLowerCase() === c.toLowerCase() ? "2px solid var(--accent)" : "1px solid var(--line-str)", boxShadow: c.toLowerCase() === "#ffffff" ? "inset 0 0 0 1px var(--line)" : "none" }} />
      ))}
      {allowNone && <button onClick={() => onPick(null)} title="none" style={{ width: 24, height: 24, borderRadius: 6, cursor: "pointer", border: value === null ? "2px solid var(--accent)" : "1px solid var(--line-str)", display: "grid", placeItems: "center", fontSize: 11, color: "var(--text-faint)" }}>∅</button>}
    </div>
  );
}

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

  const OPUS = PRESETS.opus!; // the "opus" key is statically present in PRESETS
  const [preset, setPreset] = useState("opus");
  const [font, setFont] = useState(OPUS.font);
  const [size, setSize] = useState(OPUS.size);
  const [weight, setWeight] = useState(OPUS.weight);
  const [outline, setOutline] = useState(OPUS.outline);
  const [fill, setFill] = useState(OPUS.fill);
  const [highlight, setHighlight] = useState<string | null>(OPUS.highlight);
  const [allcaps, setAllcaps] = useState(OPUS.allcaps);
  const [position, setPosition] = useState(OPUS.position);
  const [wpl, setWpl] = useState(OPUS.words);
  // Caption craft (item D) — additive engine options; off by default. The engine applies
  // speaker color only when the clip window actually has 2+ diarized speakers (else a no-op).
  const [colorSpeakers, setColorSpeakers] = useState(false);
  const [emphasis, setEmphasis] = useState(false);
  const [balanceLines, setBalanceLines] = useState(false);

  const applyPreset = (p: string) => {
    const c = PRESETS[p]; if (!c) return;
    setPreset(p); setFont(c.font); setSize(c.size); setWeight(c.weight); setOutline(c.outline);
    setFill(c.fill); setHighlight(c.highlight); setAllcaps(c.allcaps); setPosition(c.position); setWpl(c.words);
  };

  const [hot, setHot] = useState(0);
  useEffect(() => { const iv = setInterval(() => setHot((h) => (h + 1) % Math.max(1, words.length)), 480); return () => clearInterval(iv); }, [words.length]);
  const startW = Math.floor(hot / wpl) * wpl;
  const shown = words.slice(startW, startW + wpl);

  const [pname, setPname] = useState<"reframed" | "clip">("reframed");
  const previewUrl = `${ctx.client.clipArtifactUrl(id, pname)}?v=${id}-${pname}`;
  const cssFont = FONTS.find((f) => f.ass === font)?.css ?? font;
  const PREVIEW_W = 340;
  const pxSize = Math.round(size * PREVIEW_W / 1080);
  const pxOutline = Math.max(0, Math.round(outline * PREVIEW_W / 1080));

  const onImage = (file: File | undefined) => {
    if (!file) return;
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const cv = document.createElement("canvas"); const s = 50; cv.width = s; cv.height = s;
      const g = cv.getContext("2d"); if (!g) { URL.revokeObjectURL(url); return; }
      g.drawImage(img, 0, 0, s, s);
      const data = g.getImageData(0, 0, s, s).data;
      let best = { score: -1, hex: highlight ?? "#FFE94D" };
      for (let i = 0; i < data.length; i += 4) {
        const r = data[i]!, gg = data[i + 1]!, b = data[i + 2]!; // RGBA buffer length is a multiple of 4, so i, i+1, i+2 are in range
        const mx = Math.max(r, gg, b), mn = Math.min(r, gg, b);
        const sat = mx === 0 ? 0 : (mx - mn) / mx, bright = mx / 255;
        const score = sat * bright;
        if (bright > 0.4 && score > best.score) best = { score, hex: rgbHex(r, gg, b) };
      }
      setHighlight(best.hex);
      URL.revokeObjectURL(url);
      ctx.pushToast({ icon: "wand", tone: "info", title: "Matched from image", body: `Accent → ${best.hex}` });
    };
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  };

  const burn = () => {
    ctx.pushToast({ icon: "zap", tone: "info", title: "Burning captions", body: `${preset} · custom style · track it in the queue` });
    void (async () => {
      try {
        // Submit captions FIRST: the common failure (409 no_transcript) surfaces here, where the
        // old fire-and-forget toasted success and navigated to an empty queue regardless. Render
        // waits for the caption job to finish (it reads caption's output file).
        const cap = await ctx.client.caption(id, {
          style: preset,
          overrides: { size, outline, words: wpl, fill, highlight, position, allcaps, weight, font },
          color_speakers: colorSpeakers, emphasis, balance_lines: balanceLines,
        });
        ctx.nav("queue");
        await ctx.awaitClipJob(cap.id);
        await ctx.client.render(id, { preset: clip?.platform || "tiktok" });
      } catch (e) {
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Caption failed",
          body: e instanceof SpoolApiError && e.code === "no_transcript"
            ? "Transcribe the source first — captions need words."
            : "See the Render Queue for details." });
      }
    })();
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
          <div style={{ aspectRatio: "9/16", borderRadius: "var(--radius)", overflow: "hidden", position: "relative", border: "1px solid var(--line-str)", background: "#000" }}>
            <video key={previewUrl} src={previewUrl} muted loop autoPlay playsInline style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }} onError={() => { if (pname === "reframed") setPname("clip"); }} />
            <div style={{ position: "absolute", left: "6%", right: "6%", bottom: position + "%", textAlign: "center", lineHeight: 1.12 }}>
              {shown.map((w, i) => {
                const isHot = startW + i === hot;
                return <span key={i} style={{
                  fontFamily: cssFont, fontSize: pxSize, fontWeight: weight, margin: "0 4px", display: "inline-block",
                  color: isHot && highlight ? highlight : fill, textTransform: allcaps ? "uppercase" : "none",
                  WebkitTextStroke: pxOutline ? `${pxOutline}px #000` : "0", paintOrder: "stroke fill",
                  textShadow: pxOutline ? "none" : "0 2px 8px rgba(0,0,0,0.85)", transition: "color .12s" }}>{w} </span>;
              })}
            </div>
          </div>
          <div className="kbar" style={{ marginTop: 14, justifyContent: "center" }}>
            {["opus", "karaoke", "minimal"].map((p) => <button key={p} className={"chip" + (preset === p ? " solid" : "")} style={{ cursor: "pointer", height: 30, textTransform: "capitalize" }} onClick={() => applyPreset(p)}>{p}</button>)}
          </div>
          <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", textAlign: "center", marginTop: 10 }}>{hasReal ? `${words.length} words · from the transcript` : doc.loading ? "loading transcript…" : "source not transcribed — showing the title"}</div>
        </div>

        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
          <label className="row" style={{ gap: 10, cursor: "pointer", padding: "10px 12px", border: "1px dashed var(--line-str)", borderRadius: 10 }}>
            <Icon name="wand" size={16} style={{ color: "var(--accent)" }} />
            <div style={{ flex: 1 }}><div style={{ fontSize: 12.5, fontWeight: 600 }}>Match from image</div><div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>drop a screenshot → pull its accent color</div></div>
            <input type="file" accept="image/*" style={{ display: "none" }} onChange={(e) => onImage(e.target.files?.[0])} />
          </label>

          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>Font</div>
            <div className="row" style={{ gap: 7, flexWrap: "wrap" }}>
              {FONTS.map((f) => <button key={f.ass} className={"chip" + (font === f.ass ? " solid" : "")} style={{ cursor: "pointer", height: 30, fontFamily: f.css }} onClick={() => setFont(f.ass)}>{f.label}</button>)}
            </div>
          </div>

          <Slider label="Size" value={size} min={40} max={160} step={2} fmt={(v) => `${v}`} onChange={setSize} />
          <Slider label="Weight" value={weight} min={300} max={900} step={100} fmt={(v) => `${v}`} onChange={setWeight} />
          <Slider label="Outline" value={outline} min={0} max={16} step={1} fmt={(v) => `${v}`} onChange={setOutline} />

          <div><div className="eyebrow" style={{ marginBottom: 8 }}>Fill</div><Swatches colors={FILLS} value={fill} onPick={(c) => c && setFill(c)} /></div>
          <div>
            <div className="row" style={{ marginBottom: 8 }}><div className="eyebrow">Active word</div><span className="spacer" /><Switch on={highlight !== null} onClick={() => setHighlight((h) => (h === null ? "#FFE94D" : null))} /></div>
            <Swatches colors={HLS} value={highlight} onPick={setHighlight} allowNone />
          </div>

          <div className="row"><span style={{ fontSize: 12.5 }}>All-caps</span><span className="spacer" /><Switch on={allcaps} onClick={() => setAllcaps((a) => !a)} /></div>

          <Slider label="Position" value={position} min={4} max={60} step={1} fmt={(v) => `${v}%`} onChange={setPosition} />
          <Slider label="Words / line" value={wpl} min={1} max={8} step={1} fmt={(v) => `${v}`} onChange={setWpl} />

          <div style={{ borderTop: "1px solid var(--line)", paddingTop: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>Caption craft</div>
            <div className="row" style={{ marginBottom: 10 }}>
              <div><div style={{ fontSize: 12.5 }}>Speaker colors</div><div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>tint each word by who&apos;s talking · 2+ speakers</div></div>
              <span className="spacer" /><Switch on={colorSpeakers} onClick={() => setColorSpeakers((v) => !v)} />
            </div>
            <div className="row" style={{ marginBottom: 10 }}>
              <div><div style={{ fontSize: 12.5 }}>Keyword emphasis</div><div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>scale up acronyms / shouted words</div></div>
              <span className="spacer" /><Switch on={emphasis} onClick={() => setEmphasis((v) => !v)} />
            </div>
            <div className="row">
              <div><div style={{ fontSize: 12.5 }}>Balance lines</div><div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>no 1-word orphan lines</div></div>
              <span className="spacer" /><Switch on={balanceLines} onClick={() => setBalanceLines((v) => !v)} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
