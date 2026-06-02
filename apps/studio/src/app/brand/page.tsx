"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { SettingCard, Row } from "@/components/spool/panels";
import { Btn, Chip, Icon, Seg, Switch, Thumb } from "@/components/spool/ui";

/* Brand kits — 1:1 port of the demo (07). A local kit editor (type, palette, caption style,
 * watermark, lower-third) with a live applied preview; "Apply to project" drives the agent.
 * Brand-kit persistence is the Phase-2 surface — the editor is the approved design. */

interface Kit { id: string; name: string; def: boolean; palette: string[]; display: string; body: string; caption: string; hl: string; wmPos: string; wmOp: number; exp: string }
const KITS: Kit[] = [
  { id: "acme", name: "Acme Media", def: true, palette: ["#45556E", "#C98A3D", "#E8E4DA", "#211E17"], display: "Schibsted Grotesk", body: "Schibsted Grotesk", caption: "opus", hl: "#F4D24B", wmPos: "br", wmOp: 70, exp: "9:16 · TikTok" },
  { id: "lena", name: "Lena Builds", def: false, palette: ["#4C6B54", "#D98C5F", "#F2EEE6", "#23201B"], display: "Instrument Serif", body: "Schibsted Grotesk", caption: "karaoke", hl: "#37E2A0", wmPos: "tl", wmOp: 55, exp: "9:16 · Reels" },
];
const wmCSS: Record<string, React.CSSProperties> = {
  tl: { top: "8%", left: "7%" }, tc: { top: "8%", left: "50%", transform: "translateX(-50%)" }, tr: { top: "8%", right: "7%" },
  ml: { top: "50%", left: "7%", transform: "translateY(-50%)" }, mc: { top: "50%", left: "50%", transform: "translate(-50%,-50%)" }, mr: { top: "50%", right: "7%", transform: "translateY(-50%)" },
  bl: { bottom: "10%", left: "7%" }, bc: { bottom: "10%", left: "50%", transform: "translateX(-50%)" }, br: { bottom: "10%", right: "7%" },
};

