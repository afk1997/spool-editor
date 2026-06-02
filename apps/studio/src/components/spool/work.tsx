"use client";

import { useState } from "react";
import { useSpool, type Candidate, type TranscriptLine, type SpeakerInfo } from "./context";
import { Btn, Chip, Empty, Icon, Thumb, fmtTC } from "./ui";

/* Shared work-screen components ported 1:1 from the demo (04): CandidateCard, AdjustModal,
 * DiscoveryBody, TranscriptView. Used by both Project (S4) and Discovery (S5).
 *
 * Glass-box adaptation: a Phase-1 candidate has real named `signals` + a real transcript
 * `excerpt` (the engine's `find_moments`), but NO numeric score / 5-factor breakdown — that
 * `rank` opportunity-score is Phase 3. So the card keeps the demo's chrome but the expandable
 * panel shows the real named signals instead of fabricated factor bars. */

function SignalPanel({ signals }: { signals: string[] }) {
  if (!signals.length) return <div style={{ padding: "4px 2px", fontSize: 12, color: "var(--text-faint)" }}>No named signals returned for this moment.</div>;
  return (
    <div style={{ padding: "4px 2px" }}>
      <div className="row" style={{ gap: 8, marginBottom: 10, color: "var(--accent-2)", fontSize: 11, fontWeight: 600 }}>
        <Icon name="zap" size={12} /> WHY IT SCORED — matched signals
      </div>
      <div className="kbar">
        {signals.map((s) => <span key={s} className="chip acc" style={{ height: 26 }}><Icon name="check" size={12} />{s}</span>)}
      </div>
    </div>
  );
}

export function CandidateCard({ c, selected, onToggle, onAdjust }: { c: Candidate; selected: boolean; onToggle: (id: string) => void; onAdjust?: (c: Candidate) => void }) {
  const [showSignals, setShowSignals] = useState(false);
  const [hover, setHover] = useState(false);
  return (
    <div className="card" style={{ overflow: "hidden", borderColor: selected ? "var(--accent)" : "var(--line)", transition: "border-color .15s" }}>
      <div className="row" style={{ alignItems: "stretch" }}>
        <div style={{ width: 150, flex: "none", position: "relative" }} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
          <Thumb seed={c.id} kind={c.mode} label={false}>
            <div className="tl"><span className="badge mono">{fmtTC(c.start)}</span></div>
            <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center" }}>
              <button className="roundbtn" style={{ width: 34, height: 34, opacity: hover ? 1 : 0.85 }}><Icon name="play" size={15} /></button>
            </div>
            {hover && <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: 3, background: "var(--bg-4)" }}><div style={{ height: "100%", width: "45%", background: "var(--accent)", animation: "stripe 1.5s linear infinite" }} /></div>}
          </Thumb>
        </div>
        <div className="grow" style={{ padding: "13px 15px", minWidth: 0 }}>
          <div className="row" style={{ gap: 9, marginBottom: 7 }}>
            <Chip tone="acc">{c.mode}</Chip>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{fmtTC(c.start)} → {fmtTC(c.end)} · {Math.round(c.end - c.start)}s</span>
            <span className="spacer" />
            <button className="chip" style={{ cursor: "pointer", color: "var(--accent-2)" }} onClick={() => setShowSignals((s) => !s)} aria-expanded={showSignals} title="Matched signals (glass-box)"><Icon name="star" size={12} />{c.signals.length}<Icon name={showSignals ? "chevD" : "chevR"} size={11} /></button>
          </div>
          <div style={{ fontWeight: 600, fontSize: 15.5, marginBottom: 7, fontFamily: "var(--font-display)" }}>{c.title}</div>
          <div className="row" style={{ gap: 8, marginBottom: 8, color: "var(--accent)", fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap" }}><Icon name="sparkles" size={13} />WHY THIS WORKS</div>
          <p style={{ margin: 0, color: "var(--text-dim)", fontSize: 13, lineHeight: 1.5 }}>{c.why}</p>
          {c.excerpt && <div style={{ marginTop: 10, padding: "9px 11px", borderLeft: "2px solid var(--line-str)", background: "var(--bg-1)", borderRadius: "0 8px 8px 0", fontSize: 12.5, color: "var(--text-dim)", fontStyle: "italic" }}>“{c.excerpt}”</div>}
          {showSignals && <div style={{ marginTop: 12 }}><SignalPanel signals={c.signals} /></div>}
        </div>
      </div>
      <div className="row" style={{ padding: "10px 14px", borderTop: "1px solid var(--line)", gap: 8, background: "var(--bg-1)" }}>
        <Btn variant="ghost" size="sm" icon="crop" onClick={() => onAdjust && onAdjust(c)}>Adjust in/out</Btn>
        <Btn variant="ghost" size="sm" icon="layers">Merge next</Btn>
        <span className="spacer" />
        <Btn variant={selected ? "primary" : "ghost"} size="sm" icon={selected ? "check" : "plus"} onClick={() => onToggle(c.id)}>{selected ? "Selected" : "Accept"}</Btn>
      </div>
    </div>
  );
}

