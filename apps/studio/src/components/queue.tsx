"use client";

import type { ClipJobView, JobView } from "@spool/types";
import { Badge, Button, StatusDot, cn } from "./ui";

/** Render-queue rows shared by the Import and Queue screens. All data is the live SSE
 *  snapshot; actions call the engine and the next snapshot reflects them. */

const ACTIVE_JOB = new Set(["downloading", "queued", "running", "paused"]);
const TERMINAL = new Set(["done", "error", "failed", "cancelled"]);

function ProgressBar({ pct, active }: { pct: number; active: boolean }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-bg-3" aria-hidden>
      <div
        className={cn("h-full rounded-full", active ? "bg-info" : "bg-accent")}
        style={{ width: `${Math.max(0, Math.min(100, pct))}%`, transition: "width 200ms" }}
      />
    </div>
  );
}

export interface JobActions {
  onPause?: (id: string) => void;
  onResume?: (id: string) => void;
  onCancel?: (id: string) => void;
  onDismiss?: (id: string) => void;
}

export function JobRow({ job, actions }: { job: JobView; actions?: JobActions }) {
  const active = ACTIVE_JOB.has(job.status);
  return (
    <li className="flex items-center gap-4 px-4 py-3">
      <StatusDot status={job.status} pulse={active} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium text-text">{job.title || job.url}</span>
          {job.auto_transcribe && <Badge tone="info">auto-transcribe</Badge>}
        </div>
        <p className="truncate text-xs text-text-dim">{job.human?.summary ?? job.status}</p>
        {active && <div className="mt-1.5"><ProgressBar pct={job.progress_pct} active /></div>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {job.status === "downloading" && actions?.onPause && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => actions.onPause!(job.id)}>Pause</Button>
        )}
        {job.status === "paused" && actions?.onResume && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => actions.onResume!(job.id)}>Resume</Button>
        )}
        {active && actions?.onCancel && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => actions.onCancel!(job.id)}>Cancel</Button>
        )}
        {TERMINAL.has(job.status) && actions?.onDismiss && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => actions.onDismiss!(job.id)}>Dismiss</Button>
        )}
      </div>
    </li>
  );
}

export function ClipJobRow({ job, onCancel, onDismiss }: { job: ClipJobView; onCancel?: (id: string) => void; onDismiss?: (id: string) => void }) {
  const active = job.status === "running" || job.status === "queued";
  const tone = job.status === "error" ? "err" : job.status === "done" ? "ok" : "info";
  return (
    <li className="flex items-center gap-4 px-4 py-3">
      <StatusDot status={job.status} pulse={active} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Badge tone={tone}>{job.kind}</Badge>
          {job.clip_id && <span className="font-mono text-xs text-text-faint">{job.clip_id}</span>}
          {job.stage && active && <span className="text-xs text-text-dim">· {job.stage}</span>}
        </div>
        <p className="truncate text-xs text-text-dim">{job.human?.summary ?? job.status}</p>
        {active && <div className="mt-1.5"><ProgressBar pct={job.progress_pct} active /></div>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {active && onCancel && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => onCancel(job.id)}>Cancel</Button>
        )}
        {!active && onDismiss && (
          <Button variant="ghost" className="min-h-9 px-2.5" onClick={() => onDismiss(job.id)}>Dismiss</Button>
        )}
      </div>
    </li>
  );
}
