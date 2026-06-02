"use client";

import { useMemo, useState } from "react";
import type { BrandKit } from "@spool/api-client";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { Btn, Icon } from "@spool/ui";

/* S9 Brand Kits — 1:1 port of the demo (07), fully wired (Phase 2). Kits persist via the
 * engine's /brand-kits store; "Apply to project" caption-burns the kit's preset + highlight
 * + font + watermark + lower-third across every clip of a source, then renders. Zero dummy. */

const PALETTE_POOL = ["#45556E", "#C98A3D", "#E8E4DA", "#211E17", "#4C6B54", "#D98C5F", "#6AA9FF", "#FF6B9D"];
const HLS = ["#FFE94D", "#37E2A0", "#6AA9FF", "#FF6B9D", "#FF5C5C", "#C77DFF"];
const FONTS = [
  { label: "Display", css: "'Arial Black', sans-serif", ass: "Arial Black" },
  { label: "Impact", css: "Impact, sans-serif", ass: "Impact" },
  { label: "Serif", css: "Georgia, serif", ass: "Georgia" },
];

interface Form { name: string; palette: string[]; preset: string; highlight: string; font: string; watermark: string; lowerThird: string }
const EMPTY: Form = { name: "", palette: ["#45556E", "#C98A3D"], preset: "opus", highlight: "#FFE94D", font: "Arial Black", watermark: "", lowerThird: "" };
const toForm = (k: BrandKit): Form => ({
  name: k.name || "", palette: k.palette ?? [], preset: k.caption_preset || "opus",
  highlight: (k.caption_overrides?.highlight as string) || "#FFE94D", font: (k.caption_overrides?.font as string) || "Arial Black",
  watermark: k.watermark || "", lowerThird: k.lower_third || "",
});
const toKit = (f: Form): Partial<BrandKit> => ({
  name: f.name.trim() || "Untitled kit", palette: f.palette, caption_preset: f.preset,
  caption_overrides: { highlight: f.highlight, font: f.font }, watermark: f.watermark.trim(), lower_third: f.lowerThird.trim(),
});

