"use client";

import { useState } from "react";
import { useSpool, type SpoolJob } from "@/components/spool/context";
import { Btn, Chip, Empty, Icon, Progress, Seg, Thumb } from "@/components/spool/ui";

/* S10 Render Queue — 1:1 port of the demo (06). The whole live work set (downloads,
 * transcribes, clip/render jobs) from the SSE snapshot; cancel/dismiss/retry route to the
 * right engine surface by domain. The log panel shows the job's real stage + error. */

export default function QueueScreen() {
  const ctx = useSpool();
  const [filter, setFilter] = useState("all");
  const [openLog, setOpenLog] = useState<string | null>(null);
  const all = ctx.jobs;
  const jobs = all.filter((j) => filter === "all" || (filter === "running" && j.status === "running") || (filter === "queued" && j.status === "queued") || (filter === "done" && j.status === "done") || (filter === "failed" && j.status === "failed"));
  const typeIcon: Record<string, string> = { render: "film", transcribe: "type", download: "download" };
  const statusChip = (j: SpoolJob) => ({ running: <Chip tone="info" dot>running</Chip>, queued: <Chip tone="warn" dot>queued</Chip>, done: <Chip tone="ok" dot>done</Chip>, failed: <Chip tone="err" dot>failed</Chip> }[j.status] ?? null);

  const cancel = (j: SpoolJob) => { if (j.domain === "download") ctx.client.cancelJob(j.id).catch(() => {}); else if (j.domain === "clip") ctx.client.cancelClipJob(j.id).catch(() => {}); };
  const dismiss = (j: SpoolJob) => { if (j.domain === "download") ctx.client.dismissJob(j.id).catch(() => {}); else if (j.domain === "clip") ctx.client.dismissClipJob(j.id).catch(() => {}); };
  const retry = (j: SpoolJob) => { if (j.domain === "download") ctx.client.resumeJob(j.id).catch(() => {}); else dismiss(j); };
  const clearFinished = () => { all.filter((j) => j.status === "done" || j.status === "failed").forEach(dismiss); ctx.pushToast({ icon: "trash", tone: "info", title: "Cleared finished jobs" }); };
  const pauseAll = () => { all.filter((j) => j.domain === "download" && j.status === "running").forEach((j) => ctx.client.pauseJob(j.id).catch(() => {})); };
  const retryFailed = () => all.filter((j) => j.status === "failed").forEach(retry);

  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Render Queue</div><h1 style={{ fontSize: 30 }}>Jobs</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" size="sm" icon="pause" onClick={pauseAll}>Pause all</Btn>
        <Btn variant="ghost" size="sm" icon="refresh" onClick={retryFailed}>Retry failed</Btn>
        <Btn variant="ghost" size="sm" icon="trash" onClick={clearFinished}>Clear finished</Btn>
      </div>
      <div className="row" style={{ gap: 10, marginBottom: 18 }}>
        <Seg value={filter} onChange={setFilter} neutral options={[{ value: "all", label: "All" }, { value: "running", label: "Running" }, { value: "queued", label: "Queued" }, { value: "done", label: "Done" }, { value: "failed", label: "Failed" }]} />
        <span className="spacer" />
        <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>{all.filter((j) => j.status === "running").length} active · on-device encode</span>
      </div>

      <div className="panel" style={{ overflow: "hidden" }}>
        {jobs.map((j, idx) => (
          <div key={j.id}>
            <div className="row" style={{ padding: "13px 16px", gap: 14, borderBottom: idx < jobs.length - 1 ? "1px solid var(--line-2)" : "none", background: j.err ? "var(--err-soft)" : "transparent" }}>
              <div style={{ width: 30, height: 30, borderRadius: 8, background: "var(--bg-3)", display: "grid", placeItems: "center", flex: "none", color: j.status === "failed" ? "var(--err)" : "var(--accent)" }}><Icon name={typeIcon[j.type] || "film"} size={15} /></div>
              <div style={{ width: 72, flex: "none", borderRadius: 7, overflow: "hidden" }}><Thumb seed={j.src} kind="" label={false} /></div>
              <div className="grow" style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.label}</div>
                {j.status === "running" ? <Progress value={j.prog} striped /> : <div className="mono" style={{ fontSize: 11.5, color: j.status === "failed" ? "var(--err)" : "var(--text-faint)" }}>{j.stage}</div>}
              </div>
              <div style={{ width: 120, flex: "none", textAlign: "right" }}>
                {statusChip(j)}
                {j.status === "running" && <div className="mono" style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 5 }}>{Math.round(j.prog)}%{j.eta !== "—" ? ` · ETA ${j.eta}` : ""}</div>}
                {j.status === "done" && <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 5 }}>{j.elapsed}</div>}
              </div>
              <div className="row" style={{ gap: 4, flex: "none" }}>
                <button className="iconbtn" onClick={() => setOpenLog(openLog === j.id ? null : j.id)} title="Logs"><Icon name="terminal" size={15} /></button>
                {j.status === "failed" ? <button className="iconbtn" title="Retry" onClick={() => retry(j)}><Icon name="refresh" size={15} /></button>
                  : j.status === "done" ? <button className="iconbtn" title="Open" onClick={() => ctx.nav("clips")}><Icon name="arrowR" size={15} /></button>
                  : <button className="iconbtn" title="Cancel" onClick={() => cancel(j)}><Icon name="x" size={15} /></button>}
              </div>
            </div>
            {openLog === j.id && (
              <div className="mono" style={{ padding: "12px 16px 14px 60px", background: "#070809", fontSize: 11.5, color: "var(--text-faint)", borderBottom: "1px solid var(--line-2)", lineHeight: 1.7 }}>
                {[`$ ${j.type === "download" ? "yt-dlp" : j.type === "transcribe" ? "whisper" : "ffmpeg"} · ${j.id}`,
                  `stage: ${j.stage}`,
                  j.err ? `error: ${j.stage}` : `progress: ${Math.round(j.prog)}%${j.elapsed !== "—" ? ` · elapsed ${j.elapsed}` : ""}`,
                  j.err ? "  ↳ hint: open the engine log (/tmp/spool_engine.log) for the full trace" : "intermediates kept in ~/Spool/.cache",
                ].map((l, i) => <div key={i} style={{ color: j.err && i >= 2 ? "var(--err)" : "inherit" }}>{l}</div>)}
              </div>
            )}
          </div>
        ))}
        {jobs.length === 0 && <div style={{ padding: 30 }}><Empty icon="layers" title="Nothing here">No jobs match this filter.</Empty></div>}
      </div>
    </div>
  );
}
