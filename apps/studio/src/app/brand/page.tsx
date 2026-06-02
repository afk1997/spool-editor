"use client";

import { FutureScreen, SettingCard, Row } from "@/components/spool/panels";
import { Chip, Thumb } from "@spool/ui";

/* Brand Kits — Phase 2 (spec §5). The kit designer is fully designed; its backend
 * (persisted kits + apply-on-render with fonts/palette/watermark/lower-third) lands in
 * Phase 2. Shown as the honest "designed — coming in Phase 2" surface, not a fake editor. */

const KITS: [string, string[]][] = [
  ["Acme Media", ["#45556E", "#C98A3D", "#E8E4DA", "#211E17"]],
  ["Lena Builds", ["#4C6B54", "#D98C5F", "#F2EEE6", "#23201B"]],
];

export default function BrandScreen() {
  return (
    <FutureScreen code="Brand Kit" phase="2" icon="palette" title="Brand kits"
      desc="Save a reusable look — fonts, color palette, caption preset, logo/watermark and a lower-third — and apply it across a project's clips on render. Designed; the backend (persistence + apply-on-render) lands in Phase 2.">
      <div style={{ display: "grid", gridTemplateColumns: "236px 1fr 300px", gap: 20, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {KITS.map(([name, pal], i) => (
            <div key={name} className="card" style={{ padding: 14, borderColor: i === 0 ? "var(--accent)" : "var(--line)" }}>
              <div className="row" style={{ gap: 6, marginBottom: 11 }}>{pal.map((c, j) => <span key={j} style={{ width: 22, height: 22, borderRadius: 6, background: c, border: "1px solid var(--line)" }} />)}</div>
              <div className="row" style={{ gap: 8 }}><span style={{ fontWeight: 600 }}>{name}</span>{i === 0 && <Chip tone="ok">default</Chip>}</div>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <SettingCard title="Type"><Row l="Display" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>Grotesk</span>} /><Row l="Body" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>Grotesk</span>} /></SettingCard>
          <SettingCard title="Caption + watermark"><Row l="Preset" r={<Chip>opus</Chip>} /><Row l="Highlight" r={<span style={{ width: 22, height: 22, borderRadius: 6, background: "#F4D24B", display: "inline-block" }} />} /><Row l="Watermark" r={<span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>bottom-right · 70%</span>} /></SettingCard>
        </div>
        <div className="card" style={{ padding: 14 }}>
          <div className="eyebrow" style={{ marginBottom: 10 }}>Applied preview</div>
          <div style={{ width: 170, margin: "0 auto", aspectRatio: "9/16", borderRadius: 12, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)" }}>
            <Thumb seed="reframe" vertical kind="" label={false} />
            <div style={{ position: "absolute", top: "8%", right: "7%", color: "#fff", fontFamily: "var(--font-display)", fontWeight: 700, fontSize: 11, textShadow: "0 1px 4px rgba(0,0,0,.6)" }}>Acme</div>
            <div style={{ position: "absolute", left: 0, right: 0, bottom: "13%", textAlign: "center", fontFamily: "var(--font-caption)", fontSize: 16, color: "#fff", textShadow: "0 2px 6px #000" }}>local <span style={{ color: "#F4D24B" }}>first</span></div>
          </div>
        </div>
      </div>
    </FutureScreen>
  );
}
