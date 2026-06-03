"use client";

import { useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool, type SpoolClip } from "@/components/spool/context";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { Btn, Chip, Empty, Icon, Seg, Switch, fmtTC } from "@spool/ui";

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
        <Empty icon="scissors" title="Clip not found" action={<Btn variant="primary" onClick={() => ctx.nav("clips")}>Back to clips</Btn>}>It may still be rendering, or was cleared from the working set.</Empty>
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
  const apply = () => {
    const k = kits.find((x) => x.id === sel);
    if (!k) return;
    ctx.client.caption(clipId, { style: k.caption_preset || "opus", overrides: k.caption_overrides, watermark: k.watermark || undefined, lower_third: k.lower_third || undefined })
      .then(() => ctx.client.render(clipId, { preset }).catch(() => {})).catch(() => {});
    ctx.pushToast({ icon: "palette", tone: "info", title: `Applying “${k.name}”`, body: "Caption + render queued — track it in the queue" });
    ctx.nav("queue");
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <span className="field-label" style={{ margin: 0 }}>Brand kit</span>
      {kits.length === 0 ? (
        <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No kits yet — design one in the Brand screen, then apply it here.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {kits.map((k) => (
            <button key={k.id} onClick={() => setSel(k.id)} className="card" style={{ padding: 10, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", borderColor: sel === k.id ? "var(--accent)" : "var(--line)", background: sel === k.id ? "var(--accent-soft)" : "var(--bg-2)" }}>
              <div className="row" style={{ gap: 4 }}>{(k.palette ?? []).slice(0, 4).map((c, j) => <span key={j} style={{ width: 14, height: 14, borderRadius: 4, background: c, border: "1px solid var(--line)" }} />)}</div>
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>{k.name}</span>
              <span className="spacer" />
              {k.watermark && <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>{k.watermark}</span>}
            </button>
          ))}
        </div>
      )}
      <Btn variant="primary" icon="palette" onClick={apply} disabled={!sel}>Apply kit + render</Btn>
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
  const [aspect, setAspect] = useState(clip.aspect || "9:16");
  const [reframe, setReframe] = useState("pan");
  const [preset, setPreset] = useState(clip.platform || "tiktok");
  const [safe, setSafe] = useState(true);
  const [cur, setCur] = useState(0);               // playhead (clip-relative seconds) for the live caption overlay
  const [style, setStyle] = useState(clip.style || "opus");
  const [artifact, setArtifact] = useState<"reframed" | "clip">("reframed"); // which intermediate to preview
  const videoRef = useRef<HTMLVideoElement>(null);

  // Render = burn the chosen caption style + export (reframe first if the format changed here).
  const render = () => ctx.makeClipsFrom([{ id }], { aspect, mode: reframe, preset, style });
  const capWords = (clip.title || "your caption here").split(" ").slice(0, 8);
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
  const deletedInWin = allWords.filter((w) => w.deleted && inWin(w)).length;
  const seekTo = (t: number) => { const v = videoRef.current; if (v && isFinite(t)) v.currentTime = Math.max(0, t - lo); };
  const delWord = (idx: number) => { if (src?.transcriptId) ctx.client.editWord(src.transcriptId, idx, { op: "delete" }).then(() => doc.reload()).catch(() => {}); };
  const recut = () => { if (!src) return; ctx.client.cut(src.id, { start: lo, end: hi }).then(() => { ctx.pushToast({ icon: "scissors", tone: "info", title: "Re-cutting clip", body: `${deletedInWin} deleted word${deletedInWin === 1 ? "" : "s"} rippled out — a new version is in the queue` }); ctx.nav("queue"); }).catch(() => {}); };
  const togglePlay = () => { const v = videoRef.current; if (v) { if (v.paused) void v.play(); else v.pause(); setPlaying(!v.paused); } else setPlaying((p) => !p); };
  const others = ctx.clips.filter((c) => c.src === clip.src && c.id !== clip.id).slice(0, 4);

  // Live preview source + framing:
  //  - a burned render → play it as-is (contain).
  //  - the clip's baked aspect (9:16) → play the real reframed cut (the diar⊕ROI speaker-pan), contain.
  //  - any other aspect the user picks here → re-frame LIVE by playing the original cut center-cropped
  //    (object-fit: cover) into the chosen frame, so 16:9 / 1:1 / 4:5 actually change the picture.
  //    The exact speaker-pan at that aspect bakes in on Render.
  const reframedAspect = clip.aspect || "9:16";
  const showReframed = !renderSrc && aspect === reframedAspect && artifact === "reframed";
  const previewKind: "reframed" | "clip" = showReframed ? "reframed" : "clip";
  const previewSrc = renderSrc ?? ctx.client.clipArtifactUrl(id, previewKind);
  const previewFit: "contain" | "cover" = renderSrc || showReframed ? "contain" : "cover";
  const hl = ({ opus: "var(--caption-hl)", karaoke: "#37E2A0", minimal: "#ffffff" } as Record<string, string>)[style] || "var(--caption-hl)";
  let activeIdx = -1;
  for (let i = 0; i < tlWords.length; i++) { if (((tlWords[i].start ?? lo) - lo) <= cur) activeIdx = i; else break; }
  const lineStart = Math.max(0, activeIdx - 2);
  const capLine = tlWords.slice(lineStart, lineStart + 6);
  const activeWordIdx = tlWords[activeIdx]?.idx;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }} className="fadein">
      <div className="row" style={{ gap: 10, padding: "12px 20px", borderBottom: "1px solid var(--line)", flex: "none" }}>
        <button className="btn subtle sm" onClick={() => ctx.nav("clips")}><Icon name="chevL" size={15} /> Clips</button>
        <div className="divider" style={{ width: 1, height: 20, background: "var(--line)" }} />
        <span style={{ fontWeight: 600 }}>{clip.title}</span>
        <span className="spacer" />
        <div className="row" style={{ gap: 6 }}>{others.map((o) => <button key={o.id} className="chip" style={{ cursor: "pointer" }} onClick={() => ctx.nav("editor", { id: o.id })}>{o.title.split(" ").slice(0, 3).join(" ")}…</button>)}</div>
        <Btn variant="ghost" size="sm" icon="undo">Undo</Btn>
        <Btn variant="primary" size="sm" icon="zap" onClick={render}>Render</Btn>
      </div>

      <div style={{ flex: 1, display: "grid", gridTemplateColumns: "1fr 320px", minHeight: 0 }}>
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0, borderRight: "1px solid var(--line)" }}>
          <div style={{ flex: 1, display: "grid", placeItems: "center", padding: 24, background: "#070809", minHeight: 0 }}>
            <div style={{ height: "100%", aspectRatio: aspect === "9:16" ? "9/16" : aspect === "1:1" ? "1/1" : aspect === "4:5" ? "4/5" : "16/9", maxHeight: "52vh", borderRadius: 10, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)", background: "#000" }}>
              {/* Play the real clip: the burned render if there is one, otherwise the reframed cut
                  (→ raw cut on 404). The caption overlay is live only for the un-burned preview. */}
              <video key={renderSrc ? selRender?.result.render_id : previewKind} ref={videoRef} src={previewSrc} controls playsInline
                onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                onTimeUpdate={(e) => setCur(e.currentTarget.currentTime)}
                onError={() => { if (!renderSrc && artifact === "reframed") setArtifact("clip"); }}
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: previewFit, background: "#000" }} />
              {!renderSrc && (
                <>
                  {safe && <div style={{ position: "absolute", inset: "8% 6%", border: "1px dashed rgba(255,255,255,0.3)", borderRadius: 6, pointerEvents: "none" }} />}
                  {capLine.length > 0 && (
                    <div style={{ position: "absolute", left: 0, right: 0, bottom: "15%", textAlign: "center", padding: "0 8%", pointerEvents: "none", fontFamily: "var(--font-caption)", fontSize: 19, lineHeight: 1.2, textShadow: "0 2px 7px #000", WebkitTextStroke: "0.5px rgba(0,0,0,.6)", textTransform: style === "opus" ? "uppercase" : "none" }}>
                      {capLine.map((w) => <span key={w.idx} style={{ color: w.idx === activeWordIdx ? hl : "#fff", fontWeight: w.idx === activeWordIdx ? 800 : 600 }}>{w.w} </span>)}
                    </div>
                  )}
                </>
              )}
              <div className="badge" style={{ position: "absolute", top: 8, left: 8 }}>{renderSrc ? "rendered" : "live preview"}</div>
            </div>
          </div>
          <div className="row" style={{ gap: 14, padding: "10px 18px", borderTop: "1px solid var(--line)", flex: "none" }}>
            <button className="iconbtn" onClick={togglePlay} style={{ background: "var(--accent)", color: "var(--accent-ink)" }}><Icon name={playing ? "pause" : "play"} size={16} /></button>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{renderSrc ? "playing the rendered clip" : "live preview · captions overlaid · burned in on Render"}</span>
            <span className="spacer" />
            <label className="row" style={{ gap: 7, fontSize: 12.5, cursor: "pointer" }}><Switch on={safe} onClick={() => setSafe(!safe)} /> Safe zones</label>
          </div>
          <div style={{ flex: "none", borderTop: "1px solid var(--line)", background: "var(--bg-1)", padding: "12px 18px" }}>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="eyebrow">Timeline</span>
              {tlWords.length > 0 && <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>· {tlWords.length} words · click to scrub · ✕ to ripple-cut</span>}
              <span className="spacer" />
              {renders.length > 1 && (
                <div className="row" style={{ gap: 5, marginRight: 8 }}>
                  <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>A/B</span>
                  {renders.map((r, i) => <button key={r.id} className={"chip" + (verIdx === i ? " solid" : "")} style={{ cursor: "pointer", height: 22, padding: "0 8px" }} onClick={() => setVer(i)}>v{i + 1}</button>)}
                </div>
              )}
              {deletedInWin > 0 && <Btn variant="primary" size="sm" icon="scissors" onClick={recut}>Re-cut (drop {deletedInWin})</Btn>}
            </div>
            {tlWords.length > 0 ? (
              <div className="row" style={{ gap: 4, flexWrap: "wrap", maxHeight: 66, overflowY: "auto" }}>
                {tlWords.map((w) => (
                  <span key={w.idx} onClick={() => seekTo(w.start as number)} title={`scrub to ${fmtTC((w.start as number) - lo)}`}
                    style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 12, padding: "2px 5px 2px 7px", borderRadius: 6, background: "var(--bg-3)", cursor: "pointer" }}>
                    {w.w}
                    <button onClick={(e) => { e.stopPropagation(); delWord(w.idx); }} title="delete word (rippled out on Re-cut)" style={{ border: 0, background: "transparent", color: "var(--text-faint)", cursor: "pointer", padding: "0 1px", lineHeight: 1, fontSize: 13 }}>×</button>
                  </span>
                ))}
              </div>
            ) : (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>{doc.loading ? "loading the clip's transcript…" : "This clip's source isn't transcribed — transcribe it for a word-level timeline. Set the format & preset on the right, then Render."}</div>
            )}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div className="tabs" style={{ padding: "0 8px", flex: "none" }}>
            {["Format", "Captions", "Brand", "Export"].map((t) => <div key={t} className={"tab" + (insp === t ? " on" : "")} style={{ padding: "11px 11px", fontSize: 12.5 }} onClick={() => setInsp(t)}>{t}</div>)}
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
            {insp === "Format" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
                <div><span className="field-label">Aspect ratio</span><Seg value={aspect} onChange={setAspect} options={["9:16", "16:9", "1:1", "4:5"]} /></div>
                <div><span className="field-label">Reframe mode</span>
                  <div className="row" style={{ gap: 8 }}>
                    {([["pan", "flip", "Pan"], ["split", "layout", "Split"], ["center", "crop", "Center"]] as const).map(([v, ic, l]) => (
                      <button key={v} onClick={() => setReframe(v)} className="card" style={{ flex: 1, padding: "12px 0", display: "flex", flexDirection: "column", alignItems: "center", gap: 7, cursor: "pointer", borderColor: reframe === v ? "var(--accent)" : "var(--line)", background: reframe === v ? "var(--accent-soft)" : "var(--bg-2)" }}>
                        <Icon name={ic} size={20} /><span style={{ fontSize: 12, fontWeight: 600 }}>{l}</span>
                      </button>
                    ))}
                  </div>
                </div>
                <Btn variant="ghost" icon="scan" onClick={() => ctx.nav("reframe", { id })}>Open ROI editor →</Btn>
              </div>
            )}
            {insp === "Captions" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div><span className="field-label">Preset</span><div className="kbar">{["opus", "karaoke", "minimal"].map((p) => <span key={p} className={"chip" + (style === p ? " solid" : "")} style={{ cursor: "pointer", textTransform: "capitalize" }} onClick={() => setStyle(p)}>{p}</span>)}</div></div>
                <div className="card" style={{ padding: 12, textAlign: "center", background: "#0a0b0d" }}><span style={{ fontFamily: "var(--font-caption)", fontSize: 18, color: "#fff", textTransform: style === "opus" ? "uppercase" : "none" }}>{capWords[0]} <span style={{ color: hl }}>{capWords[1] || ""}</span></span></div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>Captions play live over the preview on the left. Render burns this style in; fine-tune size/colors/position in the Caption Studio.</div>
                <Btn variant="ghost" icon="type" onClick={() => ctx.nav("caption", { id })}>Open Caption Studio →</Btn>
              </div>
            )}
            {insp === "Brand" && <BrandInspector clipId={id} preset={preset} />}
            {insp === "Export" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div><span className="field-label">Export preset</span><Seg value={preset} onChange={setPreset} neutral options={[{ value: "tiktok", label: "TikTok" }, { value: "reels", label: "Reels" }, { value: "shorts", label: "Shorts" }]} /></div>
                <div className="card" style={{ padding: 13, fontSize: 12.5, color: "var(--text-dim)" }}><div className="row" style={{ marginBottom: 6 }}><span>Codec</span><span className="spacer" /><span className="mono">H.264 · VideoToolbox</span></div><div className="row" style={{ marginBottom: 6 }}><span>Resolution</span><span className="spacer" /><span className="mono">{aspect === "9:16" ? "1080×1920" : aspect === "1:1" ? "1080×1080" : aspect === "4:5" ? "1080×1350" : "1920×1080"}</span></div><div className="row"><span>Aspect</span><span className="spacer" /><span className="mono">{aspect}</span></div></div>
                <div>
                  <span className="field-label">Renders</span>
                  <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                    {renders.length === 0 ? <div style={{ fontSize: 12.5, color: "var(--text-faint)", padding: "6px 2px" }}>No renders yet — hit Render to make one.</div>
                      : renders.map((r, i) => {
                        const path = (r.result.output_path as string) || "";
                        return (
                          <div key={r.id} className="card" style={{ padding: "9px 11px", display: "flex", flexDirection: "column", gap: 6, borderColor: i === renders.length - 1 ? "var(--accent)" : "var(--line)" }}>
                            <div className="row" style={{ gap: 10 }}>
                              <span className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>v{i + 1}</span>
                              <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{(r.result.preset as string) || "render"} · {(r.result.aspect as string) || aspect}</span>
                              <span className="spacer" />
                              {i === renders.length - 1 && <Chip tone="acc">latest</Chip>}
                              <a className="btn subtle sm" style={{ height: 24, padding: "0 8px" }} href={ctx.client.renderFileUrl(id, r.result.render_id!)} download>Download</a>
                            </div>
                            {path && (
                              <div className="row" style={{ gap: 6 }}>
                                <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", direction: "rtl", textAlign: "left" }} title={path}>{path}</span>
                                <button className="iconbtn" style={{ width: 22, height: 22, flex: "none" }} title="Copy file path" onClick={() => { navigator.clipboard?.writeText(path); ctx.pushToast({ icon: "copy", tone: "ok", title: "Path copied", body: "Paste in Finder → Go to Folder (⌘⇧G)" }); }}><Icon name="copy" size={12} /></button>
                              </div>
                            )}
                          </div>
                        );
                      })}
                  </div>
                </div>
                <Btn variant="primary" icon="zap" onClick={render}>Render &amp; export</Btn>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
