"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool, type SpoolClip } from "@/components/spool/context";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { useClipSeededState } from "@/lib/use-clip-seeded-state";
import { captionPage, STYLE_CHUNK } from "@/lib/caption-page";
import { describeActionError } from "@/lib/action-error";
import { Timeline } from "@/components/spool/timeline";
import { Btn, Chip, Empty, Icon, Seg, Switch } from "@spool/ui";

const PREVIEW_SSE_GRACE_MS = 5_000;

/* S6 Editor — connective hub, 1:1 port of the demo (06). Preview · transport · timeline ·
 * inspector (Format / Captions / Brand / Export). Render runs the real engine with the
 * inspector's chosen aspect / reframe-mode / export-preset; the inspector links to the live
 * Reframe + Caption screens; Version history lists the clip's real renders.
 * (Deeper timeline editing — trim-render, A/B, word ripple-cut — is the Phase-2 surface.) */

export default function EditorScreen() {
  const ctx = useSpool();
  const { snapshot } = useLive();
  const id = String(useParams().id);
  const clip = ctx.clips.find((c) => c.id === id);

  // Distinguish "snapshot still loading" from "clip genuinely absent" so a deep link doesn't
  // flash "not found", and so EditorBody only mounts once the real clip exists (state seeds right).
  if (!clip) {
    if (!snapshot) return <div className="mainpad fadein" style={{ color: "var(--text-faint)" }}>Loading clip…</div>;
    return (
      <div className="mainpad fadein">
        <button className="btn subtle sm" style={{ marginBottom: 14, paddingLeft: 0 }} onClick={() => ctx.nav("clips")}><Icon name="chevL" size={15} /> Clips</button>
        <Empty icon="scissors" title="Clip not found" action={<Btn variant="primary" onClick={() => ctx.nav("clips")}>Back to clips</Btn>}>This clip ID is not available in the current engine snapshot. Its import or render may still be incomplete.</Empty>
      </div>
    );
  }
  return <EditorBody key={clip.id} clip={clip} />;
}

/* Editor → Brand inspector: pick a persisted kit and apply it to this clip (caption with the
 * kit's preset/overrides/watermark/lower-third, then render) — reuses the S9 brand-kit store. */
function BrandInspector({ clipId, preset }: { clipId: string; preset: string }) {
  const ctx = useSpool();
  const kitsQ = useEngineQuery((c) => c.listBrandKits(), []);
  const kits = kitsQ.data?.brand_kits ?? [];
  const [sel, setSel] = useState("");
  const [applying, setApplying] = useState(false);
  const applyInFlight = useRef(false);
  const applyOp = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      applyOp.current += 1;
    };
  }, []);
  const apply = async () => {
    const k = kits.find((x) => x.id === sel);
    if (!k || applyInFlight.current) return;
    const startedAtLocation = window.location.href;
    const op = ++applyOp.current;
    const isCurrent = () => mounted.current && applyOp.current === op && window.location.href === startedAtLocation;
    applyInFlight.current = true;
    setApplying(true);
    try {
      const cap = await ctx.client.caption(clipId, {
        style: k.caption_preset || "opus", overrides: k.caption_overrides,
        watermark: k.watermark || undefined, lower_third: k.lower_third || undefined,
      });
      if (!isCurrent()) return;
      await ctx.awaitClipJob(cap.id);
      if (!isCurrent()) return;
      await ctx.client.render(clipId, { preset });
      if (!isCurrent()) return;
      ctx.pushToast({ icon: "palette", tone: "info", title: `Applied “${k.name}”`, body: "Caption finished and render was submitted." });
      ctx.nav("queue");
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Brand apply failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (applyOp.current === op) applyInFlight.current = false;
      if (isCurrent()) setApplying(false);
    }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <span className="field-label" style={{ margin: 0 }}>Brand kit</span>
      {kits.length === 0 ? (
        <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No kits yet — design one in the Brand screen, then apply it here.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {kits.map((k) => (
            <button type="button" key={k.id} aria-pressed={sel === k.id} disabled={applying} onClick={() => { if (!applyInFlight.current) setSel(k.id); }} className="card" style={{ padding: 10, display: "flex", alignItems: "center", gap: 8, cursor: applying ? "not-allowed" : "pointer", borderColor: sel === k.id ? "var(--accent)" : "var(--line)", background: sel === k.id ? "var(--accent-soft)" : "var(--bg-2)" }}>
              <div className="row" style={{ gap: 4 }}>{(k.palette ?? []).slice(0, 4).map((c, j) => <span key={j} style={{ width: 14, height: 14, borderRadius: 4, background: c, border: "1px solid var(--line)" }} />)}</div>
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>{k.name}</span>
              <span className="spacer" />
              {k.watermark && <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>{k.watermark}</span>}
            </button>
          ))}
        </div>
      )}
      <Btn variant="primary" icon="palette" onClick={apply} disabled={!sel || applying}>{applying ? "Applying…" : "Apply kit + render"}</Btn>
      <Btn variant="ghost" icon="plus" onClick={() => ctx.nav("brand")}>Design kits →</Btn>
    </div>
  );
}

