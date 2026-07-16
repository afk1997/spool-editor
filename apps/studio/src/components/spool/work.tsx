"use client";

import { useEffect, useRef, useState } from "react";
import type { RankFactors } from "@spool/types";
import { useSpool, scoreFromFactors, ENGINE_DEFAULT_WEIGHTS, type Candidate, type TranscriptLine, type SpeakerInfo } from "./context";
import { Btn, Chip, Empty, Icon, Thumb, fmtTC, parseTC } from "@spool/ui";
import { formatActionError } from "@/lib/action-error";
import { WindowList } from "./virtual";

// Past this many speaker-lines the transcript windows (only the visible lines mount — a
// one-hour video is thousands of word-nodes); below it the exact original markup renders, so
// every transcript the demo + tests show is byte-identical. The windowed path reuses the same
// per-line renderer, so the rows look the same either way (spec §6.4).
const LINE_VIRT_THRESHOLD = 60;

/* Shared work-screen components ported 1:1 from the demo (04): ScoreBar, CandidateCard,
 * AdjustModal, DiscoveryBody, TranscriptView. Used by both Project (S4) and Discovery (S5).
 *
 * Glass-box (Phase 3): the candidate carries a real `score` that decomposes into the five named,
 * reweightable `factors` from the engine's `moments.rank` (hook / self-contained / arc / energy /
 * length-fit — replacing the demo's mock factor set), surfaced as the ScoreBar + the Reweight
 * panel below. The expandable panel ALSO keeps the real matched `signals` cues. Nothing fabricated:
 * the score and every factor come from the engine; reweighting recomputes the same transparent sum
 * the engine uses (`scoreFromFactors`), so the slider is instant (spec §6.4) and mirrors the
 * engine's ordering, integer-rounded for display. */

// Factor key → label + bar color (single source for the ScoreBar AND the Reweight sliders).
const FACTOR_META: { key: keyof RankFactors; label: string; color: string }[] = [
  { key: "hook", label: "Hook", color: "var(--accent)" },
  { key: "self_contained", label: "Self-contained", color: "var(--info)" },
  { key: "arc", label: "Arc", color: "var(--warn)" },
  { key: "energy", label: "Energy", color: "var(--ok)" },
  { key: "length_fit", label: "Length-fit", color: "#b98cff" },
  { key: "boundary_quality", label: "Boundary", color: "#6fb1ff" },
];