export function AdjustModal({ c, onClose }: { c: Candidate; onClose: () => void }) {
  const [inP, setIn] = useState(c.start), [outP, setOut] = useState(c.end);
  return (
    <div className="overlay" onClick={onClose}>
      <div className="palette" style={{ width: "min(560px,92vw)", padding: 20 }} onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ marginBottom: 16 }}><h3 style={{ fontSize: 17 }}>Adjust “{c.title}”</h3><span className="spacer" /><button className="iconbtn" onClick={onClose}><Icon name="x" size={16} /></button></div>
        <div style={{ borderRadius: 10, overflow: "hidden", marginBottom: 16 }}><Thumb seed={c.id} kind={c.mode} label={false} /></div>
        <div style={{ position: "relative", height: 46, background: "var(--bg-3)", borderRadius: 8, marginBottom: 16, overflow: "hidden" }}>
          <div style={{ position: "absolute", top: 0, bottom: 0, left: "15%", right: "18%", background: "var(--accent-soft)", borderLeft: "2px solid var(--accent)", borderRight: "2px solid var(--accent)" }} />
          <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "space-around", fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-faint)" }}>{["waveform", "·", "·", "·", "·"].map((x, i) => <span key={i}>{x}</span>)}</div>
        </div>
        <div className="row" style={{ gap: 16, marginBottom: 18 }}>
          <div className="grow"><span className="field-label">In point</span><input className="input mono" value={fmtTC(inP)} onChange={() => setIn(inP)} /></div>
          <div className="grow"><span className="field-label">Out point</span><input className="input mono" value={fmtTC(outP)} onChange={() => setOut(outP)} /></div>
          <Btn variant="ghost" style={{ alignSelf: "flex-end" }}>Snap to sentence</Btn>
        </div>
        <div className="row" style={{ gap: 10, justifyContent: "flex-end" }}><Btn variant="ghost" onClick={onClose}>Cancel</Btn><Btn variant="primary" icon="check" onClick={onClose}>Save range</Btn></div>
      </div>
    </div>
  );
}

const MODES = ["All", "Funny", "Insightful", "Hot-take", "Story", "How-to", "Q&A"];