function EditorBody({ clip }: { clip: SpoolClip }) {
  const ctx = useSpool();
  const { snapshot } = useLive();
  const id = clip.id;
  const [insp, setInsp] = useState("Format");
  const [playing, setPlaying] = useState(false);
  const [aspect, setAspect] = useClipSeededState(clip.aspect, "9:16");
  const [reframe, setReframe] = useState("pan");
  const [preset, setPreset] = useClipSeededState(clip.platform, "tiktok");
  const [safe, setSafe] = useState(true);
  const [cur, setCur] = useState(0);               // playhead (clip-relative seconds) for the live caption overlay
  const [style, setStyle] = useClipSeededState(clip.style, "opus");
  const [artifact, setArtifact] = useState<"reframed" | "clip">("reframed"); // which intermediate to preview
  const videoRef = useRef<HTMLVideoElement>(null);

  // F.3 — real-render preview: render a fast low-res REAL reframe for the chosen aspect/mode so the
  // editor shows what-you-get (not a CSS crop). Per-combo: cleared whenever aspect/mode changes.
  const [pvJob, setPvJob] = useState<{ id: string; aspect: string; mode: string; localPending: boolean } | null>(null);
  const previewInFlight = useRef(false);
  const previewGraceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const renderInFlight = useRef(false);
  const cutInFlight = useRef(false);
  const wordEditInFlight = useRef(false);
  const previewOp = useRef(0);
  const renderOp = useRef(0);
  const cutOp = useRef(0);
  const wordEditOp = useRef(0);
  const mounted = useRef(false);
  const [previewSubmitting, setPreviewSubmitting] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [cutting, setCutting] = useState(false);
  const [wordEditPending, setWordEditPending] = useState(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (previewGraceTimer.current) clearTimeout(previewGraceTimer.current);
      previewOp.current += 1;
      renderOp.current += 1;
      cutOp.current += 1;
      wordEditOp.current += 1;
    };
  }, []);
  // Relevance is DERIVED (no effect): a stored preview only applies while its combo matches the
  // current aspect/mode — change either and it's ignored (falls back to the live crop) until re-run.
  const pvMatch = !!pvJob && pvJob.aspect === aspect && pvJob.mode === reframe;
  const pvLive = pvMatch ? (snapshot?.clips ?? []).find((c) => c.id === pvJob!.id) : undefined;
  const pvReady = pvMatch && pvLive?.status === "done";
  const pvRendering = pvMatch && (
    pvLive?.status === "queued" || pvLive?.status === "running" || (!pvLive && !!pvJob?.localPending)
  );
  const requestPreview = async () => {
    if (previewInFlight.current || pvRendering) return;
    const startedAtLocation = window.location.href;
    const op = ++previewOp.current;
    const isCurrent = () => mounted.current && previewOp.current === op && window.location.href === startedAtLocation;
    if (previewGraceTimer.current) {
      clearTimeout(previewGraceTimer.current);
      previewGraceTimer.current = null;
    }
    previewInFlight.current = true;
    setPreviewSubmitting(true);
    try {
      const job = await ctx.client.reframe(id, { aspect, mode: reframe, preview: true });
      if (isCurrent()) {
        setPvJob({ id: job.id, aspect, mode: reframe, localPending: true });
        previewGraceTimer.current = setTimeout(() => {
          previewGraceTimer.current = null;
          if (isCurrent())
            setPvJob((current) => current?.id === job.id ? { ...current, localPending: false } : current);
        }, PREVIEW_SSE_GRACE_MS);
      }
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Preview failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (previewOp.current === op) previewInFlight.current = false;
      if (isCurrent()) setPreviewSubmitting(false);
    }
  };

  // Render = burn the chosen caption style + export (reframe first if the format changed here).
  const render = async () => {
    if (renderInFlight.current) return;
    const startedAtLocation = window.location.href;
    const op = ++renderOp.current;
    const isCurrent = () => mounted.current && renderOp.current === op && window.location.href === startedAtLocation;
    renderInFlight.current = true;
    setRendering(true);
    try {
      await ctx.makeClipsFrom([{ id }], { aspect, mode: reframe, preset, style });
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Render failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (renderOp.current === op) renderInFlight.current = false;
      if (isCurrent()) setRendering(false);
    }
  };
  const renders = (snapshot?.clips ?? []).filter((c) => c.clip_id === id && (c.kind === "export" || c.kind === "pipeline") && c.status === "done" && c.result.render_id);
  const [ver, setVer] = useState<number | null>(null);   // A/B: which render version is in the preview (null = latest)
  const verIdx = ver == null ? renders.length - 1 : Math.min(ver, renders.length - 1);
  const selRender = renders[verIdx];
  const renderSrc = selRender ? ctx.client.renderFileUrl(id, selRender.result.render_id!) : null;

  // The clip's transcript words (sliced to its window) drive the word-level timeline:
  // click to scrub the rendered video, ✕ to delete → ripple-cut on re-cut (reuses slice 3).
  const src = ctx.sources.find((s) => s.id === clip.src);
  const doc = useEngineQuery((c) => (src?.transcriptId ? c.getTranscriptDoc(src.transcriptId) : Promise.resolve(undefined)), [src?.transcriptId]);
  const lo = clip.start ?? 0, hi = clip.end ?? Infinity;
  const allWords = doc.data?.words ?? [];
  const inWin = (w: { start: number | null }) => w.start != null && w.start >= lo && w.start <= hi;
  const tlWords = allWords.filter((w) => !w.deleted && inWin(w));
  const captionSample = tlWords.slice(0, 2).map((w) => w.w);
  const deletedInWin = allWords.filter((w) => w.deleted && inWin(w)).length;
  // Real timeline lanes for the clip window: Energy (loudness envelope) + Scenes (cut markers).
  // hiSafe falls back to the last word's end when the clip has no explicit out point.
  const hiSafe = isFinite(hi) ? hi : (tlWords.length ? (tlWords[tlWords.length - 1]!.end ?? lo + 60) : lo + 60);
  const energyQ = useEngineQuery((c) => (src ? c.sourceEnergy(clip.src, 80, { start: lo, end: hiSafe }) : Promise.resolve({ bars: [], buckets: 0 })), [clip.src, lo, hiSafe]);
  const scenesQ = useEngineQuery((c) => (src ? c.sourceScenes(clip.src, { start: lo, end: hiSafe }) : Promise.resolve({ cuts: [] })), [clip.src, lo, hiSafe]);
  const filmstripQ = useEngineQuery((c) => (src ? c.sourceFilmstrip(clip.src, { start: lo, end: hiSafe }, 14) : Promise.resolve({ strip: null, frames: 0 })), [clip.src, lo, hiSafe]);
  const delWord = async (idx: number) => {
    if (!src?.transcriptId || wordEditInFlight.current || cutInFlight.current) return;
    const startedAtLocation = window.location.href;
    const op = ++wordEditOp.current;
    const isCurrent = () => mounted.current && wordEditOp.current === op && window.location.href === startedAtLocation;
    wordEditInFlight.current = true;
    setWordEditPending(true);
    try {
      await ctx.client.editWord(src.transcriptId, idx, { op: "delete" });
      if (!isCurrent()) return;
      doc.reload();
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Word edit failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (wordEditOp.current === op) wordEditInFlight.current = false;
      if (mounted.current && wordEditOp.current === op) setWordEditPending(false);
    }
  };
  const recut = async () => {
    if (!src || cutInFlight.current || wordEditInFlight.current) return;
    const startedAtLocation = window.location.href;
    const op = ++cutOp.current;
    const isCurrent = () => mounted.current && cutOp.current === op && window.location.href === startedAtLocation;
    cutInFlight.current = true;
    setCutting(true);
    try {
      await ctx.client.cut(src.id, { start: lo, end: hi });
      if (!isCurrent()) return;
      ctx.pushToast({ icon: "scissors", tone: "info", title: "Re-cutting clip", body: `${deletedInWin} deleted word${deletedInWin === 1 ? "" : "s"} rippled out — a new version is in the queue` });
      ctx.nav("queue");
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Re-cut failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (cutOp.current === op) cutInFlight.current = false;
      if (isCurrent()) setCutting(false);
    }
  };
  // Trim → re-cut to a new [start, end] window from the timeline's trim handles.
  const trimRecut = async (s: number, e: number) => {
    if (!src || cutInFlight.current || wordEditInFlight.current) return;
    const startedAtLocation = window.location.href;
    const op = ++cutOp.current;
    const isCurrent = () => mounted.current && cutOp.current === op && window.location.href === startedAtLocation;
    cutInFlight.current = true;
    setCutting(true);
    try {
      await ctx.client.cut(src.id, { start: s, end: e });
      if (!isCurrent()) return;
      ctx.pushToast({ icon: "scissors", tone: "info", title: "Re-cutting clip", body: `Trimmed to ${Math.round(e - s)}s — a new version is in the queue` });
      ctx.nav("queue");
    } catch (error) {
      if (isCurrent()) {
        const failure = describeActionError(error);
        ctx.pushToast({ icon: "alert", tone: "warn", title: "Trim failed", body: `${failure.code}: ${failure.message}` });
      }
    } finally {
      if (cutOp.current === op) cutInFlight.current = false;
      if (isCurrent()) setCutting(false);
    }
  };
  // play() rejects (NotSupportedError) when the source can't load yet — e.g. a cut clip's
  // preview momentarily points at the not-yet-existent "reframed" artifact before onError
  // falls back to the raw cut. That's expected and recoverable, so swallow it instead of
  // leaving an unhandled rejection (which surfaces as a Next.js runtime error overlay).
  const togglePlay = () => { const v = videoRef.current; if (v) { if (v.paused) v.play().catch(() => {}); else v.pause(); setPlaying(!v.paused); } else setPlaying((p) => !p); };
  const others = ctx.clips.filter((c) => c.src === clip.src && c.id !== clip.id).slice(0, 4);

  // Live preview source + framing:
  //  - a burned render → play it as-is (contain).
  //  - the clip's baked aspect (9:16) → play the real reframed cut (the diar⊕ROI speaker-pan), contain.
  //  - any other aspect the user picks here → re-frame LIVE by playing the original cut center-cropped
  //    (object-fit: cover) into the chosen frame, so 16:9 / 1:1 / 4:5 actually change the picture.
  //    The exact speaker-pan at that aspect bakes in on Render.
  // The baked reframed cut is the real diar⊕ROI *pan* at 9:16 — show it only when the picks match
  // it. For any other combo, "Preview real reframe" (F.3) renders the actual low-res reframe to
  // preview.mp4 → played here (contain, what-you-get); until then the original cut is shown
  // center-cropped (object-fit: cover) as an instant approximation, with a hint for Split.
  const reframedAspect = clip.aspect || "9:16";
  const showReframed = !renderSrc && reframe === "pan" && aspect === reframedAspect && artifact === "reframed";
  const usePreview = !renderSrc && !showReframed && pvReady;   // the real low-res reframe for this combo
  const previewKind: "reframed" | "clip" = showReframed ? "reframed" : "clip";
  const previewSrc = renderSrc
    ? renderSrc
    : usePreview ? `${ctx.client.clipArtifactUrl(id, "preview")}?v=${pvJob!.id}`
    : ctx.client.clipArtifactUrl(id, previewKind);
  const previewFit: "contain" | "cover" = renderSrc || showReframed || usePreview ? "contain" : "cover";
  const hl = ({ opus: "var(--caption-hl)", karaoke: "#37E2A0", minimal: "#ffffff" } as Record<string, string>)[style] || "var(--caption-hl)";
  // Paged karaoke, mirroring the burn: the page stays fixed while the highlight
  // transfers word-by-word, and swaps only after its last word ends (words are in
  // source time; `cur` is clip-relative, hence lo + cur).
  const capPage = captionPage(tlWords, lo + cur, STYLE_CHUNK[style] ?? 3);
  const capLine = capPage?.page ?? [];
  const activeWordIdx = capPage?.page[capPage.activeInPage]?.idx;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }} className="fadein">
      <div className="row" style={{ gap: 10, padding: "12px 20px", borderBottom: "1px solid var(--line)", flex: "none" }}>
        <button className="btn subtle sm" onClick={() => ctx.nav("clips")}><Icon name="chevL" size={15} /> Clips</button>
        <div className="divider" style={{ width: 1, height: 20, background: "var(--line)" }} />
        <span style={{ fontWeight: 600 }}>{clip.title}</span>
        <span className="spacer" />
        <div className="row" style={{ gap: 6 }}>{others.map((o) => <button key={o.id} className="chip" style={{ cursor: "pointer" }} onClick={() => ctx.nav("editor", { id: o.id })}>{o.title.split(" ").slice(0, 3).join(" ")}…</button>)}</div>
        <Btn variant="primary" size="sm" icon="zap" onClick={render} disabled={rendering}>{rendering ? "Rendering…" : "Render"}</Btn>
      </div>

      <div style={{ flex: 1, display: "grid", gridTemplateColumns: "1fr 320px", minHeight: 0 }}>
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0, borderRight: "1px solid var(--line)" }}>
          <div style={{ flex: 1, display: "grid", placeItems: "center", padding: 24, background: "#070809", minHeight: 0 }}>
            <div style={{ height: "100%", aspectRatio: aspect === "9:16" ? "9/16" : aspect === "1:1" ? "1/1" : aspect === "4:5" ? "4/5" : "16/9", maxHeight: "52vh", borderRadius: 10, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)", background: "#000" }}>
              {/* Play the real clip: the burned render if there is one, otherwise the reframed cut
                  (→ raw cut on 404). The caption overlay is live only for the un-burned preview. */}
              <video key={renderSrc ? selRender?.result.render_id : usePreview ? `pv-${pvJob!.id}` : previewKind} ref={videoRef} src={previewSrc} controls playsInline
                onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                onTimeUpdate={(e) => setCur(e.currentTarget.currentTime)}
                onError={() => { if (!renderSrc && artifact === "reframed") setArtifact("clip"); }}
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: previewFit, background: "#000" }} />
              {!renderSrc && (
                <>
                  {safe && <div style={{ position: "absolute", inset: "8% 6%", border: "1px dashed rgba(255,255,255,0.3)", borderRadius: 6, pointerEvents: "none" }} />}
                  {reframe === "split" && !usePreview && (
                    <div style={{ position: "absolute", top: "9%", left: "50%", transform: "translateX(-50%)", padding: "4px 9px", borderRadius: 6, background: "rgba(0,0,0,0.66)", color: "#fff", fontSize: 11, fontFamily: "var(--font-mono)", pointerEvents: "none", whiteSpace: "nowrap" }}>split · both speakers stack on Render</div>
                  )}
                  {capLine.length > 0 && (
                    <div style={{ position: "absolute", left: 0, right: 0, bottom: "15%", textAlign: "center", padding: "0 8%", pointerEvents: "none", fontFamily: "var(--font-caption)", fontSize: 19, lineHeight: 1.2, textShadow: "0 2px 7px #000", WebkitTextStroke: "0.5px rgba(0,0,0,.6)", textTransform: style === "opus" ? "uppercase" : "none" }}>
                      {capLine.map((w) => <span key={w.idx} style={{ color: w.idx === activeWordIdx ? hl : "#fff", fontWeight: w.idx === activeWordIdx ? 800 : 600 }}>{w.w} </span>)}
                    </div>
                  )}
                </>
              )}
              <div className="badge" style={{ position: "absolute", top: 8, left: 8 }}>{renderSrc ? "rendered" : usePreview ? `preview · ${reframe}` : `live · ${reframe}`}</div>
            </div>
          </div>
          <div className="row" style={{ gap: 14, padding: "10px 18px", borderTop: "1px solid var(--line)", flex: "none" }}>
            <button className="iconbtn" aria-label={playing ? "Pause preview" : "Play preview"} onClick={togglePlay} style={{ background: "var(--accent)", color: "var(--accent-ink)" }}><Icon name={playing ? "pause" : "play"} size={16} /></button>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{renderSrc ? "playing the rendered clip" : "live preview · captions overlaid · burned in on Render"}</span>
            <span className="spacer" />
            <label className="row" style={{ gap: 7, fontSize: 12.5, cursor: "pointer" }}><Switch label="Safe zones" on={safe} onClick={() => setSafe(!safe)} /> Safe zones</label>
          </div>
          <div style={{ flex: "none", borderTop: "1px solid var(--line)", background: "var(--bg-1)", padding: "12px 18px" }}>
            {(renders.length > 1 || deletedInWin > 0) && (
              <div className="row" style={{ marginBottom: 10 }}>
                <span className="spacer" />
                {renders.length > 1 && (
                  <div className="row" style={{ gap: 5, marginRight: 8 }}>
                    <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>A/B</span>
                    {renders.map((r, i) => <button key={r.id} className={"chip" + (verIdx === i ? " solid" : "")} style={{ cursor: "pointer", height: 22, padding: "0 8px" }} onClick={() => setVer(i)}>v{i + 1}</button>)}
                  </div>
                )}
                {deletedInWin > 0 && <Btn variant="primary" size="sm" icon="scissors" onClick={recut} disabled={cutting || wordEditPending}>{cutting ? "Re-cutting…" : `Re-cut (drop ${deletedInWin})`}</Btn>}
              </div>
            )}
            {tlWords.length > 0 ? (
              <Timeline
                words={tlWords}
                segments={doc.data?.segments ?? []}
                lo={lo}
                hi={hiSafe}
                cur={cur}
                onSeek={(rel) => { const v = videoRef.current; if (v && isFinite(rel)) v.currentTime = Math.max(0, rel); setCur(rel); }}
                onDeleteWord={delWord}
                mutationPending={wordEditPending || cutting}
                energyBars={energyQ.data?.bars ?? []}
                sceneCuts={scenesQ.data?.cuts ?? []}
                filmstrip={filmstripQ.data?.strip ?? null}
                onTrim={trimRecut}
              />
            ) : (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>{doc.loading ? "loading the clip's transcript…" : "This clip's source isn't transcribed — transcribe it for a word-level timeline. Set the format & preset on the right, then Render."}</div>
            )}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div className="tabs" role="tablist" aria-label="Clip inspector" style={{ padding: "0 8px", flex: "none" }}>
            {["Format", "Captions", "Brand", "Export"].map((t) => <button type="button" role="tab" aria-selected={insp === t} key={t} className={"tab" + (insp === t ? " on" : "")} style={{ padding: "11px 11px", fontSize: 12.5, fontFamily: "inherit", background: "transparent", borderTop: 0, borderLeft: 0, borderRight: 0 }} onClick={() => setInsp(t)}>{t}</button>)}
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
            {insp === "Format" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
                <div><span className="field-label">Aspect ratio</span><Seg value={aspect} onChange={setAspect} options={["9:16", "16:9", "1:1", "4:5"]} /></div>
                <div><span className="field-label">Reframe mode</span>
                  <div className="row" style={{ gap: 8 }}>
                    {([["pan", "flip", "Pan"], ["split", "layout", "Split"], ["center", "crop", "Center"]] as const).map(([v, ic, l]) => (
                      <button type="button" key={v} aria-pressed={reframe === v} onClick={() => setReframe(v)} className="card" style={{ flex: 1, padding: "12px 0", display: "flex", flexDirection: "column", alignItems: "center", gap: 7, cursor: "pointer", borderColor: reframe === v ? "var(--accent)" : "var(--line)", background: reframe === v ? "var(--accent-soft)" : "var(--bg-2)" }}>
                        <Icon name={ic} size={20} /><span style={{ fontSize: 12, fontWeight: 600 }}>{l}</span>
                      </button>
                    ))}
                  </div>
                </div>
                {!showReframed && (
                  <div>
                    <Btn variant="ghost" icon="scan" onClick={requestPreview} disabled={previewSubmitting || pvRendering} style={{ width: "100%" }}>
                      {previewSubmitting || pvRendering ? "Rendering real preview…" : pvReady ? "Re-render preview" : "Preview real reframe"}
                    </Btn>
                    <div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", marginTop: 6, lineHeight: 1.5 }}>
                      {pvReady ? "showing the real low-res reframe (what Render bakes)" : "renders the actual reframe for this aspect/mode (vs. the live crop)"}
                    </div>
                  </div>
                )}
                <Btn variant="ghost" icon="scan" onClick={() => ctx.nav("reframe", { id })}>Open ROI editor →</Btn>
              </div>
            )}
            {insp === "Captions" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div><span className="field-label">Preset</span><div className="kbar">{["opus", "karaoke", "minimal"].map((p) => <button type="button" aria-pressed={style === p} key={p} className={"chip" + (style === p ? " solid" : "")} style={{ cursor: "pointer", textTransform: "capitalize", fontFamily: "inherit" }} onClick={() => setStyle(p)}>{p}</button>)}</div></div>
                <div className="card" style={{ padding: 12, textAlign: "center", background: "#0a0b0d" }}>
                  {captionSample.length ? <span style={{ fontFamily: "var(--font-caption)", fontSize: 18, color: "#fff", textTransform: style === "opus" ? "uppercase" : "none" }}>{captionSample[0]} <span style={{ color: hl }}>{captionSample[1] || ""}</span></span>
                    : <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>No transcript words available for preview.</span>}
                </div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>Captions play live over the preview on the left. Render burns this style in; fine-tune size/colors/position in the Caption Studio.</div>
                <Btn variant="ghost" icon="type" onClick={() => ctx.nav("caption", { id })}>Open Caption Studio →</Btn>
              </div>
            )}
            {insp === "Brand" && <BrandInspector clipId={id} preset={preset} />}
            {insp === "Export" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div><span className="field-label">Export preset</span><Seg value={preset} onChange={setPreset} neutral options={[{ value: "tiktok", label: "TikTok" }, { value: "reels", label: "Reels" }, { value: "shorts", label: "Shorts" }]} /></div>
                <div className="card" style={{ padding: 13, fontSize: 12.5, color: "var(--text-dim)" }}><div className="row" style={{ marginBottom: 6 }}><span>Preset</span><span className="spacer" /><span className="mono">{preset}</span></div><div className="row"><span>Requested aspect</span><span className="spacer" /><span className="mono">{aspect}</span></div></div>
                <div>
                  <span className="field-label">Renders</span>
                  <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                    {renders.length === 0 ? <div style={{ fontSize: 12.5, color: "var(--text-faint)", padding: "6px 2px" }}>No renders yet — hit Render to make one.</div>
                      : renders.map((r, i) => {
                        const path = (r.result.output_path as string) || "";
                        const renderMeta = [r.result.preset, r.result.aspect].filter((value): value is string => typeof value === "string" && value.length > 0).join(" · ") || "render";
                        return (
                          <div key={r.id} className="card" style={{ padding: "9px 11px", display: "flex", flexDirection: "column", gap: 6, borderColor: i === renders.length - 1 ? "var(--accent)" : "var(--line)" }}>
                            <div className="row" style={{ gap: 10 }}>
                              <span className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>v{i + 1}</span>
                              <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{renderMeta}</span>
                              <span className="spacer" />
                              {i === renders.length - 1 && <Chip tone="acc">latest</Chip>}
                              <a className="btn subtle sm" style={{ height: 24, padding: "0 8px" }} href={ctx.client.renderFileUrl(id, r.result.render_id!)} download>Download</a>
                            </div>
                            {path && (
                              <div className="row" style={{ gap: 6 }}>
                                <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", direction: "rtl", textAlign: "left" }} title={path}>{path}</span>
                                <button className="iconbtn" style={{ width: 22, height: 22, flex: "none" }} title="Copy file path" onClick={async () => {
                                  try {
                                    await navigator.clipboard.writeText(path);
                                    ctx.pushToast({ icon: "copy", tone: "ok", title: "Path copied", body: "Paste in Finder → Go to Folder (⌘⇧G)" });
                                  } catch (error) {
                                    const failure = describeActionError(error);
                                    ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't copy path", body: `${failure.code}: ${failure.message}` });
                                  }
                                }}><Icon name="copy" size={12} /></button>
                              </div>
                            )}
                          </div>
                        );
                      })}
                  </div>
                </div>
                <Btn variant="primary" icon="zap" onClick={render} disabled={rendering}>{rendering ? "Rendering…" : "Render & export"}</Btn>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