function ScoreBar({ factors }: { factors: RankFactors }) {
  const f = factors as Record<string, number | undefined>;
  const present = FACTOR_META.filter((m) => f[m.key] != null);
  if (!present.length) return null;
  return (
    <div style={{ padding: "4px 2px" }}>
      <div className="row" style={{ height: 9, borderRadius: 6, overflow: "hidden", marginBottom: 9 }}>
        {present.map((m) => <div key={m.key} style={{ flex: f[m.key] ?? 0, background: m.color }} />)}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "5px 14px" }}>
        {present.map((m) => (
          <div key={m.key} className="row" style={{ gap: 7, fontSize: 11.5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: m.color }} />
            <span style={{ color: "var(--text-dim)" }}>{m.label}</span>
            <span className="spacer" /><span className="mono">{Math.round((f[m.key] ?? 0) * 100)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

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

export function CandidateCard({ c, selected, onToggle, onAdjust, onMerge, dynScore }: { c: Candidate; selected: boolean; onToggle: (id: string) => void; onAdjust?: (c: Candidate) => void; onMerge?: (c: Candidate) => void; dynScore?: number }) {
  const [showScore, setShowScore] = useState(false);
  // Only label a value as a score when the engine returned one (or the user is viewing a real
  // factor reweight). Signal count is useful evidence, but it is not an opportunity score.
  const headline = dynScore ?? (c.score != null ? Math.round(c.score) : undefined);
  return (
    <div className="card" style={{ overflow: "hidden", borderColor: selected ? "var(--accent)" : "var(--line)", transition: "border-color .15s" }}>
      <div className="row" style={{ alignItems: "stretch" }}>
        <div style={{ width: 150, flex: "none", position: "relative" }}>
          <Thumb seed={c.id} kind={c.mode} label={false}>
            <div className="tl"><span className="badge mono">{fmtTC(c.start)}</span></div>
          </Thumb>
        </div>
        <div className="grow" style={{ padding: "13px 15px", minWidth: 0 }}>
          <div className="row" style={{ gap: 9, marginBottom: 7 }}>
            <Chip tone="acc">{c.mode}</Chip>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{fmtTC(c.start)} → {fmtTC(c.end)} · {Math.round(c.end - c.start)}s</span>
            <span className="spacer" />
            {headline != null && <button className="chip" style={{ cursor: "pointer", color: "var(--accent-2)" }} onClick={() => setShowScore((s) => !s)} aria-expanded={showScore} title="Score & factors (glass-box)"><Icon name="star" size={12} />{headline}<Icon name={showScore ? "chevD" : "chevR"} size={11} /></button>}
          </div>
          <div style={{ fontWeight: 600, fontSize: 15.5, marginBottom: 7, fontFamily: "var(--font-display)" }}>{c.title}</div>
          <div className="row" style={{ gap: 8, marginBottom: 8, color: "var(--accent)", fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap" }}><Icon name="sparkles" size={13} />WHY THIS WORKS</div>
          <p style={{ margin: 0, color: "var(--text-dim)", fontSize: 13, lineHeight: 1.5 }}>{c.why}</p>
          {c.excerpt && <div style={{ marginTop: 10, padding: "9px 11px", borderLeft: "2px solid var(--line-str)", background: "var(--bg-1)", borderRadius: "0 8px 8px 0", fontSize: 12.5, color: "var(--text-dim)", fontStyle: "italic" }}>“{c.excerpt}”</div>}
          {showScore && (
            <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
              {c.factors && <ScoreBar factors={c.factors} />}
              <SignalPanel signals={c.signals} />
            </div>
          )}
        </div>
      </div>
      <div className="row" style={{ padding: "10px 14px", borderTop: "1px solid var(--line)", gap: 8, background: "var(--bg-1)" }}>
        {onAdjust && <Btn variant="ghost" size="sm" icon="crop" onClick={() => onAdjust(c)}>Adjust in/out</Btn>}
        <Btn variant="ghost" size="sm" icon="layers" onClick={() => onMerge?.(c)} disabled={!onMerge} title={onMerge ? "Extend this clip to include the next moment" : "No moment after this one to merge"}>Merge next</Btn>
        <span className="spacer" />
        <Btn variant={selected ? "primary" : "ghost"} size="sm" icon={selected ? "check" : "plus"} onClick={() => onToggle(c.id)}>{selected ? "Selected" : "Accept"}</Btn>
      </div>
    </div>
  );
}

export function AdjustModal({ c, onClose, onSave }: { c: Candidate; onClose: () => void; onSave: (start: number, end: number) => void }) {
  const [inText, setInText] = useState(fmtTC(c.start));
  const [outText, setOutText] = useState(fmtTC(c.end));
  const inP = parseTC(inText), outP = parseTC(outText);
  const valid = Number.isFinite(inP) && Number.isFinite(outP) && outP > inP;
  const save = () => { if (valid) onSave(inP, outP); onClose(); };
  return (
    <div className="overlay" onClick={onClose}>
      <div className="palette" style={{ width: "min(560px,92vw)", padding: 20 }} onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ marginBottom: 16 }}><h3 style={{ fontSize: 17 }}>Adjust “{c.title}”</h3><span className="spacer" /><button className="iconbtn" aria-label="Close range adjustment" onClick={onClose}><Icon name="x" size={16} /></button></div>
        <div style={{ borderRadius: 10, overflow: "hidden", marginBottom: 16 }}><Thumb seed={c.id} kind={c.mode} label={false} /></div>
        <div className="row" style={{ gap: 16, marginBottom: 8 }}>
          <div className="grow"><span className="field-label">In point</span><input className="input mono" value={inText} onChange={(e) => setInText(e.target.value)} placeholder="MM:SS:FF" /></div>
          <div className="grow"><span className="field-label">Out point</span><input className="input mono" value={outText} onChange={(e) => setOutText(e.target.value)} placeholder="MM:SS:FF" /></div>
          <Btn variant="ghost" style={{ alignSelf: "flex-end" }} onClick={() => { setInText(fmtTC(c.start)); setOutText(fmtTC(c.end)); }}>Reset</Btn>
        </div>
        <div className="mono" style={{ fontSize: 11, color: valid ? "var(--text-faint)" : "var(--err)", marginBottom: 14, minHeight: 14 }}>{valid ? `${Math.round(outP - inP)}s clip` : "Out must be after In (MM:SS:FF)"}</div>
        <div className="row" style={{ gap: 10, justifyContent: "flex-end" }}><Btn variant="ghost" onClick={onClose}>Cancel</Btn><Btn variant="primary" icon="check" onClick={save} disabled={!valid}>Save range</Btn></div>
      </div>
    </div>
  );
}

const MODES = ["All", "Funny", "Insightful", "Hot-take", "Story", "How-to", "Q&A"];
// The engine mode strings (canonical, hyphens kept — they key clip.moments._MODE_GUIDES, and the
// candidate's mode round-trips back through these so the tab filter matches).
const ENGINE_MODES = MODES.slice(1).map((m) => m.toLowerCase());

export function DiscoveryBody({ candidates, sourceId, finding }: { candidates: Candidate[]; sourceId: string; finding: boolean }) {
  const ctx = useSpool();
  const [mode, setMode] = useState("All");
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [ranges, setRanges] = useState<Record<string, { start: number; end: number }>>({});
  const [adjust, setAdjust] = useState<Candidate | null>(null);
  const [showRank, setShowRank] = useState(false);
  const scanInFlight = useRef(false);
  const makeInFlight = useRef(false);
  const [scanning, setScanning] = useState(false);
  const [making, setMaking] = useState(false);
  // Slider weights for the glass-box reweight, seeded from the engine's DEFAULT_WEIGHTS (integer
  // ratios that normalize to the engine's .30/.25/.20/.15/.10) so toggling "Rank by score" matches
  // the engine's score/order at the initial state. They drive scoreFromFactors — the same
  // transparent sum the engine uses.
  const [weights, setWeights] = useState<Record<string, number>>(() => ({ ...ENGINE_DEFAULT_WEIGHTS }));

  // merge saved in/out adjustments so the cards, footage total, and the render all use them
  const merged = candidates.map((c) => { const r = ranges[c.id]; return r ? { ...c, start: r.start, end: r.end } : c; });
  const isSel = (c: Candidate) => overrides[c.id] ?? c.sel;
  const sel = merged.filter(isSel);
  const toggle = (id: string) => setOverrides((o) => { const c = candidates.find((x) => x.id === id); return { ...o, [id]: !(o[id] ?? c?.sel ?? false) }; });
  const view = mode === "All" ? merged : merged.filter((c) => c.mode.toLowerCase() === mode.toLowerCase());
  const countFor = (m: string) => (m === "All" ? merged.length : merged.filter((c) => c.mode.toLowerCase() === m.toLowerCase()).length);

  // Live reweighted score (client mirror of the engine) + score-sorted display. `view` stays in
  // time order so "Merge next" always extends to the next-in-time moment, regardless of sort.
  const scoreOf = (c: Candidate) => scoreFromFactors(c.factors, weights);
  const display = showRank ? [...view].sort((a, b) => scoreOf(b) - scoreOf(a)) : view;
  const timeNext = (c: Candidate) => { const i = view.findIndex((x) => x.id === c.id); return i >= 0 ? view[i + 1] : undefined; };

  // The mode tabs FILTER the already-found candidates — instant, no re-scan, nothing lost.
  // Finding more is the explicit action below; candidates accumulate (mapCandidates), so a new
  // scan never discards what's already on screen.
  const findMore = async () => {
    if (scanInFlight.current || finding) return;
    scanInFlight.current = true;
    setScanning(true);
    const modes = mode === "All" ? ENGINE_MODES : [mode.toLowerCase()];
    try {
      const results = await Promise.allSettled(modes.map((m) => ctx.client.findMoments(sourceId, { mode: m })));
      const succeeded = results.filter((result) => result.status === "fulfilled").length;
      const failures = results.flatMap((result) => result.status === "rejected"
        ? [formatActionError(result.reason, "Could not start moment scan.")]
        : []);
      const failed = failures.length;
      const counts = `${succeeded} succeeded · ${failed} failed`;
      if (failed > 0) {
        ctx.pushToast({
          icon: "alert",
          tone: succeeded > 0 ? "warn" : "err",
          title: succeeded > 0 ? "Some scans could not start" : "Moment scan failed",
          body: `${counts}. ${failures.join(" ")}`,
        });
        return;
      }
      ctx.pushToast({
        icon: "sparkles",
        tone: "info",
        title: mode === "All" ? "Moment scans started" : `Finding more ${mode.toLowerCase()} moments`,
        body: `${counts}. New moments appear as each scan finishes; your current picks stay put.`,
      });
    } finally {
      scanInFlight.current = false;
      setScanning(false);
    }
  };
  const makeSelected = async () => {
    if (makeInFlight.current || !sel.length) return;
    makeInFlight.current = true;
    setMaking(true);
    try {
      await ctx.makeClipsFrom(sel);
    } finally {
      makeInFlight.current = false;
      setMaking(false);
    }
  };
  // "Merge next": extend this clip's out-point to the next-in-time candidate's end (one clip spanning both).
  const mergeNext = (c: Candidate) => {
    const next = timeNext(c);
    if (!next) return;
    setRanges((r) => ({ ...r, [c.id]: { start: c.start, end: next.end } }));
    setOverrides((o) => ({ ...o, [c.id]: true, [next.id]: false }));
    ctx.pushToast({ icon: "layers", tone: "info", title: "Merged with the next moment", body: `Clip now spans ${fmtTC(c.start)} → ${fmtTC(next.end)}` });
  };

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <div className="seg neutral" style={{ flexWrap: "wrap" }}>
          {MODES.map((m) => {
            const n = countFor(m);
            return <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>{m}{n > 0 && <span className="mono" style={{ marginLeft: 6, opacity: 0.55, fontSize: 11 }}>{n}</span>}</button>;
          })}
        </div>
        <div className="spacer" />
        <Btn variant={showRank ? "primary" : "ghost"} icon="chart" onClick={() => setShowRank((s) => !s)} disabled={!view.some((c) => c.factors)} title={view.some((c) => c.factors) ? "Rank by the glass-box opportunity score" : "Scores appear once a scan finishes"}>Rank by score</Btn>
        <Btn variant="ghost" icon="refresh" onClick={findMore} disabled={scanning || finding}>{scanning ? "Starting scans…" : mode === "All" ? "Scan all modes" : `Find more ${mode.toLowerCase()}`}</Btn>
      </div>

      {/* Glass-box reweight: drag a factor's importance → the scores + order update instantly (the
          same transparent Σ(factor·weight) the engine's moments.rank / POST …/rank computes). */}
      {showRank && !finding && view.length > 0 && (
        <div className="card" style={{ padding: 16, marginBottom: 16 }}>
          <div className="row" style={{ marginBottom: 14 }}>
            <div className="eyebrow">Reweight ranking factors</div>
            <span className="spacer" />
            <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>glass-box — sorted high → low</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 18 }}>
            {FACTOR_META.map((m) => (
              <div key={m.key}>
                <div className="row" style={{ marginBottom: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{m.label}</span>
                  <span className="spacer" />
                  <span className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>{weights[m.key]}×</span>
                </div>
                <input type="range" min={0} max={6} value={weights[m.key]} onChange={(e) => setWeights((w) => ({ ...w, [m.key]: +e.target.value }))} style={{ width: "100%", accentColor: "var(--accent)" }} aria-label={m.label + " weight"} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Existing candidates always stay on screen — a scan-in-progress shows a banner above them,
          never a wipe-to-skeletons (that was the bug). Skeletons only when there's nothing yet. */}
      {view.length > 0 && (
        <>
          {finding && (
            <div className="row" style={{ gap: 10, marginBottom: 14, color: "var(--accent)", fontSize: 13 }}>
              <Icon name="sparkles" size={15} style={{ animation: "pulse 1.5s infinite" }} /> Scanning for more — your current moments stay below…
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingBottom: sel.length > 0 ? 80 : 0 }}>
            {display.map((c) => <CandidateCard key={c.id} c={c} selected={isSel(c)} onToggle={toggle} onAdjust={setAdjust} onMerge={timeNext(c) ? mergeNext : undefined} dynScore={showRank ? scoreOf(c) : undefined} />)}
          </div>
        </>
      )}

      {view.length === 0 && finding && (
        <div>
          <div className="row" style={{ gap: 10, marginBottom: 16, color: "var(--accent)", fontSize: 13 }}>
            <Icon name="sparkles" size={16} style={{ animation: "pulse 1.5s infinite" }} /> Scanning transcript{mode === "All" ? "" : ` for ${mode.toLowerCase()} moments`}…
          </div>
          {[0, 1, 2].map((i) => (
            <div key={i} className="card" style={{ display: "flex", gap: 14, padding: 13, marginBottom: 12 }}>
              <div className="skel" style={{ width: 130, height: 84, borderRadius: 8, flex: "none" }} />
              <div className="grow"><div className="skel" style={{ height: 16, width: "45%", marginBottom: 10 }} /><div className="skel" style={{ height: 12, width: "90%", marginBottom: 7 }} /><div className="skel" style={{ height: 12, width: "70%" }} /></div>
            </div>
          ))}
        </div>
      )}

      {view.length === 0 && !finding && (
        <Empty icon="scan" title={mode === "All" ? "No moments yet" : `No ${mode.toLowerCase()} moments yet`}
          action={<Btn variant="primary" icon="sparkles" onClick={findMore} disabled={scanning}>{scanning ? "Starting scans…" : mode === "All" ? "Scan all modes" : `Find ${mode.toLowerCase()} moments`}</Btn>}>
          {mode === "All"
            ? "Scan the transcript for clip-worthy moments — punchlines, hot-takes, stories and more. Each mode you scan adds to its tab."
            : `No ${mode.toLowerCase()} moments found yet — scan for them. Moments you've already found stay under their own tabs.`}
        </Empty>
      )}

      {sel.length > 0 && (
        <div style={{ position: "sticky", bottom: 0, marginTop: 14, padding: "12px 16px", background: "var(--bg-1)", border: "1px solid var(--line-str)", borderRadius: "var(--radius)", boxShadow: "var(--shadow-pop)", display: "flex", alignItems: "center", gap: 14 }}>
          <span className="chip solid">{sel.length}</span>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>candidates selected</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>≈ {Math.round(sel.reduce((a, c) => a + (c.end - c.start), 0))}s of footage</span>
          <span className="spacer" />
          <Btn variant="ghost" onClick={() => setOverrides((o) => ({ ...o, ...Object.fromEntries(view.map((c) => [c.id, true])) }))}>Select all</Btn>
          <Btn variant="primary" icon="scissors" onClick={makeSelected} disabled={making}>{making ? "Starting clips…" : `Make ${sel.length} clips →`}</Btn>
        </div>
      )}
      {adjust && <AdjustModal c={adjust} onClose={() => setAdjust(null)} onSave={(start, end) => setRanges((r) => ({ ...r, [adjust.id]: { start, end } }))} />}
    </div>
  );
}

/* S4 transcript — editable (Phase 2). Click words to select a range → "Cut clip from
 * selection" (the engine ripple-cuts any deleted words out); double-click a word to fix its
 * text; ✕ to delete it. Edits persist to words.json so caption re-burns use the fixes. */
export function TranscriptView({ lines, speakers, tid, sourceId, onEdited }: { lines: TranscriptLine[]; speakers: Record<string, SpeakerInfo>; tid?: string; sourceId?: string; onEdited?: () => void }) {
  const ctx = useSpool();
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<{ idx: number; text: string } | null>(null);
  const [editPending, setEditPending] = useState(false);
  const [cutPending, setCutPending] = useState(false);
  const [sel, setSel] = useState<{ a: number; b: number } | null>(null);
  const editInFlight = useRef(false);
  const editOp = useRef(0);
  const cutInFlight = useRef(false);
  const cutOp = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      editOp.current += 1;
      cutOp.current += 1;
    };
  }, []);
  if (!lines.length) return <Empty icon="type" title="No transcript yet">Once this source finishes transcribing, the word-level transcript shows here.</Empty>;

  const editable = !!tid;
  const lo = sel ? Math.min(sel.a, sel.b) : 0, hi = sel ? Math.max(sel.a, sel.b) : 0;
  const inSel = (ti: number) => sel != null && ti >= lo && ti <= hi;
  const selWords = sel ? lines.flatMap((l) => l.tokens).filter((t) => inSel(t.ti)) : [];

  const doOp = async (idx: number, op: string, w?: string) => {
    if (!tid || editInFlight.current || cutInFlight.current) return;
    const startedAtLocation = window.location.href;
    const operation = ++editOp.current;
    const isCurrent = () => mounted.current && editOp.current === operation && window.location.href === startedAtLocation;
    editInFlight.current = true;
    setEditPending(true);
    try {
      await ctx.client.editWord(tid, idx, w !== undefined ? { op, w } : { op });
      if (!isCurrent()) return;
      setEditing(null);
      onEdited?.();
    } catch (error) {
      if (isCurrent())
        ctx.pushToast({ icon: "alert", tone: "err", title: "Transcript edit failed", body: formatActionError(error, "Could not update this word.") });
    } finally {
      if (editOp.current === operation) editInFlight.current = false;
      if (mounted.current && editOp.current === operation) setEditPending(false);
    }
  };
  const cutSelection = async () => {
    if (!sourceId || !selWords.length || cutInFlight.current || editInFlight.current) return;
    const end = Math.max(hi, ...selWords.map((t) => t.te));
    const startedAtLocation = window.location.href;
    const op = ++cutOp.current;
    const isCurrent = () => mounted.current && cutOp.current === op && window.location.href === startedAtLocation;
    cutInFlight.current = true;
    setCutPending(true);
    try {
      await ctx.client.cut(sourceId, { start: lo, end });
      if (!isCurrent()) return;
      ctx.pushToast({ icon: "scissors", tone: "info", title: "Cutting clip from transcript", body: `${selWords.length} words · deleted words ripple out · track it in the queue` });
      setSel(null);
      ctx.nav("queue");
    } catch (error) {
      if (isCurrent())
        ctx.pushToast({ icon: "alert", tone: "err", title: "Clip cut failed", body: formatActionError(error, "Could not cut this transcript selection.") });
    } finally {
      if (cutOp.current === op) cutInFlight.current = false;
      if (mounted.current && cutOp.current === op) setCutPending(false);
    }
  };

  // One speaker-line — rendered verbatim whether the list is windowed or not (so the rows look
  // identical; only off-screen lines are absent from the DOM when windowed).
  const renderLine = (line: TranscriptLine) => {
    const sp = speakers[line.sp] || { name: line.sp, color: "var(--accent)" };
    const match = q && line.words.toLowerCase().includes(q.toLowerCase());
    return (
      <div key={line.id} style={{ display: "flex", gap: 14, padding: "10px 16px", background: match ? "var(--accent-soft)" : "transparent", borderRadius: 8 }}>
        <div style={{ flex: "none", width: 96, textAlign: "right" }}>
          <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>{fmtTC(line.t)}</div>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: sp.color }}>{sp.name}</div>
        </div>
        <div style={{ lineHeight: 1.9, fontSize: 14 }}>
          {line.tokens.map((tk, i) => {
            if (editing && editing.idx === tk.idx) {
              return (
                <span key={i} style={{ display: "inline-flex", gap: 4, alignItems: "center", verticalAlign: "middle", margin: "0 3px" }}>
                  <input autoFocus aria-label={`Edit transcript word ${tk.w}`} value={editing.text} disabled={editPending || cutPending} onChange={(e) => setEditing({ idx: tk.idx, text: e.target.value })}
                    onKeyDown={(e) => { if (e.key === "Enter" && editing.text.trim()) doOp(tk.idx, "set_text", editing.text.trim()); if (e.key === "Escape") setEditing(null); }}
                    style={{ font: "inherit", fontSize: 14, padding: "1px 6px", borderRadius: 5, border: "1px solid var(--accent)", background: "var(--bg-1)", color: "var(--text)", width: Math.max(64, editing.text.length * 9) }} />
                  <button className="btn subtle sm" title="save" disabled={editPending || cutPending} style={{ padding: "2px 6px" }} onClick={() => editing.text.trim() && doOp(tk.idx, "set_text", editing.text.trim())}><Icon name="check" size={13} /></button>
                  <button className="btn subtle sm" title="delete word" disabled={editPending || cutPending} style={{ padding: "2px 6px", color: "var(--err, #e5484d)" }} onClick={() => doOp(tk.idx, "delete")}><Icon name="trash" size={13} /></button>
                </span>
              );
            }
            return (
              <button type="button" key={i}
                aria-label={`${tk.w} — select word; press F2 to edit`}
                aria-pressed={inSel(tk.ti)}
                onClick={() => editable && setSel((c) => (!c || c.a !== c.b ? { a: tk.ti, b: tk.ti } : { a: c.a, b: tk.ti }))}
                onDoubleClick={() => editable && setEditing({ idx: tk.idx, text: tk.w })}
                onKeyDown={(e) => {
                  if (editable && e.key === "F2") {
                    e.preventDefault();
                    setEditing({ idx: tk.idx, text: tk.w });
                  }
                }}
                title={editable ? "select word · F2 or double-click: edit" : undefined}
                style={{ cursor: editable ? "pointer" : "default", padding: "1px 2px", margin: 0, border: 0, borderRadius: 3, background: inSel(tk.ti) ? "var(--accent)" : "transparent", color: inSel(tk.ti) ? "var(--accent-ink)" : "inherit", font: "inherit", lineHeight: "inherit", verticalAlign: "baseline" }}>{tk.w} </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 16 }}>
        <div className="cmdk" style={{ maxWidth: 280 }}><Icon name="search" size={15} /><input style={{ background: "transparent", border: 0, outline: "none", color: "var(--text)", flex: 1, fontFamily: "inherit" }} placeholder="Search transcript…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <div className="row" style={{ gap: 14, marginLeft: 6 }}>
          {Object.entries(speakers).map(([k, v]) => <span key={k} className="row" style={{ gap: 7, fontSize: 12.5 }}><span style={{ width: 9, height: 9, borderRadius: 3, background: v.color }} />{v.name}</span>)}
        </div>
        <span className="spacer" />
        <span style={{ fontSize: 12, color: "var(--text-faint)" }}>{editable ? "Select words with keyboard or pointer · F2/double-click to fix · ✕ to delete" : "Transcribe to edit"}</span>
      </div>
      <div className="panel" style={{ padding: "8px 4px", maxWidth: 760 }}>
        {lines.length > LINE_VIRT_THRESHOLD
          ? <WindowList items={lines} getKey={(l) => l.id} estimateSize={56}>{(line) => renderLine(line)}</WindowList>
          : lines.map(renderLine)}
      </div>
      {editable && sel && selWords.length > 0 && (
        <div className="row" style={{ position: "sticky", bottom: 16, marginTop: 16, gap: 12, padding: "10px 14px", borderRadius: 12, background: "var(--bg-1)", border: "1px solid var(--line-str)", boxShadow: "0 8px 30px rgba(0,0,0,0.18)", width: "fit-content" }}>
          <Icon name="scissors" size={15} style={{ color: "var(--accent)" }} />
          <span style={{ fontSize: 13 }}>{selWords.length} word{selWords.length === 1 ? "" : "s"} · {fmtTC(lo)}–{fmtTC(hi)}</span>
          <Btn variant="primary" size="sm" icon="scissors" onClick={cutSelection} disabled={editPending || cutPending}>{cutPending ? "Cutting…" : "Cut clip from selection"}</Btn>
          <button className="btn subtle sm" onClick={() => setSel(null)}>Clear</button>
        </div>
      )}
    </div>
  );
}
