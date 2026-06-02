"use client";

import { useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { Btn, Icon, Seg, Switch, Thumb, fmtTC } from "@/components/spool/ui";

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
  const [segments, setSegments] = useState([{ sp: "L", w: 22 }, { sp: "R", w: 14 }, { sp: "L", w: 30 }, { sp: "R", w: 18 }, { sp: "L", w: 16 }]);
  const [minDwell, setMinDwell] = useState(1.0);
  const frameRef = useRef<HTMLDivElement>(null);

  const autoDetect = () => { setDetecting(true); setTimeout(() => { setDetecting(false); setBoxes({ L: { x: 7, y: 20, w: 38, h: 62 }, R: { x: 55, y: 17, w: 39, h: 65 } }); ctx.pushToast({ icon: "scan", tone: "ok", title: "Faces detected", body: "2 speakers · cyan = left, magenta = right" }); }, 1300); };
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
            <Btn variant="primary" icon="scan" onClick={autoDetect}>Auto-detect</Btn>
            <Btn variant="ghost" icon="eye" onClick={() => ctx.pushToast({ icon: "eye", tone: "info", title: "Motion-diff preview", body: "Rendering who-is-talking overlay…" })}>Verify (motion diff)</Btn>
            <span className="spacer" />
            <Btn variant="ghost" icon="refresh" onClick={applyReframe}>Apply &amp; re-render</Btn>
          </div>

          <div className="card" style={{ padding: 16, marginTop: 18 }}>
            <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Speaker track · segments.json</div><span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>click a segment to flip speaker</span></div>
            <div className="row" style={{ height: 34, borderRadius: 8, overflow: "hidden", gap: 2 }}>
              {segments.map((seg, i) => (
                <div key={i} onClick={() => setSegments((s) => s.map((x, j) => (j === i ? { ...x, sp: x.sp === "L" ? "R" : "L" } : x)))}
                  style={{ flex: seg.w, background: seg.sp === "L" ? "color-mix(in srgb, var(--roi-l) 30%, var(--bg-3))" : "color-mix(in srgb, var(--roi-r) 30%, var(--bg-3))", borderTop: `2px solid ${seg.sp === "L" ? "var(--roi-l)" : "var(--roi-r)"}`, display: "grid", placeItems: "center", cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text)" }}>{seg.sp}</div>
              ))}
            </div>
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

          <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
            <div className="eyebrow">Speaker switching</div>
            <div>
              <div className="row" style={{ marginBottom: 6 }}><span className="field-label" style={{ margin: 0 }}>Min dwell</span><span className="spacer" /><span className="mono" style={{ fontSize: 12 }}>{minDwell.toFixed(1)}s</span></div>
              <input type="range" min="0.4" max="3" step="0.1" value={minDwell} onChange={(e) => setMinDwell(+e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} />
              <div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", marginTop: 5 }}>hold on a speaker ≥ {minDwell.toFixed(1)}s before cutting</div>
            </div>
            <div className="row"><span style={{ fontSize: 13 }}>Smoothing</span><span className="spacer" /><Switch on onClick={() => {}} /></div>
            <div className="row"><span style={{ fontSize: 13 }}>Crop margin</span><span className="spacer" /><span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>8%</span></div>
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
