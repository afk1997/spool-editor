"use client";

import { useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { EnergyWave } from "./energy";

/* Editor timeline (S6) — a real, multi-lane, time-aligned view of the clip window, matching the
 * approved editor design:
 *   Video (thumbnail filmstrip) · Captions (word pills, ✕ ripple-cut) · Speaker (L/R from
 *   diarization) · Energy (loudness waveform) · Scenes (cut ticks) — over a click-to-seek playhead,
 *   a draggable minimap scrollbar with zoom, and trim handles → re-cut.
 * Every lane is real engine data (filmstrip, transcript speakers, signals.energy_envelope/
 * scene_cuts) — never decorative. Editing is additive to the existing render / A-B / ripple flow. */

const LABEL_W = 64;
const SPEAKER_COLORS = ["#5fb6a8", "#d98aa8", "#7c89c8", "#c8a86a"];   // L=teal, R=pink, …
const DOTS: Record<string, string> = {
  Video: "#8a8a8a", Captions: "#e6b800", Speaker: "#5fb6a8", Energy: "#86c9a8", Scenes: "#c8a86a",
};

interface TLWord { idx: number; w: string; start: number | null; end: number | null; speaker?: string | null }

function LaneRow({ name, h, children }: { name: string; h: number; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, height: h }}>
      <div className="row" style={{ width: LABEL_W, flex: "none", gap: 6, fontSize: 11, color: "var(--text-dim)" }}>
        <span style={{ width: 7, height: 7, borderRadius: 999, background: DOTS[name] ?? "var(--text-faint)", flex: "none" }} />
        {name}
      </div>
      <div style={{ position: "relative", flex: 1, height: "100%", background: "var(--bg-2)", borderRadius: 4, overflow: "hidden" }}>{children}</div>
    </div>
  );
}

interface SpeakerSeg { start: number; end: number; speaker: string | null }