export function DiscoveryBody({ candidates, sourceId, finding }: { candidates: Candidate[]; sourceId: string; finding: boolean }) {
  const ctx = useSpool();
  const [mode, setMode] = useState("All");
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [adjust, setAdjust] = useState<Candidate | null>(null);

  const isSel = (c: Candidate) => overrides[c.id] ?? c.sel;
  const sel = candidates.filter(isSel);
  const toggle = (id: string) => setOverrides((o) => { const c = candidates.find((x) => x.id === id); return { ...o, [id]: !(o[id] ?? c?.sel ?? false) }; });
  const refind = (m: string) => { setMode(m); if (m !== "All") ctx.client.findMoments(sourceId, { mode: m.toLowerCase().replace("-", "") }).catch(() => {}); };
  const findMore = () => { ctx.client.findMoments(sourceId, { mode: mode === "All" ? "funny" : mode.toLowerCase().replace("-", "") }).catch(() => {}); ctx.pushToast({ icon: "sparkles", tone: "info", title: "Finding more moments", body: "Scanning the transcript…" }); };

  const view = mode === "All" ? candidates : candidates.filter((c) => c.mode.toLowerCase() === mode.toLowerCase());

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <div className="seg neutral" style={{ flexWrap: "wrap" }}>
          {MODES.map((m) => <button key={m} className={mode === m ? "on" : ""} onClick={() => (m === "All" ? setMode("All") : refind(m))}>{m}</button>)}
        </div>
        <div className="spacer" />
        <Btn variant="ghost" icon="refresh" onClick={findMore}>Find more</Btn>
      </div>

      {finding ? (
        <div>
          <div className="row" style={{ gap: 10, marginBottom: 16, color: "var(--accent)", fontSize: 13 }}>
            <Icon name="sparkles" size={16} style={{ animation: "pulse 1.5s infinite" }} /> Scanning transcript for {mode.toLowerCase()} moments…
          </div>
          {[0, 1, 2].map((i) => (
            <div key={i} className="card" style={{ display: "flex", gap: 14, padding: 13, marginBottom: 12 }}>
              <div className="skel" style={{ width: 130, height: 84, borderRadius: 8, flex: "none" }} />
              <div className="grow"><div className="skel" style={{ height: 16, width: "45%", marginBottom: 10 }} /><div className="skel" style={{ height: 12, width: "90%", marginBottom: 7 }} /><div className="skel" style={{ height: 12, width: "70%" }} /></div>
            </div>
          ))}
        </div>
      ) : view.length === 0 ? (
        <Empty icon="scan" title="No strong moments yet" action={<Btn variant="primary" icon="sparkles" onClick={findMore}>Find moments</Btn>}>
          Run discovery to scan this transcript for clip-worthy moments — punchlines, hot-takes, stories and more.
        </Empty>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingBottom: sel.length > 0 ? 80 : 0 }}>
          {view.map((c) => <CandidateCard key={c.id} c={c} selected={isSel(c)} onToggle={toggle} onAdjust={setAdjust} />)}
        </div>
      )}

      {sel.length > 0 && !finding && (
        <div style={{ position: "sticky", bottom: 0, marginTop: 14, padding: "12px 16px", background: "var(--bg-1)", border: "1px solid var(--line-str)", borderRadius: "var(--radius)", boxShadow: "var(--shadow-pop)", display: "flex", alignItems: "center", gap: 14 }}>
          <span className="chip solid">{sel.length}</span>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>candidates selected</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>≈ {Math.round(sel.reduce((a, c) => a + (c.end - c.start), 0))}s of footage</span>
          <span className="spacer" />
          <Btn variant="ghost" onClick={() => setOverrides(Object.fromEntries(candidates.map((c) => [c.id, true])))}>Select all</Btn>
          <Btn variant="primary" icon="scissors" onClick={() => ctx.makeClipsFrom(sel)}>Make {sel.length} clips →</Btn>
        </div>
      )}
      {adjust && <AdjustModal c={adjust} onClose={() => setAdjust(null)} />}
    </div>
  );
}

export function TranscriptView({ lines, speakers }: { lines: TranscriptLine[]; speakers: Record<string, SpeakerInfo> }) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState<string | null>(null);
  if (!lines.length) return <Empty icon="type" title="No transcript yet">Once this source finishes transcribing, the word-level transcript shows here.</Empty>;
  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 16 }}>
        <div className="cmdk" style={{ maxWidth: 280 }}><Icon name="search" size={15} /><input style={{ background: "transparent", border: 0, outline: "none", color: "var(--text)", flex: 1, fontFamily: "inherit" }} placeholder="Search transcript…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <div className="row" style={{ gap: 14, marginLeft: 6 }}>
          {Object.entries(speakers).map(([k, v]) => <span key={k} className="row" style={{ gap: 7, fontSize: 12.5 }}><span style={{ width: 9, height: 9, borderRadius: 3, background: v.color }} />{v.name}</span>)}
        </div>
        <span className="spacer" />
        <span style={{ fontSize: 12, color: "var(--text-faint)" }}>Select text → “create clip from selection”</span>
      </div>
      <div className="panel" style={{ padding: "8px 4px", maxWidth: 760 }}>
        {lines.map((line) => {
          const sp = speakers[line.sp] || { name: line.sp, color: "var(--accent)" };
          const match = q && line.words.toLowerCase().includes(q.toLowerCase());
          return (
            <div key={line.id} style={{ display: "flex", gap: 14, padding: "10px 16px", background: match ? "var(--accent-soft)" : "transparent", borderRadius: 8 }}>
              <div style={{ flex: "none", width: 96, textAlign: "right" }}>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>{fmtTC(line.t)}</div>
                <div style={{ fontSize: 11.5, fontWeight: 600, color: sp.color }}>{sp.name}</div>
              </div>
              <div style={{ lineHeight: 1.7, fontSize: 14 }}>
                {line.tokens.map((tk, i) => (
                  <span key={i} onClick={() => setActive(line.id + "-" + i)}
                    style={{ cursor: "pointer", padding: "1px 1px", borderRadius: 3, background: active === line.id + "-" + i ? "var(--accent)" : "transparent", color: active === line.id + "-" + i ? "var(--accent-ink)" : "inherit" }}
                    onMouseEnter={(e) => { if (active !== line.id + "-" + i) e.currentTarget.style.background = "var(--bg-3)"; }}
                    onMouseLeave={(e) => { if (active !== line.id + "-" + i) e.currentTarget.style.background = "transparent"; }}>{tk.w} </span>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
