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
  const [pos, setPos] = useState(34);
  const [aspect, setAspect] = useState(clip.aspect || "9:16");
  const [reframe, setReframe] = useState("pan");
  const [preset, setPreset] = useState(clip.platform || "tiktok");
  const [safe, setSafe] = useState(true);
  const [ab, setAb] = useState("A");
  const [trimIn, setTrimIn] = useState(8), [trimOut, setTrimOut] = useState(86);
  const [cut, setCut] = useState<Record<number, boolean>>({});
  const trimRef = useRef<HTMLDivElement>(null);
  const maskInRef = useRef<HTMLDivElement>(null), maskOutRef = useRef<HTMLDivElement>(null);
  const edgeInRef = useRef<HTMLDivElement>(null), edgeOutRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  const render = () => ctx.makeClipsFrom([{ id }], { aspect, mode: reframe, preset });
  const capWords = (clip.title || "your caption here").split(" ").slice(0, 8);
  const renders = (snapshot?.clips ?? []).filter((c) => c.clip_id === id && (c.kind === "export" || c.kind === "pipeline") && c.status === "done" && c.result.render_id);
  const latestRender = renders.at(-1);
  const renderSrc = latestRender ? ctx.client.renderFileUrl(id, latestRender.result.render_id!) : null;
  const togglePlay = () => { const v = videoRef.current; if (v) { if (v.paused) void v.play(); else v.pause(); setPlaying(!v.paused); } else setPlaying((p) => !p); };
  // §6.3: update the trim mask/edge elements imperatively during the drag; commit to state on release.
  const dragTrim = (e: React.PointerEvent, which: "in" | "out") => {
    e.preventDefault();
    const rect = trimRef.current!.getBoundingClientRect();
    const sIn = trimIn, sOut = trimOut;
    let latest = which === "in" ? sIn : sOut;
    const move = (ev: PointerEvent) => {
      let p = ((ev.clientX - rect.left) / rect.width) * 100; p = Math.max(0, Math.min(100, p));
      if (which === "in") { p = Math.min(sOut - 5, Math.max(0, p)); latest = p; if (maskInRef.current) maskInRef.current.style.width = p + "%"; if (edgeInRef.current) edgeInRef.current.style.left = `calc(${p}% - 5px)`; }
      else { p = Math.max(sIn + 5, Math.min(100, p)); latest = p; if (maskOutRef.current) maskOutRef.current.style.width = 100 - p + "%"; if (edgeOutRef.current) edgeOutRef.current.style.left = `calc(${p}% - 5px)`; }
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); if (which === "in") setTrimIn(latest); else setTrimOut(latest); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  };
  const trimSecs = Math.round(clip.dur * (trimOut - trimIn) / 100);
  const lane = (label: string, color: string, children: React.ReactNode) => (
    <div className="row" style={{ gap: 0, alignItems: "stretch", borderBottom: "1px solid var(--line-2)" }}>
      <div style={{ width: 92, flex: "none", padding: "8px 12px", fontSize: 11, color: "var(--text-faint)", borderRight: "1px solid var(--line)", display: "flex", alignItems: "center", gap: 7 }}><span style={{ width: 7, height: 7, borderRadius: 2, background: color }} />{label}</div>
      <div style={{ flex: 1, position: "relative", minHeight: 38, display: "flex", alignItems: "center", padding: "0 6px" }}>{children}</div>
    </div>
  );
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
                  <div style={{ position: "absolute", left: 0, right: 0, bottom: ab === "B" ? "23%" : "16%", textAlign: "center", fontFamily: "var(--font-caption)", fontSize: 18, color: "#fff", textShadow: "0 2px 6px #000", padding: "0 8%", lineHeight: 1.15 }}>
                    {capWords.filter((_, i) => !cut[i]).map((w, i) => <span key={i} style={{ color: i === 1 ? (ab === "B" ? "#37E2A0" : "var(--caption-hl)") : "#fff" }}>{w} </span>)}
                  </div>
                </>
              )}
              <div className="badge" style={{ position: "absolute", top: 8, left: 8 }}>{renderSrc ? "rendered" : "preview"}</div>
            </div>
          </div>
          <div className="row" style={{ gap: 14, padding: "10px 18px", borderTop: "1px solid var(--line)", flex: "none" }}>
            <button className="iconbtn" onClick={togglePlay} style={{ background: "var(--accent)", color: "var(--accent-ink)" }}><Icon name={playing ? "pause" : "play"} size={16} /></button>
            <span className="mono" style={{ fontSize: 12 }}>00:{String(Math.floor(pos * 0.52)).padStart(2, "0")} / 00:{Math.round(clip.dur)}</span>
            <span className="spacer" />
            <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>trim {trimSecs}s</span>
            <label className="row" style={{ gap: 7, fontSize: 12.5, cursor: "pointer" }}><Switch on={safe} onClick={() => setSafe(!safe)} /> Safe zones</label>
            <div className="row" style={{ gap: 6, fontSize: 11.5, color: "var(--text-faint)" }}>A/B<Seg value={ab} onChange={setAb} neutral options={[{ value: "A", label: "A" }, { value: "B", label: "B" }]} /></div>
            <button className="iconbtn"><Icon name="expand" size={16} /></button>
          </div>
          <div style={{ flex: "none", borderTop: "1px solid var(--line)", background: "var(--bg-1)", maxHeight: "34vh", overflow: "auto" }}>
            <div className="row" style={{ padding: "7px 12px", gap: 10, borderBottom: "1px solid var(--line)", position: "sticky", top: 0, background: "var(--bg-1)", zIndex: 2 }}>
              <span className="eyebrow">Timeline</span><span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)" }}>snap: word</span><span className="spacer" />
              <button className="iconbtn" style={{ width: 26, height: 26 }}><Icon name="minus" size={14} /></button><button className="iconbtn" style={{ width: 26, height: 26 }}><Icon name="plus" size={14} /></button>
            </div>
            <div style={{ position: "relative" }}>
              <div style={{ position: "absolute", left: `calc(92px + ${pos}%)`, top: 0, bottom: 0, width: 2, background: "var(--accent)", zIndex: 3, pointerEvents: "none" }}><div style={{ position: "absolute", top: -1, left: -4, width: 10, height: 10, borderRadius: "50%", background: "var(--accent)" }} /></div>
              <div ref={trimRef} style={{ position: "absolute", left: 92, right: 0, top: 0, bottom: 0, zIndex: 2 }}>
                <div ref={maskInRef} className="trim-mask" style={{ left: 0, width: trimIn + "%" }} />
                <div ref={maskOutRef} className="trim-mask" style={{ right: 0, width: (100 - trimOut) + "%" }} />
                <div ref={edgeInRef} className="trim-edge" style={{ left: `calc(${trimIn}% - 5px)` }} onPointerDown={(e) => dragTrim(e, "in")} title="Trim in" />
                <div ref={edgeOutRef} className="trim-edge" style={{ left: `calc(${trimOut}% - 5px)` }} onPointerDown={(e) => dragTrim(e, "out")} title="Trim out" />
              </div>
              {lane("Video", "var(--text-dim)", <div className="row" style={{ gap: 3, flex: 1 }}>{Array.from({ length: 10 }).map((_, i) => <div key={i} style={{ flex: 1, height: 30, borderRadius: 3, overflow: "hidden" }}><Thumb seed={clip.id + i} kind="" label={false} /></div>)}</div>)}
              {lane("Captions", "var(--caption-hl)", <div className="kbar">{capWords.map((w, i) => <span key={i} className={"chip" + (cut[i] ? " cut-word" : "")} style={{ height: 22, fontSize: 10.5, cursor: "pointer" }} onClick={() => setCut((c) => ({ ...c, [i]: !c[i] }))} title="Click to delete this word (ripple-cuts the video)">{w}</span>)}</div>)}
              {lane("Speaker", "var(--roi-l)", <div className="row" style={{ gap: 2, flex: 1, height: 22 }}>{([["L", 30], ["R", 18], ["L", 34], ["R", 18]] as const).map(([sp, w], i) => <div key={i} style={{ flex: w, borderRadius: 3, background: sp === "L" ? "color-mix(in srgb,var(--roi-l) 30%,var(--bg-3))" : "color-mix(in srgb,var(--roi-r) 30%,var(--bg-3))", display: "grid", placeItems: "center", fontFamily: "var(--font-mono)", fontSize: 9 }}>{sp}</div>)}</div>)}
              {lane("Energy", "var(--ok)", <svg width="100%" height="26" preserveAspectRatio="none" viewBox="0 0 300 26">{Array.from({ length: 60 }).map((_, i) => { const h = Math.round(4 + Math.abs(Math.sin(i * 0.7)) * 20); return <rect key={i} x={i * 5} y={Math.round((26 - h) / 2)} width="3" height={h} fill="var(--ok)" opacity="0.5" />; })}</svg>)}
              {lane("Scenes", "var(--warn)", <div className="row" style={{ flex: 1, position: "relative", height: 22 }}>{[20, 68].map((p, i) => <div key={i} style={{ position: "absolute", left: p + "%", top: 0, bottom: 0, width: 2, background: "var(--warn)" }} />)}</div>)}
            </div>
            <input type="range" min="0" max="100" value={pos} onChange={(e) => setPos(+e.target.value)} style={{ width: "calc(100% - 100px)", margin: "8px 0 8px 96px", accentColor: "var(--accent)" }} />
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
                <span className="field-label">Apply brand kit</span>
                {["No kit", "Acme Media", "Lena Builds"].map((k, i) => <div key={k} className="card" style={{ padding: "11px 13px", cursor: "pointer", borderColor: i === 1 ? "var(--accent)" : "var(--line)" }}><div className="row" style={{ gap: 9 }}><Icon name="palette" size={15} style={{ color: "var(--accent)" }} />{k}{i === 1 && <span className="spacer" />}{i === 1 && <Icon name="check" size={15} style={{ color: "var(--accent)" }} />}</div></div>)}
                <Btn variant="ghost" icon="palette" onClick={() => ctx.nav("brand")}>Manage brand kits →</Btn>
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
