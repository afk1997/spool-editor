"use client";

import { useEngine, useLive } from "@/lib/engine-context";
import { Badge, Card, EmptyState, StatusDot } from "@/components/ui";
import { ClipJobRow, JobRow, type JobActions } from "@/components/queue";

/** S10 — Render Queue. The whole live work set: clip/render jobs, downloads, transcribes —
 *  all from the SSE snapshot, all cancellable/dismissable through api_v1. */
export default function QueuePage() {
  const client = useEngine();
  const { snapshot } = useLive();

  const jobs = snapshot?.jobs ?? [];
  const clips = snapshot?.clips ?? [];
  const transcripts = snapshot?.transcripts ?? [];

  const jobActions: JobActions = {
    onPause: (id) => void client.pauseJob(id).catch(() => {}),
    onResume: (id) => void client.resumeJob(id).catch(() => {}),
    onCancel: (id) => void client.cancelJob(id).catch(() => {}),
    onDismiss: (id) => void client.dismissJob(id).catch(() => {}),
  };
  const cancelClip = (id: string) => void client.cancelClipJob(id).catch(() => {});
  const dismissClip = (id: string) => void client.dismissClipJob(id).catch(() => {});

  const empty = jobs.length === 0 && clips.length === 0 && transcripts.length === 0;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Render queue</h1>
        <p className="mt-1 text-sm text-text-dim">Everything in flight — downloads, transcribes, and clip renders.</p>
      </header>

      {empty ? (
        <EmptyState title="Queue is empty" hint="Imports, transcribes, and clip renders show up here as they run." />
      ) : (
        <>
          <Section title="Clip renders" count={clips.length}>
            {clips.length > 0 && (
              <Card>
                <ul className="divide-y divide-line">
                  {clips.map((c) => (
                    <ClipJobRow key={c.id} job={c} onCancel={cancelClip} onDismiss={dismissClip} />
                  ))}
                </ul>
              </Card>
            )}
          </Section>

          <Section title="Downloads" count={jobs.length}>
            {jobs.length > 0 && (
              <Card>
                <ul className="divide-y divide-line">
                  {jobs.map((j) => (
                    <JobRow key={j.id} job={j} actions={jobActions} />
                  ))}
                </ul>
              </Card>
            )}
          </Section>

          <Section title="Transcribes" count={transcripts.length}>
            {transcripts.length > 0 && (
              <Card>
                <ul className="divide-y divide-line">
                  {transcripts.map((t) => (
                    <li key={t.id} className="flex items-center gap-4 px-4 py-3">
                      <StatusDot status={t.status} pulse={t.status === "running" || t.status === "queued"} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-text">{t.human?.summary ?? t.status}</p>
                      </div>
                      {t.speaker_count != null && <Badge tone="info">{t.speaker_count} speakers</Badge>}
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  if (count === 0) return null;
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-text-dim">
        {title}
        <span className="tabular-nums text-text-faint">{count}</span>
      </h2>
      {children}
    </section>
  );
}