function Swatches({ colors, picked, onPick, multi }: { colors: string[]; picked: string[]; onPick: (c: string) => void; multi?: boolean }) {
  return (
    <div className="row" style={{ gap: 7, flexWrap: "wrap" }}>
      {colors.map((c) => (
        <button key={c} onClick={() => onPick(c)} title={c}
          style={{ width: 26, height: 26, borderRadius: 7, background: c, cursor: "pointer", border: picked.map((x) => x.toLowerCase()).includes(c.toLowerCase()) ? "2px solid var(--accent)" : "1px solid var(--line-str)", boxShadow: c.toLowerCase() === "#e8e4da" || c.toLowerCase() === "#ffffff" ? "inset 0 0 0 1px var(--line)" : "none" }} />
      ))}
      {multi && <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", alignSelf: "center" }}>click to add/remove</span>}
    </div>
  );
}

export default function BrandScreen() {
  const ctx = useSpool();
  const kitsQ = useEngineQuery((c) => c.listBrandKits(), []);
  const kits = useMemo(() => kitsQ.data?.brand_kits ?? [], [kitsQ.data]);

  const [sel, setSel] = useState<string | null>(null);   // null = new (unsaved) kit
  const [f, setF] = useState<Form>(EMPTY);
  const [synced, setSynced] = useState(false);
  const [applySrc, setApplySrc] = useState("");
  const [saving, setSaving] = useState(false);

  // Auto-load the first kit into the editor once it arrives — set-state-during-render is the
  // supported React pattern for syncing to async data (no effect, runs once via the guard).
  if (!synced && sel === null && kits.length) { setSel(kits[0].id); setF(toForm(kits[0])); setSynced(true); }

  const selectKit = (k: BrandKit) => { setSel(k.id); setF(toForm(k)); };
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((s) => ({ ...s, [k]: v }));
  const togglePalette = (c: string) => set("palette", f.palette.map((x) => x.toLowerCase()).includes(c.toLowerCase()) ? f.palette.filter((x) => x.toLowerCase() !== c.toLowerCase()) : [...f.palette, c]);

  const newKit = () => { setSel(null); setF({ ...EMPTY, name: "" }); setSynced(true); };
  const save = () => {
    setSaving(true);
    const body = toKit(f);
    const p = sel ? ctx.client.updateBrandKit(sel, body) : ctx.client.createBrandKit(body);
    p.then((k) => { setSel(k.id); kitsQ.reload(); ctx.pushToast({ icon: "check", tone: "ok", title: "Brand kit saved", body: k.name }); })
      .catch(() => ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't save the kit" }))
      .finally(() => setSaving(false));
  };
  const del = () => {
    if (!sel) return;
    ctx.client.deleteBrandKit(sel).then(() => { setSel(null); setF({ ...EMPTY, name: "" }); kitsQ.reload(); ctx.pushToast({ icon: "trash", tone: "info", title: "Kit deleted" }); }).catch(() => {});
  };

  const targetClips = ctx.clips.filter((c) => c.src === applySrc);
  const applyToProject = () => {
    if (!applySrc || !targetClips.length) { ctx.pushToast({ icon: "alert", tone: "warn", title: "Pick a project with clips" }); return; }
    const ov = { highlight: f.highlight, font: f.font };
    for (const c of targetClips) {
      ctx.client.caption(c.id, { style: f.preset, overrides: ov, watermark: f.watermark.trim() || undefined, lower_third: f.lowerThird.trim() || undefined })
        .then(() => ctx.client.render(c.id, { preset: c.platform || "tiktok" }).catch(() => {})).catch(() => {});
    }
    ctx.pushToast({ icon: "palette", tone: "info", title: `Applying “${f.name || "kit"}” to ${targetClips.length} clip${targetClips.length > 1 ? "s" : ""}`, body: "Caption + render queued — track it in the Render Queue" });
    ctx.nav("queue");
  };

  const fontCss = FONTS.find((x) => x.ass === f.font)?.css ?? f.font;

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1240 }}>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Brand kits</div><h1 style={{ fontSize: 28 }}>A reusable look</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" icon="plus" onClick={newKit}>New kit</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr 300px", gap: 20, alignItems: "start" }}>
        {/* kit list */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {kits.length === 0 && <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No kits yet — set a name, palette, caption look and watermark, then Save.</div>}
          {kits.map((k) => (
            <div key={k.id} className="card" onClick={() => selectKit(k)} style={{ padding: 13, cursor: "pointer", borderColor: sel === k.id ? "var(--accent)" : "var(--line)" }}>
              <div className="row" style={{ gap: 6, marginBottom: 10 }}>{(k.palette ?? []).slice(0, 5).map((c, j) => <span key={j} style={{ width: 20, height: 20, borderRadius: 6, background: c, border: "1px solid var(--line)" }} />)}</div>
              <div style={{ fontWeight: 600, fontSize: 13.5 }}>{k.name}</div>
            </div>
          ))}
        </div>

        {/* editor */}
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="row">
            <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Kit name"
              style={{ font: "inherit", fontSize: 17, fontWeight: 600, background: "transparent", border: 0, borderBottom: "1px solid var(--line)", color: "var(--text)", outline: "none", padding: "2px 0", flex: 1 }} />
            <span className="spacer" />
            {sel && <button className="btn subtle sm" style={{ color: "var(--err, #e5484d)" }} onClick={del}><Icon name="trash" size={14} /></button>}
            <Btn variant="primary" icon="check" onClick={save} disabled={saving}>{sel ? "Save" : "Create"}</Btn>
          </div>

          <div><div className="eyebrow" style={{ marginBottom: 8 }}>Color palette</div><Swatches colors={PALETTE_POOL} picked={f.palette} onPick={togglePalette} multi /></div>

          <div className="row" style={{ gap: 28, flexWrap: "wrap" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Caption preset</div>
              <div className="row" style={{ gap: 7 }}>{["opus", "karaoke", "minimal"].map((p) => <button key={p} className={"chip" + (f.preset === p ? " solid" : "")} style={{ cursor: "pointer", height: 30, textTransform: "capitalize" }} onClick={() => set("preset", p)}>{p}</button>)}</div>
            </div>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Highlight</div><Swatches colors={HLS} picked={[f.highlight]} onPick={(c) => set("highlight", c)} /></div>
          </div>

          <div><div className="eyebrow" style={{ marginBottom: 8 }}>Caption font</div>
            <div className="row" style={{ gap: 7 }}>{FONTS.map((x) => <button key={x.ass} className={"chip" + (f.font === x.ass ? " solid" : "")} style={{ cursor: "pointer", height: 30, fontFamily: x.css }} onClick={() => set("font", x.ass)}>{x.label}</button>)}</div>
          </div>

          <div className="row" style={{ gap: 16 }}>
            <div style={{ flex: 1 }}><div className="eyebrow" style={{ marginBottom: 6 }}>Watermark</div><input value={f.watermark} onChange={(e) => set("watermark", e.target.value)} placeholder="@yourhandle" style={{ width: "100%", font: "inherit", fontSize: 13, padding: "7px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }} /></div>
            <div style={{ flex: 1 }}><div className="eyebrow" style={{ marginBottom: 6 }}>Lower-third</div><input value={f.lowerThird} onChange={(e) => set("lowerThird", e.target.value)} placeholder="Ep. 42 — title" style={{ width: "100%", font: "inherit", fontSize: 13, padding: "7px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }} /></div>
          </div>
        </div>

        {/* preview + apply */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card" style={{ padding: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>Applied preview</div>
            <div style={{ width: 168, margin: "0 auto", aspectRatio: "9/16", borderRadius: 12, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)", background: "linear-gradient(160deg,#1a1d22,#0a0b0d)" }}>
              {f.watermark.trim() && <div style={{ position: "absolute", top: "5%", right: "6%", color: "#fff", fontFamily: fontCss, fontWeight: 700, fontSize: 11, opacity: 0.7, textShadow: "0 1px 4px rgba(0,0,0,.6)" }}>{f.watermark.trim()}</div>}
              {f.lowerThird.trim() && <div style={{ position: "absolute", top: "9%", left: 0, right: 0, textAlign: "center", color: "#fff", fontFamily: fontCss, fontWeight: 700, fontSize: 11, textShadow: "0 1px 4px #000" }}>{f.lowerThird.trim()}</div>}
              <div style={{ position: "absolute", left: "8%", right: "8%", bottom: "14%", textAlign: "center", fontFamily: fontCss, fontWeight: 800, fontSize: 19, color: "#fff", textTransform: f.preset === "opus" ? "uppercase" : "none", WebkitTextStroke: "1px #000", textShadow: "0 2px 6px #000", lineHeight: 1.1 }}>local <span style={{ color: f.highlight }}>first</span></div>
            </div>
          </div>
          <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="eyebrow">Apply to a project</div>
            <select value={applySrc} onChange={(e) => setApplySrc(e.target.value)} style={{ font: "inherit", fontSize: 13, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }}>
              <option value="">Choose a project…</option>
              {ctx.sources.map((s) => <option key={s.id} value={s.id}>{s.title} ({ctx.clips.filter((c) => c.src === s.id).length})</option>)}
            </select>
            <Btn variant="primary" icon="palette" onClick={applyToProject} disabled={!applySrc || targetClips.length === 0}>Apply to {targetClips.length} clip{targetClips.length === 1 ? "" : "s"}</Btn>
            <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>Re-captions every clip of the project with this kit&apos;s preset, highlight, font, watermark + lower-third, then renders.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
