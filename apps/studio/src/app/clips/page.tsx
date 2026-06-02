"use client";

import Link from "next/link";
import type { ClipJobView } from "@spool/types";
import { useEngine, useLive } from "@/lib/engine-context";
import { Badge, Button, Card, EmptyState, StatusDot, fmtDuration } from "@/components/ui";

/** S11 — Clips Library. A clip is born from a cut/pipeline job; we group the live clip-job
 *  stream by clip_id and show each clip's latest render (previewable + downloadable) or its
 *  current stage. Click through to the workspace to reframe / caption / export. */
export default function ClipsLibrary() {
  const client = useEngine();
  const { snapshot } = useLive();

  const byClip = new Map<string, ClipJobView[]>();
  for (const job of snapshot?.clips ?? []) {
    if (!job.clip_id) continue;
    (byClip.get(job.clip_id) ?? byClip.set(job.clip_id, []).get(job.clip_id)!).push(job);
  }
  const clips = [...byClip.entries()].reverse();

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Clips</h1>
        <p className="mt-1 text-sm text-text-dim">Every clip you&rsquo;ve cut — open one to reframe, caption, and export.</p>
      </header>

      {clips.length === 0 ? (
        <EmptyState
          title="No clips yet"
          hint="Find moments in a source and cut one, or run a quick render."
          action={
            <Link href="/library">
              <Button>Go to Library</Button>
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {clips.map(([clipId, jobs]) => {
            const cut = jobs.find((j) => j.kind === "cut") ?? jobs.find((j) => j.kind === "pipeline");
            const win = cut?.result;
            const lastRender = jobs
              .filter((j) => (j.kind === "export" || j.kind === "pipeline") && j.status === "done" && j.result.render_id)
              .at(-1);
            const active = jobs.find((j) => j.status === "running" || j.status === "queued");
            const stage = active ? `${active.kind}…` : lastRender ? "rendered" : "ready";

            return (
              <Card key={clipId} className="flex flex-col overflow-hidden">
                <Link href={`/clips/${clipId}`} className="block aspect-video bg-black/90">
                  {lastRender ? (
                    <video
                      src={client.renderFileUrl(clipId, lastRender.result.render_id!)}
                      preload="metadata"
                      className="h-full w-full object-contain"
                    />
                  ) : (
                    <div className="grid h-full place-items-center text-sm text-text-faint">
                      {active ? `${active.kind}…` : "not rendered"}
                    </div>
                  )}
                </Link>
                <div className="flex items-center gap-2 p-3">
                  <StatusDot status={active ? active.status : lastRender ? "done" : "ready"} pulse={!!active} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-mono text-xs text-text">{clipId}</p>
                    <p className="text-xs text-text-dim">
                      {win?.start != null && win?.end != null ? fmtDuration(win.end - win.start) : "—"} · {stage}
                    </p>
                  </div>
                  {lastRender && <Badge tone="ok">{lastRender.result.preset}</Badge>}
                  <Link href={`/clips/${clipId}`} className="text-sm font-medium text-accent hover:underline">
                    Open
                  </Link>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