export function Timeline({
  words, segments, lo, hi, cur, onSeek, onDeleteWord, mutationPending, energyBars, sceneCuts, filmstrip, onTrim,
}: {
  words: TLWord[];
  segments: SpeakerSeg[];                    // diarization turns (real speaker data lives here, not on words)
  lo: number;
  hi: number;
  cur: number;                              // playhead, clip-relative seconds
  onSeek: (rel: number) => void;            // seek the preview (clip-relative seconds)
  onDeleteWord: (idx: number) => void;
  mutationPending?: boolean;
  energyBars: number[];
  sceneCuts: number[];                      // absolute source seconds
  filmstrip: string | null;                 // data:image/jpeg URI, or null
  onTrim: (absStart: number, absEnd: number) => void;
}) {
  const D = Math.max(0.001, hi - lo);
  const innerRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [scrollFrac, setScrollFrac] = useState(0);          // 0..1 left edge of the viewport
  const [trim, setTrim] = useState<{ inRel: number; outRel: number } | null>(null);
  const inRel = trim?.inRel ?? 0;
  const outRel = trim?.outRel ?? D;
  const trimmed = inRel > 0.1 || outRel < D - 0.1;
  const pct = (rel: number) => `${Math.max(0, Math.min(100, (rel / D) * 100))}%`;
  // Too many words for the current zoom → text pills would overlap into noise, so show ticks instead.
  const capDense = words.length / zoom > 50;

  const seekAt = (e: ReactMouseEvent) => {
    const el = innerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(D, ((e.clientX - r.left) / r.width) * D)));
  };

  // Speaker runs — the diarization turns. Real speaker labels live on SEGMENTS (words carry none),
  // so prefer segments (clamped to the clip window); fall back to per-word speaker if absent.
  let runs: { sp: string; s: number; e: number }[] = [];
  const segRuns = segments
    .filter((sg) => sg.speaker && sg.end > lo && sg.start < hi)
    .map((sg) => ({ sp: sg.speaker as string, s: Math.max(0, sg.start - lo), e: Math.min(D, sg.end - lo) }));
  if (segRuns.length) {
    runs = segRuns;
  } else {
    for (const w of words) {
      if (w.start == null || !w.speaker) continue;
      const sp = w.speaker;
      const s = w.start - lo, e = (w.end ?? w.start) - lo;
      const last = runs[runs.length - 1];
      if (last && last.sp === sp && s - last.e < 1.2) last.e = e;
      else runs.push({ sp, s, e });
    }
  }
  // Deterministic color per speaker name (sorted), so "Speaker 1"→teal, "Speaker 2"→pink, stable.
  const spIdx: Record<string, number> = {};
  [...new Set(runs.map((r) => r.sp))].sort().forEach((k, i) => (spIdx[k] = i));

  // Minimap: viewport width = 1/zoom; dragging it scrolls the lanes.
  const viewW = 1 / zoom;
  const onScroll = () => {
    const el = scrollRef.current;
    if (el && el.scrollWidth > el.clientWidth) setScrollFrac(el.scrollLeft / (el.scrollWidth - el.clientWidth));
  };
  const dragMini = (e: ReactMouseEvent) => {
    const bar = e.currentTarget as HTMLDivElement;
    const move = (clientX: number) => {
      const r = bar.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width - viewW / 2)) / (1 - viewW || 1);
      const el = scrollRef.current;
      if (el) el.scrollLeft = Math.max(0, Math.min(1, f)) * (el.scrollWidth - el.clientWidth);
    };
    move(e.clientX);
    const mm = (ev: MouseEvent) => move(ev.clientX);
    const up = () => { window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", mm);
    window.addEventListener("mouseup", up);
  };

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 9 }}>
        <span className="eyebrow">Timeline</span>
        <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>snap: word · {words.length} words{capDense ? " · zoom in to read" : ""}</span>
        <span className="spacer" />
        {trimmed && (
          <button className="btn primary sm" style={{ height: 22, marginRight: 4 }}
            disabled={mutationPending}
            onClick={() => onTrim(lo + inRel, lo + outRel)}>Re-cut to trim ({Math.round(outRel - inRel)}s)</button>
        )}
        <button className="iconbtn" style={{ width: 22, height: 22 }} title="zoom out" onClick={() => setZoom((z) => Math.max(1, +(z - 0.5).toFixed(1)))}>−</button>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-faint)", width: 26, textAlign: "center" }}>{zoom.toFixed(1)}×</span>
        <button className="iconbtn" style={{ width: 22, height: 22 }} title="zoom in" onClick={() => setZoom((z) => Math.min(8, +(z + 0.5).toFixed(1)))}>+</button>
      </div>

      <div ref={scrollRef} onScroll={onScroll} style={{ overflowX: zoom > 1 ? "auto" : "hidden", overflowY: "hidden" }}>
        {/* the label column is INSIDE the scroller but pinned so it stays put while tracks scroll */}
        <div ref={innerRef} onClick={seekAt} style={{ position: "relative", width: `${zoom * 100}%`, display: "flex", flexDirection: "column", gap: 6, cursor: "text" }}>
          <LaneRow name="Video" h={42}>
            {filmstrip
              ? <div style={{ position: "absolute", inset: 0, backgroundImage: `url(${filmstrip})`, backgroundSize: "100% 100%", opacity: 0.9 }} />
              : <div className="mono" style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", fontSize: 10, color: "var(--text-faint)" }}>frames…</div>}
          </LaneRow>

          <LaneRow name="Captions" h={26}>
            {/* Dense windows (many words at low zoom) would overlap into noise as text pills, so
                render clean word ticks then; zoom in (or short clips) reveal readable pills. */}
            {capDense
              ? words.map((w) => w.start == null ? null : (
                  <div key={w.idx} title={w.w}
                    onClick={(e) => { e.stopPropagation(); onSeek((w.start as number) - lo); }}
                    style={{ position: "absolute", left: pct((w.start as number) - lo), top: 5, bottom: 5, width: 2, background: "var(--text-faint)", borderRadius: 2, opacity: 0.6, cursor: "pointer" }} />
                ))
              : words.map((w) => w.start == null ? null : (
                  <div key={w.idx} title={w.w}
                    onClick={(e) => { e.stopPropagation(); onSeek((w.start as number) - lo); }}
                    style={{ position: "absolute", left: pct((w.start as number) - lo), top: 4, height: 18, maxWidth: 90, display: "inline-flex", alignItems: "center", gap: 2, padding: "0 5px", fontSize: 10.5, background: "#fff", border: "1px solid var(--line)", borderRadius: 5, whiteSpace: "nowrap", overflow: "hidden", cursor: "pointer" }}>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{w.w}</span>
                    <button title="delete word (ripple-cut on Re-cut)" disabled={mutationPending} onClick={(e) => { e.stopPropagation(); onDeleteWord(w.idx); }}
                      style={{ border: 0, background: "transparent", color: "var(--text-faint)", cursor: mutationPending ? "not-allowed" : "pointer", fontSize: 11, lineHeight: 1, padding: 0 }}>×</button>
                  </div>
                ))}
          </LaneRow>

          <LaneRow name="Speaker" h={16}>
            {runs.map((r, i) => (
              <div key={i} title={`Speaker ${r.sp}`}
                style={{ position: "absolute", left: pct(r.s), width: pct(r.e - r.s), top: 0, bottom: 0, background: SPEAKER_COLORS[spIdx[r.sp]! % SPEAKER_COLORS.length], opacity: 0.85, borderRight: "2px solid var(--bg-1)" }} />
            ))}
          </LaneRow>

          <LaneRow name="Energy" h={30}>
            {energyBars.length > 0 && (
              <div style={{ position: "absolute", inset: "2px 4px" }}>
                <EnergyWave bars={energyBars} height={26} color="#86c9a8" barGap={1} />
              </div>
            )}
          </LaneRow>

          <LaneRow name="Scenes" h={14}>
            {sceneCuts.map((t, i) => {
              const rel = t - lo;
              return rel < 0 || rel > D ? null : (
                <div key={i} title={`scene cut @ ${rel.toFixed(1)}s`} style={{ position: "absolute", left: pct(rel), top: 1, bottom: 1, width: 2, background: "#c8a86a", borderRadius: 2 }} />
              );
            })}
          </LaneRow>

          {/* playhead + trim-out shade over the TRACK columns (offset past the label column) */}
          <div style={{ position: "absolute", left: LABEL_W + 8, right: 0, top: 0, bottom: 0, pointerEvents: "none" }}>
            {inRel > 0 && <div style={{ position: "absolute", left: 0, width: pct(inRel), top: 0, bottom: 0, background: "rgba(20,22,28,0.5)", borderRadius: 4 }} />}
            {outRel < D && <div style={{ position: "absolute", left: pct(outRel), right: 0, top: 0, bottom: 0, background: "rgba(20,22,28,0.5)", borderRadius: 4 }} />}
            <div style={{ position: "absolute", left: pct(cur), top: -2, bottom: -2, width: 2, background: "var(--text)", boxShadow: "0 0 0 1px rgba(255,255,255,.4)" }}>
              <div style={{ position: "absolute", top: -4, left: -3, width: 8, height: 8, borderRadius: 999, background: "var(--text)" }} />
            </div>
          </div>
        </div>
      </div>

      {/* minimap scrollbar — viewport region reflects zoom; drag to scroll */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10 }}>
        <div style={{ width: LABEL_W, flex: "none" }} />
        <div onMouseDown={dragMini} style={{ position: "relative", flex: 1, height: 8, background: "var(--bg-3)", borderRadius: 999, cursor: zoom > 1 ? "grab" : "default" }}>
          <div style={{ position: "absolute", top: 0, bottom: 0, left: `${scrollFrac * (1 - viewW) * 100}%`, width: `${viewW * 100}%`, background: "var(--accent)", borderRadius: 999, opacity: 0.8 }} />
        </div>
      </div>

      {/* trim handles → set a new [in, out] window for Re-cut */}
      <div className="row" style={{ gap: 8, marginTop: 10, alignItems: "center" }}>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-faint)", width: LABEL_W, textAlign: "right", flex: "none" }}>trim</span>
        <input type="range" min={0} max={D} step={0.1} value={inRel} aria-label="trim in"
          onChange={(e) => setTrim({ inRel: Math.min(+e.target.value, outRel - 0.5), outRel })}
          style={{ flex: 1, accentColor: "var(--accent)" }} />
        <input type="range" min={0} max={D} step={0.1} value={outRel} aria-label="trim out"
          onChange={(e) => setTrim({ inRel, outRel: Math.max(+e.target.value, inRel + 0.5) })}
          style={{ flex: 1, accentColor: "var(--accent)" }} />
        <span className="mono" style={{ fontSize: 10, color: "var(--text-faint)", width: 92, textAlign: "right", flex: "none" }}>{inRel.toFixed(1)}–{outRel.toFixed(1)}s</span>
      </div>
    </div>
  );
}
