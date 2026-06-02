"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { Btn, Icon, Seg, Switch, Thumb } from "@/components/spool/ui";

/* S8 Caption Studio — 1:1 port of the demo (05). opus / karaoke / minimal presets, live
 * animated caption preview, full style controls, caption lane. "Burn captions" applies the
 * chosen style + renders on the real engine (the burned words come from words.json). */

interface CapCfg { font: string; size: number; weight: number; fill: string; hl: string; outline: number; allcaps: boolean; anim: string; words: number; pos: number; keyword?: boolean; emoji?: boolean }
const CAP_PRESETS: Record<string, CapCfg> = {
  opus: { font: "var(--font-caption)", size: 30, weight: 900, fill: "#ffffff", hl: "#FFE94D", outline: 3, allcaps: true, anim: "pop", words: 3, pos: 62 },
  karaoke: { font: "var(--font-display)", size: 26, weight: 700, fill: "#ffffff", hl: "#37E2A0", outline: 2, allcaps: false, anim: "fade", words: 4, pos: 70 },
  minimal: { font: "var(--font-ui)", size: 22, weight: 700, fill: "#ffffff", hl: "#ffffff", outline: 0, allcaps: false, anim: "none", words: 5, pos: 80 },
};
const CAP_TEXT = "I deleted the production database at three in the morning".split(" ");

