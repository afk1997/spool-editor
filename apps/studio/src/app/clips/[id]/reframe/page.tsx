"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { Btn, Icon, Seg, fmtTC } from "@spool/ui";

/* S7 Reframe / ROI editor — 1:1 port of the demo (05), fully wired (Phase 2).
 * Real cut-clip video with draggable ROI boxes + a real scrub; an editable diar⊕ROI
 * speaker track (click a segment to flip L/R); real min-dwell / smoothing / crop-margin
 * knobs; a live 9:16 preview that plays the actual reframed render. Everything POSTs to
 * the real /clips/<id>/reframe endpoint (fractional ROIs, knobs, edited segments). */

interface Box { x: number; y: number; w: number; h: number }
interface Seg2 { start: number; end: number; speaker: string }

const DEFAULT_BOXES: { L: Box; R: Box } = { L: { x: 6, y: 18, w: 40, h: 64 }, R: { x: 54, y: 16, w: 40, h: 66 } };

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

/** A labelled tuning slider (min-dwell / smoothing / crop-margin) mapped to real engine params. */
function Knob({ label, value, min, max, step, fmt, onChange }: { label: string; value: number; min: number; max: number; step: number; fmt: (v: number) => string; onChange: (v: number) => void }) {
  return (
    <div>
      <div className="row" style={{ marginBottom: 4 }}>
        <span style={{ fontSize: 12.5 }}>{label}</span><span className="spacer" />
        <span className="mono" style={{ fontSize: 11.5, color: "var(--text-dim)" }}>{fmt(value)}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(+e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} />
    </div>
  );
}

