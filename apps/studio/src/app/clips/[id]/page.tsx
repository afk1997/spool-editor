"use client";

import { useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useSpool, type SpoolClip } from "@/components/spool/context";
import { useLive } from "@/lib/engine-context";
import { Btn, Chip, Empty, Icon, Seg, Switch, Thumb } from "@spool/ui";

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
  const videoRef = useRef<HTMLVideoElement>(null);

  const render = () => ctx.makeClipsFrom([{ id }], { aspect, mode: reframe, preset });
  const capWords = (clip.title || "your caption here").split(" ").slice(0, 8);
  const renders = (snapshot?.clips ?? []).filter((c) => c.clip_id === id && (c.kind === "export" || c.kind === "pipeline") && c.status === "done" && c.result.render_id);
  const latestRender = renders.at(-1);
  const renderSrc = latestRender ? ctx.client.renderFileUrl(id, latestRender.result.render_id!) : null;
  const togglePlay = () => { const v = videoRef.current; if (v) { if (v.paused) void v.play(); else v.pause(); setPlaying(!v.paused); } else setPlaying((p) => !p); };
  const others = ctx.clips.filter((c) => c.src === clip.src && c.id !== clip.id).slice(0, 4);

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
            <div style={{ height: "100%", aspectRatio: aspect === "9:16" ? "9/16" : aspect === "1:1" ? "1/1" : aspect === "4:5" ? "4/5" : "16/9", maxHeight: "52vh", borderRadius: 10, overflow: "hidden", position: "relative", border: "1px solid var(--line-str)" }}>
              {renderSrc ? (
                /* a rendered clip: play the REAL .mp4 (captions already burned in) */
                <video ref={videoRef} src={renderSrc} controls playsInline onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                  style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain", background: "#000" }} />
              ) : (
                /* not rendered yet: the framing placeholder + caption preview */
                <>
                  <Thumb seed={clip.id} vertical={aspect === "9:16"} kind="" label={false} />
                  {safe && <div style={{ position: "absolute", inset: "8% 6%", border: "1px dashed rgba(255,255,255,0.3)", borderRadius: 6 }} />}
                  <div style={{ position: "absolute", left: 0, right: 0, bottom: "16%", textAlign: "center", fontFamily: "var(--font-caption)", fontSize: 18, color: "#fff", textShadow: "0 2px 6px #000", padding: "0 8%", lineHeight: 1.15 }}>
                    {capWords.map((w, i) => <span key={i} style={{ color: i === 1 ? "var(--caption-hl)" : "#fff" }}>{w} </span>)}
                  </div>
                </>
              )}
              <div className="badge" style={{ position: "absolute", top: 8, left: 8 }}>{renderSrc ? "rendered" : "preview"}</div>
            </div>
          </div>
          <div className="row" style={{ gap: 14, padding: "10px 18px", borderTop: "1px solid var(--line)", flex: "none" }}>
            <button className="iconbtn" onClick={togglePlay} style={{ background: "var(--accent)", color: "var(--accent-ink)" }}><Icon name={playing ? "pause" : "play"} size={16} /></button>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>{renderSrc ? "playing the rendered clip" : "render to preview"}</span>
            <span className="spacer" />
            <label className="row" style={{ gap: 7, fontSize: 12.5, cursor: "pointer" }}><Switch on={safe} onClick={() => setSafe(!safe)} /> Safe zones</label>
          </div>
          <div style={{ flex: "none", borderTop: "1px solid var(--line)", background: "var(--bg-1)", padding: "14px 18px" }}>
            <div className="row" style={{ marginBottom: 8 }}><span className="eyebrow">Timeline</span><span className="spacer" /><span className="chip warn">Phase 2</span></div>
            <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>Word-level scrub, trim-to-render, ripple-cut (delete a caption word → cut the video), A/B versions and the energy / scene / speaker lanes are the Phase-2 editor. Today: set the format &amp; preset on the right, then Render.</div>
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
                <div><span className="field-label">Preset</span><div className="kbar">{["opus", "karaoke", "minimal"].map((p) => <span key={p} className={"chip" + (clip.style === p ? " solid" : "")} style={{ cursor: "pointer" }}>{p}</span>)}</div></div>
                <div className="card" style={{ padding: 12, textAlign: "center", background: "#0a0b0d" }}><span style={{ fontFamily: "var(--font-caption)", fontSize: 18, color: "#fff" }}>{capWords[0]} <span style={{ color: "var(--caption-hl)" }}>{capWords[1] || ""}</span></span></div>
                <Btn variant="ghost" icon="type" onClick={() => ctx.nav("caption", { id })}>Open Caption Studio →</Btn>
              </div>
            )}
            {insp === "Brand" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div className="row"><span className="field-label" style={{ margin: 0 }}>Brand kits</span><span className="spacer" /><span className="chip warn">Phase 2</span></div>
                <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>Persisted brand kits (fonts, palette, watermark, lower-third) that re-style a render are a Phase-2 feature.</div>
                <Btn variant="ghost" icon="palette" onClick={() => ctx.nav("brand")}>Preview the Brand Kit designer →</Btn>
              </div>
            )}
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