export default function CaptionScreen() {
  const ctx = useSpool();
  const id = String(useParams().id);
  const [preset, setPreset] = useState("opus");
  const [cfg, setCfg] = useState<CapCfg>(CAP_PRESETS.opus);
  const [hot, setHot] = useState(0);
  const [matching, setMatching] = useState(false);
  const set = <K extends keyof CapCfg>(k: K, v: CapCfg[K]) => setCfg((c) => ({ ...c, [k]: v }));
  // switching preset resets the style, but keeps the orthogonal keyword/emoji add-on toggles
  const applyPreset = (p: string) => { setPreset(p); setCfg((c) => ({ ...CAP_PRESETS[p], keyword: c.keyword, emoji: c.emoji })); };

  useEffect(() => { const iv = setInterval(() => setHot((h) => (h + 1) % CAP_TEXT.length), 480); return () => clearInterval(iv); }, []);

  const start = Math.floor(hot / cfg.words) * cfg.words;
  const shown = CAP_TEXT.slice(start, start + cfg.words);

  const onDrop = (e: React.SyntheticEvent) => { e.preventDefault(); setMatching(true); setTimeout(() => { setMatching(false); setCfg((c) => ({ ...c, fill: "#FFFFFF", hl: "#FF7A3D", size: 28, outline: 2, weight: 800 })); setPreset("matched"); ctx.pushToast({ icon: "wand", tone: "ok", title: "Style matched", body: "Inferred font, orange highlight, 2px outline" }); }, 1500); };

  const burn = () => {
    const style = ["opus", "karaoke", "minimal"].includes(preset) ? preset : "opus";
    ctx.client.caption(id, { style }).then(() => ctx.client.render(id, { preset: "tiktok" }).catch(() => {})).catch(() => {});
    ctx.pushToast({ icon: "zap", tone: "info", title: "Burning captions", body: `${style} · track it in the queue` });
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
            <Thumb seed="reframe" vertical kind="" label={false} />
            <div style={{ position: "absolute", left: "6%", right: "6%", top: cfg.pos + "%", textAlign: "center", lineHeight: 1.12 }}>
              {shown.map((w, i) => {
                const isHot = start + i === hot;
                const isKey = cfg.keyword && w.replace(/[^a-zA-Z]/g, "").length > 6;
                return <span key={i} className={"cap-word" + (isHot && cfg.anim === "pop" ? " hot" : "")} style={{
                  fontFamily: cfg.font, fontSize: isKey ? cfg.size * 1.08 : cfg.size, fontWeight: isKey ? 900 : cfg.weight, margin: "0 4px",
                  color: isHot || isKey ? cfg.hl : cfg.fill, textTransform: cfg.allcaps ? "uppercase" : "none",
                  WebkitTextStroke: cfg.outline ? `${cfg.outline}px #000` : "0", paintOrder: "stroke fill",
                  textShadow: cfg.outline ? "none" : "0 2px 8px rgba(0,0,0,0.8)",
                  transition: "color .12s", opacity: cfg.anim === "fade" && !isHot && start + i > hot ? 0.45 : 1 }}>{w}{cfg.emoji && isKey ? " 🔥" : ""} </span>;
              })}
            </div>
          </div>
          <div className="kbar" style={{ marginTop: 14, justifyContent: "center" }}>
            {["opus", "karaoke", "minimal"].map((p) => <button key={p} className={"chip" + (preset === p ? " solid" : "")} style={{ cursor: "pointer", height: 30, textTransform: "capitalize" }} onClick={() => applyPreset(p)}>{p}</button>)}
            {preset === "matched" && <span className="chip acc" style={{ height: 30 }}>matched</span>}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="card" onDragOver={(e) => e.preventDefault()} onDrop={onDrop} onClick={onDrop}
            style={{ padding: 16, borderStyle: "dashed", borderColor: "var(--line-str)", cursor: "pointer", display: "flex", gap: 13, alignItems: "center" }}>
            <div className="ill" style={{ width: 46, height: 46, borderRadius: 12 }}>{matching ? <Icon name="scan" size={20} style={{ animation: "pulse 1s infinite", color: "var(--accent)" }} /> : <Icon name="wand" size={20} />}</div>
            <div><div style={{ fontWeight: 600, marginBottom: 2 }}>{matching ? "Analyzing reference…" : "Match from image"}</div><div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>Drop a screenshot — the agent infers font, color &amp; layout.</div></div>
          </div>

          <div className="card" style={{ padding: 18, display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px 24px" }}>
            <div style={{ gridColumn: "1 / -1" }} className="eyebrow">Style</div>
            <Ctl label="Font">
              <Seg value={cfg.font} onChange={(v) => set("font", v)} neutral options={[{ value: "var(--font-caption)", label: "Archivo" }, { value: "var(--font-display)", label: "Grotesk" }, { value: "var(--font-ui)", label: "Hanken" }]} />
            </Ctl>
            <Ctl label={`Size · ${cfg.size}px`}><input type="range" min="16" max="44" value={cfg.size} onChange={(e) => set("size", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} /></Ctl>
            <Ctl label={`Weight · ${cfg.weight}`}><input type="range" min="400" max="900" step="100" value={cfg.weight} onChange={(e) => set("weight", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} /></Ctl>
            <Ctl label={`Outline · ${cfg.outline}px`}><input type="range" min="0" max="6" value={cfg.outline} onChange={(e) => set("outline", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} /></Ctl>
            <Ctl label="Fill"><SwatchRow value={cfg.fill} onChange={(v) => set("fill", v)} options={["#ffffff", "#FFE94D", "#0B0C0E", "#37E2A0"]} /></Ctl>
            <Ctl label="Active-word highlight"><SwatchRow value={cfg.hl} onChange={(v) => set("hl", v)} options={["#FFE94D", "#FF7A3D", "#37E2A0", "#7C7CFF", "#FF55D6"]} /></Ctl>
            <Ctl label={`Words per line · ${cfg.words}`}><input type="range" min="1" max="6" value={cfg.words} onChange={(e) => set("words", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} /></Ctl>
            <Ctl label={`Position · ${cfg.pos}%`}><input type="range" min="30" max="85" value={cfg.pos} onChange={(e) => set("pos", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} /></Ctl>
            <Ctl label="Animation"><Seg value={cfg.anim} onChange={(v) => set("anim", v)} neutral options={[{ value: "pop", label: "Pop" }, { value: "fade", label: "Fade" }, { value: "none", label: "None" }]} /></Ctl>
            <Ctl label="All caps"><div className="row" style={{ height: 36, alignItems: "center" }}><Switch on={cfg.allcaps} onClick={() => set("allcaps", !cfg.allcaps)} /></div></Ctl>
            <Ctl label="Keyword emphasis"><div className="row" style={{ height: 36, alignItems: "center" }}><Switch on={!!cfg.keyword} onClick={() => set("keyword", !cfg.keyword)} /></div></Ctl>
            <Ctl label="Auto-emoji"><div className="row" style={{ height: 36, alignItems: "center" }}><Switch on={!!cfg.emoji} onClick={() => set("emoji", !cfg.emoji)} /></div></Ctl>
          </div>

          <div className="card" style={{ padding: 16 }}>
            <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Caption lane</div><span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>click a word to fix Whisper errors</span></div>
            <div className="kbar">
              {CAP_TEXT.map((w, i) => (
                <span key={i} className="chip" style={{ cursor: "text", height: 28, background: i === hot ? "var(--accent)" : "var(--bg-3)", color: i === hot ? "var(--accent-ink)" : "var(--text-dim)" }}>{w}</span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Ctl({ label, children }: { label: string; children: ReactNode }) { return <div><span className="field-label">{label}</span>{children}</div>; }
function SwatchRow({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: string[] }) {
  return <div className="kbar">{options.map((o) => (
    <button key={o} onClick={() => onChange(o)} style={{ width: 30, height: 30, borderRadius: 8, background: o, border: value === o ? "2px solid var(--accent)" : "1px solid var(--line-str)", cursor: "pointer", boxShadow: value === o ? "0 0 0 3px var(--accent-soft)" : "none" }} />
  ))}</div>;
}