export default function ReframeScreen() {
  const ctx = useSpool();
  const id = String(useParams().id);
  const [mode, setMode] = useState("pan");
  const [boxes, setBoxes] = useState<{ L: Box; R: Box }>(DEFAULT_BOXES);
  const [active, setActive] = useState<"L" | "R">("L");
  // S7 tuning knobs → real reframe params (clamped engine-side).
  const [minDwell, setMinDwell] = useState(1.0);
  const [smoothing, setSmoothing] = useState(15);
  const [cropMargin, setCropMargin] = useState(0);
  const frameRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [cur, setCur] = useState(0);
  const [dur, setDur] = useState(0);
  const [videoOk, setVideoOk] = useState(true);

  // the REAL diar⊕ROI track from the clip's latest reframe job, now editable in-place.
  const reframeJob = (ctx.snapshot?.clips ?? []).filter((c) => c.clip_id === id && c.kind === "reframe" && c.status === "done" && ((c.result.segments as Seg2[] | undefined)?.length ?? 0) > 0).at(-1);
  const segs = (reframeJob?.result.segments as Seg2[] | undefined) ?? [];
  const trackSource = (reframeJob?.result.source as string) || "";
  const [edited, setEdited] = useState<Seg2[] | null>(null);
  // a fresh reframe job supersedes the working copy — show its real track.
  const lastJob = useRef<string | undefined>(undefined);
  useEffect(() => { if (reframeJob?.id !== lastJob.current) { lastJob.current = reframeJob?.id; setEdited(null); } }, [reframeJob?.id]);
  const track = edited ?? segs;

  const clipUrl = ctx.client.clipArtifactUrl(id, "clip");
  const reframedUrl = reframeJob ? `${ctx.client.clipArtifactUrl(id, "reframed")}?v=${reframeJob.id}` : null;

  const flip = (i: number) => {
    const base = (edited ?? segs).map((s) => ({ ...s }));
    if (!base[i]) return;
    base[i] = { ...base[i], speaker: base[i].speaker === "left" ? "right" : "left" };
    setEdited(base);
  };

  const reframeParams = () => {
    const pct = (b: Box) => ({ x: b.x / 100, y: b.y / 100, w: b.w / 100, h: b.h / 100 });
    return {
      aspect: "9:16", mode, min_dwell: minDwell, smoothing, crop_margin: cropMargin,
      ...(mode !== "center" ? { rois: { left: pct(boxes.L), right: pct(boxes.R) } } : {}),
      ...(edited ? { segments: edited.map((s) => ({ start: s.start, end: s.end, speaker: s.speaker === "left" ? "left" as const : "right" as const })) } : {}),
    };
  };

  // "Verify" recomputes + shows the track without leaving; "Apply" proceeds to captions.
  const submit = (proceed: boolean) => {
    ctx.client.reframe(id, reframeParams()).catch(() => {});
    if (proceed) {
      ctx.pushToast({ icon: "refresh", tone: "info", title: "Reframing", body: `${mode} · 9:16 · track in the queue` });
      ctx.nav("caption", { id });
    } else {
      ctx.pushToast({ icon: "scan", tone: "info", title: "Recomputing diar⊕ROI", body: "The speaker track updates here when the job finishes." });
    }
  };

  const autoDetect = () => { setBoxes(DEFAULT_BOXES); ctx.pushToast({ icon: "scan", tone: "info", title: "Boxes reset", body: "Adjust the L/R boxes, then Apply — the engine refines detection on render." }); };

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
          <div ref={frameRef} className="card" style={{ position: "relative", aspectRatio: "16/9", overflow: "hidden", borderColor: "var(--line-str)", background: "#0a0b0d" }}>
            {videoOk
              ? <video ref={videoRef} src={clipUrl} muted playsInline preload="metadata" onLoadedMetadata={(e) => setDur(e.currentTarget.duration || 0)} onTimeUpdate={(e) => setCur(e.currentTarget.currentTime)} onError={() => setVideoOk(false)} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain", background: "#000" }} />
              : <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--text-faint)", fontSize: 12.5 }}>cut the clip to load its frame</div>}
            {mode !== "center" && <ROIBox box={boxes.L} color="var(--roi-l)" label="L · speaker 1" containerRef={frameRef} onChange={(b) => { setBoxes((s) => ({ ...s, L: b })); setActive("L"); }} />}
            {mode !== "center" && <ROIBox box={boxes.R} color="var(--roi-r)" label="R · speaker 2" containerRef={frameRef} onChange={(b) => { setBoxes((s) => ({ ...s, R: b })); setActive("R"); }} />}
            {mode === "center" && <div style={{ position: "absolute", top: 0, bottom: 0, left: "31%", width: "38%", border: "2px solid var(--accent)", boxShadow: "0 0 0 9999px rgba(0,0,0,0.3)" }}><div style={{ position: "absolute", top: -22, left: 0, fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--accent)" }}>center crop 9:16</div></div>}
          </div>
          <div className="row" style={{ gap: 12, marginTop: 12 }}>
            <Icon name="film" size={15} style={{ color: "var(--text-faint)" }} />
            <input type="range" min={0} max={dur || 0.001} step={0.04} value={Math.min(cur, dur || 0)} onChange={(e) => { const t = +e.target.value; setCur(t); if (videoRef.current) videoRef.current.currentTime = t; }} style={{ flex: 1, accentColor: "var(--accent)" }} />
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-dim)" }}>{fmtTC(cur)}</span>
          </div>
          <div className="row" style={{ gap: 10, marginTop: 14 }}>
            <Btn variant="primary" icon="scan" onClick={autoDetect}>Auto-detect</Btn>
            <Btn variant="ghost" icon="check" onClick={() => submit(false)}>Verify diar⊕ROI</Btn>
            <span className="spacer" />
            <Btn variant="ghost" icon="refresh" onClick={() => submit(true)}>Apply &amp; re-render</Btn>
          </div>

          <div className="card" style={{ padding: 16, marginTop: 18 }}>
            <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Speaker track · diar⊕ROI</div><span className="spacer" />{trackSource && <span className="chip acc">{trackSource === "fused" ? "fused · diar⊕ROI" : trackSource === "manual" ? "edited by hand" : "ROI-only"}</span>}</div>
            {track.length ? (
              <>
                <div className="row" style={{ height: 34, borderRadius: 8, overflow: "hidden", gap: 2 }}>
                  {track.map((seg, i) => { const L = seg.speaker === "left"; return (
                    <button key={i} title={`${seg.speaker} · ${fmtTC(seg.start)}–${fmtTC(seg.end)} · click to flip`} onClick={() => flip(i)}
                      style={{ flex: Math.max(0.5, seg.end - seg.start), background: L ? "color-mix(in srgb, var(--roi-l) 30%, var(--bg-3))" : "color-mix(in srgb, var(--roi-r) 30%, var(--bg-3))", borderTop: `2px solid ${L ? "var(--roi-l)" : "var(--roi-r)"}`, display: "grid", placeItems: "center", fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text)", cursor: "pointer", border: "none" }}>{L ? "L" : "R"}</button>
                  ); })}
                </div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 8 }}>Click a segment to flip the speaker{edited ? " · edited — Apply to re-render" : ""}.</div>
              </>
            ) : (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No track yet — hit <b>Verify diar⊕ROI</b> and the engine fuses audio diarization with ROI motion. Then drag/flip the segments and Apply.</div>
            )}
          </div>
        </div>

        <div style={{ position: "sticky", top: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card" style={{ padding: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>Live 9:16 preview</div>
            <div style={{ width: 140, margin: "0 auto", aspectRatio: "9/16", borderRadius: 10, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)", background: "#000" }}>
              {reframedUrl
                ? <video key={reframedUrl} src={reframedUrl} muted loop autoPlay playsInline style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                : <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--text-faint)", fontSize: 11, textAlign: "center", padding: 10 }}>Apply to render a 9:16 preview</div>}
              <div style={{ position: "absolute", inset: 0, border: `2px solid ${active === "L" ? "var(--roi-l)" : "var(--roi-r)"}`, borderRadius: 10, transition: "border-color .2s", pointerEvents: "none" }} />
            </div>
            <div className="row" style={{ gap: 8, marginTop: 12, justifyContent: "center" }}>
              <button className={"chip" + (active === "L" ? " acc" : "")} style={{ cursor: "pointer" }} onClick={() => setActive("L")}><span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--roi-l)" }} />Left</button>
              <button className={"chip" + (active === "R" ? " acc" : "")} style={{ cursor: "pointer" }} onClick={() => setActive("R")}><span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--roi-r)" }} />Right</button>
            </div>
          </div>

          <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
            <div className="eyebrow">Speaker switching</div>
            <Knob label="Min-dwell" value={minDwell} min={0.3} max={3} step={0.1} fmt={(v) => `${v.toFixed(1)}s`} onChange={setMinDwell} />
            <Knob label="Smoothing" value={smoothing} min={1} max={61} step={2} fmt={(v) => `${v} frames`} onChange={setSmoothing} />
            <Knob label="Crop margin" value={cropMargin} min={0} max={0.5} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`} onChange={setCropMargin} />
            <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>Tuning the speaker pan: longer dwell + heavier smoothing = fewer cuts; crop-margin zooms the pan in.</div>
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
