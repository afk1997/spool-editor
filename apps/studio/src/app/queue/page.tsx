"use client";

import { useRef, useState } from "react";
import { useSpool, type SpoolJob } from "@/components/spool/context";
import { describeActionError } from "@/lib/action-error";
import { Btn, Chip, Empty, Icon, Progress, Seg, Thumb } from "@spool/ui";

type PendingLabel = "Cancelling…" | "Dismissing…" | "Pausing…" | "Resuming…";
type BatchKey = "pause-all" | "clear-finished";

const jobKey = (job: SpoolJob) => `${job.domain}:${job.id}`;

/* S10 Render Queue — 1:1 port of the demo (06). The whole live work set (downloads,
 * transcribes, clip/render jobs) from the SSE snapshot; cancel/dismiss/pause/resume route to the
 * right engine surface by domain. The log panel shows the job's real stage + error. */

export default function QueueScreen() {
  const ctx = useSpool();
  const [filter, setFilter] = useState("all");
  const [openLog, setOpenLog] = useState<string | null>(null);
  const pendingJobsRef = useRef(new Map<string, PendingLabel>());
  const pendingBatchesRef = useRef(new Set<BatchKey>());
  const [pendingJobs, setPendingJobs] = useState(new Map<string, PendingLabel>());
  const [pendingBatches, setPendingBatches] = useState(new Set<BatchKey>());
  const all = ctx.jobs;
  const jobs = all.filter((j) => filter === "all" || (filter === "running" && j.status === "running") || (filter === "queued" && j.status === "queued") || (filter === "done" && j.status === "done") || (filter === "failed" && j.status === "failed") || (filter === "cancelled" && j.status === "cancelled"));
  const typeIcon: Record<string, string> = { render: "film", analysis: "scan", transcribe: "type", download: "download" };
  const statusChip = (j: SpoolJob) => ({ running: <Chip tone="info" dot>running</Chip>, queued: <Chip tone="warn" dot>queued</Chip>, paused: <Chip tone="warn" dot>paused</Chip>, done: <Chip tone="ok" dot>done</Chip>, failed: <Chip tone="err" dot>failed</Chip>, cancelled: <Chip tone="warn" dot>cancelled</Chip> }[j.status] ?? null);

  const claimJobs = (targets: SpoolJob[], label: PendingLabel) => {
    const claimed = targets.filter((job) => {
      const key = jobKey(job);
      if (pendingJobsRef.current.has(key)) return false;
      pendingJobsRef.current.set(key, label);
      return true;
    });
    if (claimed.length) setPendingJobs(new Map(pendingJobsRef.current));
    return claimed;
  };

  const releaseJobs = (targets: SpoolJob[]) => {
    targets.forEach((job) => pendingJobsRef.current.delete(jobKey(job)));
    if (targets.length) setPendingJobs(new Map(pendingJobsRef.current));
  };

  const beginBatch = (batch: BatchKey) => {
    if (pendingBatchesRef.current.has(batch)) return false;
    pendingBatchesRef.current.add(batch);
    setPendingBatches(new Set(pendingBatchesRef.current));
    return true;
  };

  const endBatch = (batch: BatchKey) => {
    pendingBatchesRef.current.delete(batch);
    setPendingBatches(new Set(pendingBatchesRef.current));
  };

  const reportFailure = (title: string, error: unknown) => {
    const failure = describeActionError(error);
    ctx.pushToast({ icon: "alert", tone: "warn", title, body: `${failure.code}: ${failure.message}` });
  };

  const cancel = async (j: SpoolJob) => {
    try {
      if (j.domain === "download") await ctx.client.cancelJob(j.id);
      else if (j.domain === "clip") await ctx.client.cancelClipJob(j.id);
      else await ctx.client.cancelTranscript(j.id);
    } catch (error) {
      reportFailure("Couldn't cancel job", error);
    }
  };
  const dismiss = async (j: SpoolJob) => {
    try {
      if (j.domain === "download") await ctx.client.dismissJob(j.id);
      else if (j.domain === "clip") await ctx.client.dismissClipJob(j.id);
      else await ctx.client.dismissTranscript(j.id);
    } catch (error) {
      reportFailure("Couldn't dismiss job", error);
    }
  };
  const pause = async (j: SpoolJob) => {
    try {
      await ctx.client.pauseJob(j.id);
    } catch (error) {
      reportFailure("Couldn't pause download", error);
    }
  };
  const resume = async (j: SpoolJob) => {
    try {
      await ctx.client.resumeJob(j.id);
    } catch (error) {
      reportFailure("Couldn't resume download", error);
    }
  };

  const runRowAction = async (
    job: SpoolJob,
    label: PendingLabel,
    action: () => Promise<void>,
  ) => {
    const claimed = claimJobs([job], label);
    if (!claimed.length) return;
    try {
      await action();
    } finally {
      releaseJobs(claimed);
    }
  };

  const runBatch = async (
    title: string,
    targets: SpoolJob[],
    action: (job: SpoolJob) => Promise<unknown>,
  ) => {
    const results = await Promise.allSettled(targets.map(action));
    const succeeded = results.filter((r) => r.status === "fulfilled").length;
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
    const firstFailure = failures[0] ? describeActionError(failures[0].reason) : null;
    ctx.pushToast({
      icon: failures.length ? "alert" : "check",
      tone: failures.length ? "warn" : "info",
      title,
      body: `${succeeded} succeeded · ${failures.length} failed${firstFailure ? ` · ${firstFailure.code}: ${firstFailure.message}` : ""}`,
    });
  };

  const runGuardedBatch = async (
    batch: BatchKey,
    title: string,
    targets: SpoolJob[],
    label: PendingLabel,
    action: (job: SpoolJob) => Promise<unknown>,
  ) => {
    if (!beginBatch(batch)) return;
    const claimed = claimJobs(targets, label);
    try {
      if (claimed.length) await runBatch(title, claimed, action);
    } finally {
      releaseJobs(claimed);
      endBatch(batch);
    }
  };

  const clearFinished = async () => {
    const targets = all.filter((j) => j.status === "done" || j.status === "failed" || j.status === "cancelled");
    await runGuardedBatch(
      "clear-finished",
      "Finished-job cleanup settled",
      targets,
      "Dismissing…",
      async (j) => {
        if (j.domain === "download") await ctx.client.dismissJob(j.id);
        else if (j.domain === "clip") await ctx.client.dismissClipJob(j.id);
        else await ctx.client.dismissTranscript(j.id);
      },
    );
  };
  const pauseAll = async () => {
    const targets = all.filter((j) => j.domain === "download" && j.status === "running");
    await runGuardedBatch(
      "pause-all",
      "Pause requests settled",
      targets,
      "Pausing…",
      (j) => ctx.client.pauseJob(j.id),
    );
  };

  const openCompleted = (j: SpoolJob) => {
    if (j.domain === "download" || j.domain === "transcribe") {
      ctx.nav("project", { id: j.src || j.id });
      return;
    }
    const raw = ctx.snapshot?.clips.find((clipJob) => clipJob.id === j.id);
    if (raw?.clip_id) ctx.nav("editor", { id: raw.clip_id });
    else if (raw?.source_id) ctx.nav("project", { id: raw.source_id, tab: "Clips" });
    else ctx.nav("clips");
  };

  /** The right action button for a row, honoring what the engine can actually do per domain. */
  function rowAction(j: SpoolJob) {
    const pending = pendingJobs.get(jobKey(j));
    if (pending) {
      const icon: Record<PendingLabel, string> = {
        "Cancelling…": "x",
        "Dismissing…": "trash",
        "Pausing…": "pause",
        "Resuming…": "play",
      };
      return <button className="iconbtn" title={pending} aria-label={pending} aria-busy disabled><Icon name={icon[pending]} size={15} /></button>;
    }
    if (j.status === "failed" || j.status === "cancelled") return <button className="iconbtn" title="Dismiss" aria-label="Dismiss" onClick={() => { void runRowAction(j, "Dismissing…", () => dismiss(j)); }}><Icon name="trash" size={15} /></button>;
    if (j.status === "paused") return <button className="iconbtn" title="Resume" aria-label="Resume" onClick={() => { void runRowAction(j, "Resuming…", () => resume(j)); }}><Icon name="play" size={15} /></button>;
    if (j.status === "done") return <button className="iconbtn" title="Open" aria-label="Open" onClick={() => openCompleted(j)}><Icon name="arrowR" size={15} /></button>;
    if (j.status === "running" && j.domain === "download") return <button className="iconbtn" title="Pause" aria-label="Pause" onClick={() => { void runRowAction(j, "Pausing…", () => pause(j)); }}><Icon name="pause" size={15} /></button>;
    return <button className="iconbtn" title="Cancel" aria-label="Cancel" onClick={() => { void runRowAction(j, "Cancelling…", () => cancel(j)); }}><Icon name="x" size={15} /></button>;
  }

  const pauseAllPending = pendingBatches.has("pause-all");
  const clearFinishedPending = pendingBatches.has("clear-finished");
  const canPauseAll = all.some((j) => j.domain === "download" && j.status === "running" && !pendingJobs.has(jobKey(j)));
  const canClearFinished = all.some((j) => (j.status === "done" || j.status === "failed" || j.status === "cancelled") && !pendingJobs.has(jobKey(j)));

  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Render Queue</div><h1 style={{ fontSize: 30 }}>Jobs</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" size="sm" icon="pause" onClick={pauseAll} disabled={pauseAllPending || !canPauseAll} aria-busy={pauseAllPending}>{pauseAllPending ? "Pausing…" : "Pause all"}</Btn>
        <Btn variant="ghost" size="sm" icon="trash" onClick={clearFinished} disabled={clearFinishedPending || !canClearFinished} aria-busy={clearFinishedPending}>{clearFinishedPending ? "Clearing…" : "Clear finished"}</Btn>
      </div>
      <div className="row" style={{ gap: 10, marginBottom: 18 }}>
        <Seg value={filter} onChange={setFilter} neutral options={[{ value: "all", label: "All" }, { value: "running", label: "Running" }, { value: "queued", label: "Queued" }, { value: "done", label: "Done" }, { value: "failed", label: "Failed" }, { value: "cancelled", label: "Cancelled" }]} />
        <span className="spacer" />
        <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>{all.filter((j) => j.status === "running").length} active</span>
      </div>

      <div className="panel" style={{ overflow: "hidden" }}>
        {jobs.map((j, idx) => (
          // Off-screen queue rows skip render/layout (native windowing for a long queue);
          // contain-intrinsic-size reserves a collapsed row's height (auto remembers expanded).
          <div key={j.id} style={{ contentVisibility: "auto", containIntrinsicSize: "auto 58px" }}>
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
                {rowAction(j)}
              </div>
            </div>
            {openLog === j.id && (
              <div className="mono" style={{ padding: "12px 16px 14px 60px", background: "#070809", fontSize: 11.5, color: "var(--text-faint)", borderBottom: "1px solid var(--line-2)", lineHeight: 1.7 }}>
                {[`job: ${j.id}`,
                  `domain: ${j.domain}`,
                  `status: ${j.status}`,
                  `stage: ${j.stage}`,
                  j.err ? `error: ${j.errorCode && j.errorMessage ? `${j.errorCode}: ${j.errorMessage}` : j.errorMessage || j.errorCode || j.stage}` : `progress: ${Math.round(j.prog)}%${j.elapsed !== "—" ? ` · elapsed ${j.elapsed}` : ""}`,
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
