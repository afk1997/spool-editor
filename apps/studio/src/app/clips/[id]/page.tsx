"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { ClipJobView, ClipKind } from "@spool/types";
import { useEngine, useLive } from "@/lib/engine-context";
import { Badge, Button, Card, EmptyState, Segmented, StatusDot, cn, fmtDuration } from "@/components/ui";

const ASPECTS = ["9:16", "16:9", "1:1", "4:5"] as const;
const MODES = ["pan", "split", "center"] as const;
const STYLES = ["opus", "karaoke", "minimal"] as const;
const PRESETS = ["tiktok", "reels", "shorts", "youtube", "linkedin", "x"] as const;

/** Clip workspace — S7 Reframe (basic) + S8 Caption Studio (presets) + export. The full
 *  draggable ROI editor and live caption styling are Phase 2; here each step is a wired
 *  preset action against api_v1, and finished renders preview/download inline. */
export default function ClipWorkspace() {
  const { id: clipId } = useParams<{ id: string }>();
  const client = useEngine();
  const { snapshot } = useLive();

  const jobs = (snapshot?.clips ?? []).filter((c) => c.clip_id === clipId);
  const latest = (kind: ClipKind) => jobs.filter((j) => j.kind === kind).at(-1);
  const cut = jobs.find((j) => j.kind === "cut") ?? jobs.find((j) => j.kind === "pipeline");
  const window = cut?.result;

  const renders = jobs
    .filter((j) => (j.kind === "export" || j.kind === "pipeline") && j.status === "done" && j.result.render_id)
    .map((j) => ({ jobId: j.id, renderId: j.result.render_id!, preset: j.result.preset ?? "—" }))
    .reverse();

  const [aspect, setAspect] = useState<(typeof ASPECTS)[number]>("9:16");
  const [mode, setMode] = useState<(typeof MODES)[number]>("pan");
  const [style, setStyle] = useState<(typeof STYLES)[number]>("opus");
  const [preset, setPreset] = useState<(typeof PRESETS)[number]>("tiktok");

  const reframeJob = latest("reframe");
  const captionJob = latest("caption");
  const exportJob = latest("export");

  if (jobs.length === 0) {
    return (
      <div className="mx-auto max-w-3xl">
        <EmptyState
          title="Clip not found"
          hint="Cut a clip from a source's discovery panel first, then it'll show up here."
          action={
            <Link href="/clips">
              <Button variant="ghost">All clips</Button>
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div>
        <Link href="/clips" className="text-sm text-text-dim hover:text-accent">
          ← Clips
        </Link>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight">Clip workspace</h1>
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className="font-mono text-xs text-text-faint">{clipId}</span>
          {window?.start != null && window?.end != null && (
            <Badge>{fmtDuration(window.end - window.start)}</Badge>
          )}
        </div>
      </div>

      <Stage title="1 · Reframe" job={reframeJob} doneLabel={reframeJob?.result.aspect ? `${reframeJob.result.aspect} · ${reframeJob.result.reframe_mode ?? reframeJob.result.source}` : undefined}>
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Aspect">
            <Segmented options={ASPECTS} value={aspect} onChange={setAspect} ariaLabel="Aspect ratio" />
          </Field>
          <Field label="Mode">
            <Segmented options={MODES} value={mode} onChange={setMode} ariaLabel="Reframe mode" />
          </Field>
          <Button
            className="ml-auto"
            disabled={isActive(reframeJob)}
            onClick={() => void client.reframe(clipId, { aspect, mode }).catch(() => {})}
          >
            {isActive(reframeJob) ? "Reframing…" : "Reframe"}
          </Button>
        </div>
      </Stage>

      <Stage title="2 · Captions" job={captionJob} doneLabel={captionJob?.result.style}>
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Style">
            <Segmented options={STYLES} value={style} onChange={setStyle} ariaLabel="Caption style" />
          </Field>
          <Button
            className="ml-auto"
            disabled={isActive(captionJob)}
            onClick={() => void client.caption(clipId, { style }).catch(() => {})}
          >
            {isActive(captionJob) ? "Captioning…" : "Add captions"}
          </Button>
        </div>
      </Stage>

      <Stage title="3 · Export" job={exportJob} doneLabel={exportJob?.result.preset}>
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Platform preset">
            <Segmented options={PRESETS} value={preset} onChange={setPreset} ariaLabel="Platform preset" />
          </Field>
          <Button
            className="ml-auto"
            disabled={isActive(exportJob)}
            onClick={() => void client.render(clipId, { preset }).catch(() => {})}
          >
            {isActive(exportJob) ? "Exporting…" : "Export"}
          </Button>
        </div>
      </Stage>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-text-dim">Renders</h2>
        {renders.length === 0 ? (
          <EmptyState title="No renders yet" hint="Export the clip to produce a downloadable .mp4." />
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {renders.map((r) => {
              const url = client.renderFileUrl(clipId, r.renderId);
              return (
                <Card key={r.jobId} className="flex flex-col gap-2 p-3">
                  <div className="flex items-center justify-between">
                    <Badge tone="ok">{r.preset}</Badge>
                    <a href={url} download className="text-sm font-medium text-accent hover:underline">
                      Download
                    </a>
                  </div>
                  <video src={url} controls preload="metadata" className="w-full rounded bg-black" />
                </Card>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function isActive(job?: ClipJobView): boolean {
  return job?.status === "running" || job?.status === "queued";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-text-dim">{label}</span>
      {children}
    </label>
  );
}

function Stage({
  title,
  job,
  doneLabel,
  children,
}: {
  title: string;
  job?: ClipJobView;
  doneLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <h2 className="font-medium text-text">{title}</h2>
        {job && (
          <span className={cn("ml-auto flex items-center gap-1.5 text-xs", job.status === "error" ? "text-err" : "text-text-dim")}>
            <StatusDot status={job.status} pulse={isActive(job)} />
            {job.status === "done" && doneLabel ? doneLabel : job.status}
          </span>
        )}
      </div>
      {children}
      {job?.status === "error" && job.error_message && (
        <p className="text-xs text-err">{job.error_message}</p>
      )}
    </Card>
  );
}
