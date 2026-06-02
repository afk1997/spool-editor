"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { Btn, Chip, Icon, Progress, Seg, Switch, Thumb } from "@spool/ui";

/* ImportScreen — 1:1 port of the demo (03), wired: Resolve submits real downloads via
 * ingest.download; the Downloads list is the live jobs snapshot. A `?url=` query (e.g. from
 * Home's "Import / Paste URL") pre-fills the box. */
function ImportScreen() {
  const ctx = useSpool();
  const params = useSearchParams();
  const [tab, setTab] = useState("URL");
  const [url, setUrl] = useState(() => params.get("url") ?? "");
  const [quality, setQuality] = useState("1080p");
  const [opts, setOpts] = useState({ subs: true, chapters: true, meta: false });
  const [legal, setLegal] = useState(true);
  const downloads = ctx.downloads;

  const resolve = () => {
    const urls = url.split(/\s+/).filter(Boolean);
    if (!urls.length) return;
    for (const u of urls) {
      ctx.client.submitDownload({ url: u, format: quality === "Audio" ? "audio" : "video", auto_transcribe: true, subtitles: opts.subs, chapters: opts.chapters, embed: opts.meta }).catch(() => {});
    }
    ctx.pushToast({ icon: "download", tone: "info", title: `Downloading ${urls.length} URL${urls.length > 1 ? "s" : ""}`, body: "Progress shows below + in the queue" });
    setUrl("");
  };

  return (
    <div className="mainpad fadein">
      <div className="eyebrow" style={{ marginBottom: 6 }}>Import</div>
      <h1 style={{ fontSize: 30, marginBottom: 4 }}>Import a source</h1>
      <p style={{ color: "var(--text-faint)", marginTop: 0, marginBottom: 22 }}>Paste video URLs (downloaded with yt-dlp) or drop local files. Everything stays on your machine.</p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 24, alignItems: "start" }}>
        <div className="panel" style={{ overflow: "hidden" }}>
          <div className="tabs" style={{ padding: "0 16px" }}>
            {["URL", "Files"].map((t) => <div key={t} className={"tab" + (tab === t ? " on" : "")} onClick={() => setTab(t)}>{t === "URL" ? "Paste URL" : "Drop files"}</div>)}
          </div>
          <div style={{ padding: 20 }}>
            {tab === "URL" ? (
              <div>
                <textarea className="input" rows={3} placeholder={"https://youtube.com/watch?v=…\nPaste multiple URLs or a playlist link"} value={url} onChange={(e) => setUrl(e.target.value)} />
                <div className="row" style={{ gap: 14, marginTop: 14, flexWrap: "wrap" }}>
                  <div><span className="field-label">Quality</span><Seg value={quality} onChange={setQuality} neutral options={["Best", "1080p", "720p", "Audio"]} /></div>
                  <div className="spacer" />
                  <Btn variant="primary" icon="download" onClick={resolve} style={{ alignSelf: "flex-end" }}>Download</Btn>
                </div>
                <div className="divider" style={{ margin: "18px 0" }} />
                <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
                  {([["subs", "Download subtitles if available"], ["chapters", "Keep chapters as clip boundaries"], ["meta", "Embed metadata & thumbnail"]] as const).map(([k, l]) => (
                    <label key={k} className="row" style={{ gap: 9, cursor: "pointer", fontSize: 13 }}>
                      <Switch on={opts[k]} onClick={() => setOpts((o) => ({ ...o, [k]: !o[k] }))} /> {l}
                    </label>
                  ))}
                </div>
                <details className="trace" style={{ marginTop: 16 }}>
                  <summary><Icon name="shield" size={14} /> Authentication (advanced) · default off</summary>
                  <div className="tracebody" style={{ fontFamily: "var(--font-ui)", fontSize: 12.5, color: "var(--text-dim)" }}>
                    Use your browser cookies for member-only or age-gated videos. Spool reads them locally and never uploads them. <span style={{ color: "var(--warn)" }}>Use responsibly.</span>
                  </div>
                </details>
              </div>
            ) : (
              <div onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); ctx.pushToast({ icon: "alert", tone: "warn", title: "Local file import", body: "Drag-drop ingest lands in a later build — paste a URL for now." }); }}
                style={{ border: "1.5px dashed var(--line-str)", borderRadius: "var(--radius)", padding: "46px 20px", textAlign: "center", background: "var(--bg-2)" }}>
                <div className="ill" style={{ margin: "0 auto 14px", width: 64, height: 64, borderRadius: 18 }}><Icon name="upload" size={26} /></div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>Drop video files here</div>
                <div style={{ color: "var(--text-faint)", fontSize: 13, marginBottom: 14 }}>MP4, MOV, MKV, WebM · or browse your disk</div>
              </div>
            )}
          </div>
        </div>

        <div className="panel" style={{ padding: 16 }}>
          <div className="row" style={{ gap: 8, marginBottom: 12 }}><Icon name="shield" size={15} style={{ color: "var(--ok)" }} /><b style={{ fontSize: 13 }}>On-device</b></div>
          <p style={{ color: "var(--text-dim)", fontSize: 12.5, marginTop: 0, lineHeight: 1.55 }}>Downloads run through yt-dlp on your machine. No video leaves your computer unless you publish it.</p>
          {legal && (
            <div style={{ marginTop: 14, padding: 12, borderRadius: "var(--radius-sm)", background: "var(--warn-soft)", fontSize: 12, color: "var(--text-dim)" }}>
              <div className="row" style={{ gap: 8, marginBottom: 4 }}><Icon name="alert" size={14} style={{ color: "var(--warn)" }} /><b style={{ color: "var(--warn)" }}>Heads up</b><span className="spacer" /><Icon name="x" size={13} style={{ cursor: "pointer" }} onClick={() => setLegal(false)} /></div>
              You&rsquo;re responsible for the rights to anything you download. Respect each site&rsquo;s ToS.
            </div>
          )}
        </div>
      </div>

      <div style={{ marginTop: 26 }}>
        <div className="eyebrow" style={{ marginBottom: 12 }}>Downloads</div>
        {downloads.length === 0 ? (
          <div className="card" style={{ padding: 24, textAlign: "center", color: "var(--text-faint)", fontSize: 13 }}>Downloads appear here with live progress, speed and ETA.</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {downloads.map((d) => {
              const err = d.status === "error";
              return (
                <div key={d.id} className="card" style={{ display: "flex", gap: 14, padding: 12, alignItems: "center", borderColor: err ? "rgba(190,81,73,0.4)" : "var(--line)", background: err ? "var(--err-soft)" : "var(--bg-2)" }}>
                  <div style={{ width: 96, flex: "none", borderRadius: 8, overflow: "hidden" }}><Thumb seed={d.id} kind={d.src} label={false} /></div>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="row" style={{ marginBottom: 7, gap: 8 }}>
                      <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.title}</span>
                      <span className="spacer" />
                      {d.status === "done" ? <Chip tone="ok" dot>done</Chip>
                        : err ? <Chip tone="err" dot>failed</Chip>
                        : <span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>{Math.round(d.prog)}% · {d.size} · {d.speed} · ETA {d.eta}</span>}
                    </div>
                    {err ? <div className="row" style={{ gap: 7, fontSize: 12, color: "var(--err)" }}><Icon name="alert" size={13} />{d.err}</div>
                      : <Progress value={d.prog} tone={d.status === "done" ? "ok" : "info"} striped={d.status !== "done"} />}
                  </div>
                  {d.status === "done"
                    ? <div className="row" style={{ gap: 8 }}><Btn variant="ghost" size="sm" onClick={() => ctx.nav("project", { id: d.id })}>Open</Btn></div>
                    : err ? <Btn variant="primary" size="sm" icon="refresh" onClick={() => ctx.client.resumeJob(d.id).catch(() => {})}>Retry</Btn>
                    : <button className="iconbtn" aria-label="Pause download" onClick={() => ctx.client.pauseJob(d.id).catch(() => {})}><Icon name="pause" size={16} /></button>}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/* useSearchParams needs a Suspense boundary in a statically-rendered route. */
export default function ImportPage() {
  return (
    <Suspense fallback={null}>
      <ImportScreen />
    </Suspense>
  );
}
