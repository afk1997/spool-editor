"use client";

import { useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { Btn, Icon, Seg, Thumb, fmtTC } from "@spool/ui";

/* S7 Reframe / ROI editor — 1:1 port of the demo (05). Draggable ROI boxes; pan/split/center;
 * speaker-track lane; live 9:16 preview. "Apply & re-render" sends the ROIs to the real
 * reframe endpoint. Auto-detect previews what the engine resolves server-side on reframe. */

interface Box { x: number; y: number; w: number; h: number }

function ROIBox({ box, color, label, onChange, containerRef }: { box: Box; color: string; label: string; onChange: (b: Box) => void; containerRef: React.RefObject<HTMLDivElement | null> }) {
  const ref = useRef<HTMLDivElement>(null);
  // §6.3: drive the drag imperatively (no setState per pointermove → no re-render storm);
  // commit the final box to state once, on pointerup.
  const drag = (e: React.PointerEvent, mode: "move" | "resize") => {
    e.preventDefault(); e.stopPropagation();
    const rect = containerRef.current!.getBoundingClientRect();
    const start = { px: e.clientX, py: e.clientY, ...box };
    let latest: Box = { ...box };
    const move = (ev: PointerEvent) => {
      const dx = ((ev.clientX - start.px) / rect.width) * 100;
      const dy = ((ev.clientY - start.py) / rect.height) * 100;
      const b: Box = { x: start.x, y: start.y, w: start.w, h: start.h };
      if (mode === "move") { b.x = Math.max(0, Math.min(100 - start.w, start.x + dx)); b.y = Math.max(0, Math.min(100 - start.h, start.y + dy)); }
      else { b.w = Math.max(12, Math.min(100 - start.x, start.w + dx)); b.h = Math.max(12, Math.min(100 - start.y, start.h + dy)); }
      latest = b;
      const el = ref.current;
      if (el) { el.style.left = b.x + "%"; el.style.top = b.y + "%"; el.style.width = b.w + "%"; el.style.height = b.h + "%"; }
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); onChange(latest); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  };
  return (
    <div ref={ref} onPointerDown={(e) => drag(e, "move")}
      style={{ position: "absolute", left: box.x + "%", top: box.y + "%", width: box.w + "%", height: box.h + "%", border: `2px solid ${color}`, borderRadius: 6, cursor: "grab", boxShadow: "0 0 0 9999px rgba(0,0,0,0.18)", touchAction: "none" }}>
      <div style={{ position: "absolute", top: -22, left: 0, fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 700, color, background: "rgba(0,0,0,0.7)", padding: "1px 6px", borderRadius: 5 }}>{label}</div>
      <div onPointerDown={(e) => drag(e, "resize")} style={{ position: "absolute", right: -7, bottom: -7, width: 14, height: 14, borderRadius: "50%", background: color, cursor: "nwse-resize", border: "2px solid #000" }} />
    </div>
  );
}

export default function ReframeScreen() {
  const ctx = useSpool();
  const id = String(useParams().id);
  const [mode, setMode] = useState("pan");
  const [boxes, setBoxes] = useState<{ L: Box; R: Box }>({ L: { x: 6, y: 18, w: 40, h: 64 }, R: { x: 54, y: 16, w: 40, h: 66 } });
  const [active, setActive] = useState<"L" | "R">("L");
  const [frame, setFrame] = useState(28);
  const [detecting, setDetecting] = useState(false);
  const frameRef = useRef<HTMLDivElement>(null);
  // the REAL diar⊕ROI track from the clip's latest reframe job (read-only in P1; editable in P2)
  const reframeJob = (ctx.snapshot?.clips ?? []).filter((c) => c.clip_id === id && c.kind === "reframe" && c.status === "done" && (c.result.segments?.length ?? 0) > 0).at(-1);
  const segs = reframeJob?.result.segments ?? [];
  const trackSource = (reframeJob?.result.source as string) || "";

  const autoDetect = () => { setDetecting(true); setTimeout(() => { setDetecting(false); setBoxes({ L: { x: 7, y: 20, w: 38, h: 62 }, R: { x: 55, y: 17, w: 39, h: 65 } }); ctx.pushToast({ icon: "scan", tone: "info", title: "Boxes reset", body: "Adjust the L/R boxes, then Apply — the engine refines detection on render." }); }, 600); };
  const applyReframe = () => {
    const pct = (b: Box) => ({ x: b.x / 100, y: b.y / 100, w: b.w / 100, h: b.h / 100 });
    ctx.client.reframe(id, { aspect: "9:16", mode, ...(mode !== "center" ? { rois: { left: pct(boxes.L), right: pct(boxes.R) } } : {}) }).catch(() => {});
    ctx.pushToast({ icon: "refresh", tone: "info", title: "Reframing", body: `${mode} · 9:16 · track in the queue` });
    ctx.nav("caption", { id });
  };

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1240 }}>
      <button className="btn subtle sm" style={{ marginBottom: 12, paddingLeft: 0 }} onClick={() => ctx.nav("editor", { id })}><Icon name="chevL" size={15} /> Editor</button>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Reframe</div><h1 style={{ fontSize: 28 }}>Frame the speakers</h1></div>
        <span className="spacer" />
        <Seg value={mode} onChange={setMode} options={[{ value: "pan", label: "Pan", icon: "flip" }, { value: "split", label: "Split", icon: "layout" }, { value: "center", label: "Center", icon: "crop" }]} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 300px", gap: 22, alignItems: "start" }}>
        <div>
          <div ref={frameRef} className="card" style={{ position: "relative", aspectRatio: "16/9", overflow: "hidden", borderColor: "var(--line-str)" }}>
            <Thumb seed="reframe" kind="" label={false} />
            {detecting && <div style={{ position: "absolute", inset: 0, background: "rgba(8,9,11,0.55)", display: "grid", placeItems: "center", zIndex: 9 }}><div className="row" style={{ gap: 10, color: "var(--accent)" }}><Icon name="scan" size={20} style={{ animation: "pulse 1.2s infinite" }} /> detecting faces…</div></div>}
            {mode !== "center" && <ROIBox box={boxes.L} color="var(--roi-l)" label="L · speaker 1" containerRef={frameRef} onChange={(b) => { setBoxes((s) => ({ ...s, L: b })); setActive("L"); }} />}
            {mode !== "center" && <ROIBox box={boxes.R} color="var(--roi-r)" label="R · speaker 2" containerRef={frameRef} onChange={(b) => { setBoxes((s) => ({ ...s, R: b })); setActive("R"); }} />}
            {mode === "center" && <div style={{ position: "absolute", top: 0, bottom: 0, left: "31%", width: "38%", border: "2px solid var(--accent)", boxShadow: "0 0 0 9999px rgba(0,0,0,0.3)" }}><div style={{ position: "absolute", top: -22, left: 0, fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--accent)" }}>center crop 9:16</div></div>}
          </div>
          <div className="row" style={{ gap: 12, marginTop: 12 }}>
            <Icon name="film" size={15} style={{ color: "var(--text-faint)" }} />
            <input type="range" min="0" max="100" value={frame} onChange={(e) => setFrame(+e.target.value)} style={{ flex: 1, accentColor: "var(--accent)" }} />
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-dim)" }}>{fmtTC(20.7 + frame * 0.4)}</span>
          </div>
          <div className="row" style={{ gap: 10, marginTop: 14 }}>
            <Btn variant="primary" icon="scan" onClick={autoDetect}>Reset boxes</Btn>
            <span className="spacer" />
            <Btn variant="ghost" icon="refresh" onClick={applyReframe}>Apply &amp; re-render</Btn>
          </div>

          <div className="card" style={{ padding: 16, marginTop: 18 }}>
            <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Speaker track · diar⊕ROI</div><span className="spacer" />{trackSource && <span className="chip acc">{trackSource === "fused" ? "fused · diar⊕ROI" : "ROI-only"}</span>}</div>
            {segs.length ? (
              <div className="row" style={{ height: 34, borderRadius: 8, overflow: "hidden", gap: 2 }}>
                {segs.map((seg, i) => { const L = seg.speaker === "left"; return (
                  <div key={i} title={`${seg.speaker} · ${fmtTC(seg.start)}–${fmtTC(seg.end)}`}
                    style={{ flex: Math.max(0.5, seg.end - seg.start), background: L ? "color-mix(in srgb, var(--roi-l) 30%, var(--bg-3))" : "color-mix(in srgb, var(--roi-r) 30%, var(--bg-3))", borderTop: `2px solid ${L ? "var(--roi-l)" : "var(--roi-r)"}`, display: "grid", placeItems: "center", fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text)" }}>{L ? "L" : "R"}</div>
                ); })}
              </div>
            ) : (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No track yet — hit <b>Apply &amp; re-render</b> and the engine fuses audio diarization with ROI motion. (Editing the track by hand is Phase 2.)</div>
            )}
          </div>
        </div>

        <div style={{ position: "sticky", top: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card" style={{ padding: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>Live 9:16 preview</div>
            <div style={{ width: 140, margin: "0 auto", aspectRatio: "9/16", borderRadius: 10, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)" }}>
              <Thumb seed="reframe" vertical kind="" label={false} />
              <div style={{ position: "absolute", inset: 0, border: `2px solid ${active === "L" ? "var(--roi-l)" : "var(--roi-r)"}`, borderRadius: 10, transition: "border-color .2s" }} />
              <div style={{ position: "absolute", bottom: "18%", left: 0, right: 0, textAlign: "center", fontFamily: "var(--font-caption)", fontSize: 13, color: "#fff", textShadow: "0 2px 5px #000" }}>following <span style={{ color: "var(--caption-hl)" }}>{active === "L" ? "left" : "right"}</span></div>
            </div>
            <div className="row" style={{ gap: 8, marginTop: 12, justifyContent: "center" }}>
              <button className={"chip" + (active === "L" ? " acc" : "")} style={{ cursor: "pointer" }} onClick={() => setActive("L")}><span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--roi-l)" }} />Left</button>
              <button className={"chip" + (active === "R" ? " acc" : "")} style={{ cursor: "pointer" }} onClick={() => setActive("R")}><span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--roi-r)" }} />Right</button>
            </div>
          </div>

          <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
            <div className="row"><div className="eyebrow">Speaker switching</div><span className="spacer" /><span className="chip warn">Phase 2</span></div>
            <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>Min-dwell, smoothing and crop-margin are tuned automatically today. Manual control + an editable speaker track land in Phase 2 (the full ROI / speaker-track editor).</div>
          </div>

          {mode === "pan" && (
            <div className="card" style={{ padding: 14, borderColor: "var(--ok)", background: "var(--ok-soft)" }}>
              <div className="row" style={{ gap: 9, fontSize: 12.5 }}><Icon name="check" size={15} style={{ color: "var(--ok)" }} /><span>Single scene — fixed ROI boxes work great for pan.</span></div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