export default function BrandScreen() {
  const ctx = useSpool();
  const [kits, setKits] = useState<Kit[]>(KITS);
  const [selId, setSelId] = useState("acme");
  const kit = kits.find((k) => k.id === selId) || kits[0];
  const set = (patch: Partial<Kit>) => setKits((ks) => ks.map((k) => (k.id === selId ? { ...k, ...patch } : k)));
  const wmGrid = [["tl", "tc", "tr"], ["ml", "mc", "mr"], ["bl", "bc", "br"]];

  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 20 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Brand Kit</div><h1 style={{ fontSize: 30 }}>Brand kits</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" icon="plus" onClick={() => { const id = "k" + kits.length; setKits((ks) => [...ks, { id, name: "Untitled kit", def: false, palette: ["#45556E", "#9C968A", "#ECE9E1", "#211E17"], display: "Schibsted Grotesk", body: "Schibsted Grotesk", caption: "minimal", hl: "#F4D24B", wmPos: "br", wmOp: 60, exp: "9:16 · TikTok" }]); setSelId(id); }}>New kit</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "236px 1fr", gap: 24, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {kits.map((k) => (
            <button key={k.id} onClick={() => setSelId(k.id)} className="card" style={{ padding: 14, textAlign: "left", cursor: "pointer", borderColor: selId === k.id ? "var(--accent)" : "var(--line)", boxShadow: selId === k.id ? "0 0 0 3px var(--accent-soft)" : "var(--shadow-1)" }}>
              <div className="row" style={{ gap: 6, marginBottom: 11 }}>{k.palette.map((c, i) => <span key={i} style={{ width: 22, height: 22, borderRadius: 6, background: c, border: "1px solid var(--line)" }} />)}</div>
              <div className="row" style={{ gap: 8 }}><span style={{ fontWeight: 600 }}>{k.name}</span>{k.def && <Chip tone="ok">default</Chip>}</div>
            </button>
          ))}
        </div>

        <div>
          <div className="row" style={{ gap: 12, marginBottom: 18 }}>
            <input className="input" style={{ maxWidth: 280, fontWeight: 600, fontSize: 15 }} value={kit.name} onChange={(e) => set({ name: e.target.value })} />
            <label className="row" style={{ gap: 9, fontSize: 13, cursor: "pointer" }}><Switch on={kit.def} onClick={() => setKits((ks) => ks.map((k) => ({ ...k, def: k.id === selId })))} /> Default for new projects</label>
            <span className="spacer" />
            <Btn variant="primary" icon="palette" onClick={() => { ctx.pushToast({ icon: "palette", tone: "ok", title: `Applied “${kit.name}”`, body: "All clips in this project re-styled" }); ctx.askAgent(`Apply the ${kit.name} brand kit to these clips`); }}>Apply to project</Btn>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 20, alignItems: "start" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SettingCard title="Type">
                <Row l="Display font" r={<Seg value={kit.display} onChange={(v) => set({ display: v })} neutral options={[{ value: "Schibsted Grotesk", label: "Grotesk" }, { value: "Instrument Serif", label: "Serif" }, { value: "Archivo Black", label: "Heavy" }]} />} />
                <Row l="Body font" r={<Seg value={kit.body} onChange={(v) => set({ body: v })} neutral options={[{ value: "Schibsted Grotesk", label: "Grotesk" }, { value: "JetBrains Mono", label: "Mono" }]} />} />
              </SettingCard>
              <SettingCard title="Color palette">
                <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
                  {kit.palette.map((c, i) => (
                    <div key={i} style={{ textAlign: "center" }}>
                      <div style={{ width: 48, height: 48, borderRadius: 10, background: c, border: "1px solid var(--line)", boxShadow: "var(--shadow-1)" }} />
                      <div className="mono" style={{ fontSize: 9.5, color: "var(--text-faint)", marginTop: 5 }}>{c}</div>
                    </div>
                  ))}
                  <button className="card" style={{ width: 48, height: 48, borderRadius: 10, display: "grid", placeItems: "center", cursor: "pointer", borderStyle: "dashed", color: "var(--text-dim)", alignSelf: "flex-start" }} onClick={() => set({ palette: [...kit.palette, "#888379"] })}><Icon name="plus" size={16} /></button>
                </div>
              </SettingCard>
              <SettingCard title="Default caption style">
                <Row l="Preset" r={<Seg value={kit.caption} onChange={(v) => set({ caption: v })} neutral options={[{ value: "opus", label: "opus" }, { value: "karaoke", label: "karaoke" }, { value: "minimal", label: "minimal" }]} />} />
                <Row l="Highlight color" r={<div className="kbar">{["#F4D24B", "#FF7A3D", "#37E2A0", "#7C7CFF"].map((c) => <button key={c} onClick={() => set({ hl: c })} style={{ width: 26, height: 26, borderRadius: 7, background: c, border: kit.hl === c ? "2px solid var(--accent)" : "1px solid var(--line)", cursor: "pointer" }} />)}</div>} />
                <Row r={<Btn variant="ghost" size="sm" icon="type" onClick={() => ctx.nav("clips")}>Edit in Caption Studio →</Btn>} />
              </SettingCard>
              <SettingCard title="Logo / watermark">
                <div className="row" style={{ gap: 18, alignItems: "flex-start" }}>
                  <div>
                    <div className="field-label">Position</div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(3,26px)", gap: 5 }}>
                      {wmGrid.flat().map((p) => <button key={p} onClick={() => set({ wmPos: p })} style={{ width: 26, height: 26, borderRadius: 6, border: "1px solid var(--line)", background: kit.wmPos === p ? "var(--accent)" : "var(--bg-3)", cursor: "pointer" }} />)}
                    </div>
                  </div>
                  <div className="grow">
                    <div className="field-label">Opacity · {kit.wmOp}%</div>
                    <input type="range" min="10" max="100" value={kit.wmOp} onChange={(e) => set({ wmOp: +e.target.value })} style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <button className="card" style={{ marginTop: 12, width: "100%", padding: "14px 0", borderStyle: "dashed", display: "flex", flexDirection: "column", alignItems: "center", gap: 6, cursor: "pointer", color: "var(--text-dim)" }}><Icon name="upload" size={18} /><span style={{ fontSize: 12 }}>Upload logo (PNG)</span></button>
                  </div>
                </div>
              </SettingCard>
            </div>

            <div style={{ position: "sticky", top: 0, display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="card" style={{ padding: 14 }}>
                <div className="eyebrow" style={{ marginBottom: 10 }}>Applied preview</div>
                <div style={{ width: 170, margin: "0 auto", aspectRatio: "9/16", borderRadius: 12, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)" }}>
                  <Thumb seed="reframe" vertical kind="" label={false} />
                  <div style={{ position: "absolute", ...wmCSS[kit.wmPos], opacity: kit.wmOp / 100, color: "#fff", fontFamily: "var(--font-display)", fontWeight: 700, fontSize: 11, letterSpacing: ".02em", textShadow: "0 1px 4px rgba(0,0,0,.6)" }}>{kit.name.split(" ")[0]}</div>
                  <div style={{ position: "absolute", left: "7%", bottom: "30%", display: "flex", alignItems: "center", gap: 6 }}><div style={{ width: 3, height: 22, background: kit.palette[1] }} /><div><div style={{ color: "#fff", fontWeight: 700, fontSize: 10, fontFamily: kit.display === "Instrument Serif" ? "var(--font-serif)" : "var(--font-display)" }}>Dev Patel</div></div></div>
                  <div style={{ position: "absolute", left: 0, right: 0, bottom: "13%", textAlign: "center", fontFamily: "var(--font-caption)", fontSize: 16, color: "#fff", textShadow: "0 2px 6px #000" }}>local <span style={{ color: kit.hl }}>first</span></div>
                </div>
              </div>
              <div className="card" style={{ padding: 16 }}>
                <div className="eyebrow" style={{ marginBottom: 12 }}>Export defaults</div>
                <Row l="Format" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>{kit.exp}</span>} />
                <div className="row" style={{ marginTop: 10 }}><span style={{ fontSize: 13 }}>Caption preset</span><span className="spacer" /><Chip>{kit.caption}</Chip></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
