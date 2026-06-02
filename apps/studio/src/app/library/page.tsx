"use client";

import { useState } from "react";
import Link from "next/link";
import type { JobView, TranscribeJobView } from "@spool/types";
import { useEngine, useLive } from "@/lib/engine-context";
import { Badge, Button, Card, EmptyState, fmtDuration } from "@/components/ui";

/** S3 — Library (sources). Every downloaded media job is a source; we pair it with its
 *  transcript (if any) from the same live snapshot and offer the next real action —
 *  transcribe, or find moments. All wired to api_v1 (spec §6.2). */
export default function LibraryPage() {
  const client = useEngine();
  const { snapshot } = useLive();
  const [notice, setNotice] = useState<Record<string, string>>({});

  const sources = (snapshot?.jobs ?? []).filter((j) => j.status === "done" && j.filename);
  const transcripts = snapshot?.transcripts ?? [];
  const transcriptFor = (id: string): TranscribeJobView | undefined =>
    transcripts.filter((t) => t.parent_job_id === id).sort((a, b) => b.elapsed_seconds - a.elapsed_seconds)[0];

  function note(id: string, msg: string) {
    setNotice((n) => ({ ...n, [id]: msg }));
  }

  async function transcribe(source: JobView) {
    try {
      await client.startTranscribe(source.id);
      note(source.id, "Transcribing — see Queue");
    } catch {
      note(source.id, "Couldn't start transcribe");
    }
  }
  async function findMoments(source: JobView) {
    try {
      await client.findMoments(source.id, { mode: "funny" });
      note(source.id, "Finding moments — see Queue");
    } catch {
      note(source.id, "Couldn't start — transcript needed");
    }
  }

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Library</h1>
        <p className="mt-1 text-sm text-text-dim">Your downloaded sources. Transcribe one, then find clip-worthy moments.</p>
      </header>

      {sources.length === 0 ? (
        <EmptyState
          title="No sources yet"
          hint="Import a video to get started."
          action={
            <Link href="/import">
              <Button>Import a video</Button>
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {sources.map((source) => {
            const tj = transcriptFor(source.id);
            const transcribing = tj?.status === "running" || tj?.status === "queued";
            const transcribed = tj?.status === "done";
            return (
              <Card key={source.id} className="flex flex-col overflow-hidden">
                <div className="aspect-video bg-bg-3">
                  {source.thumbnail ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={source.thumbnail} alt="" loading="lazy" className="h-full w-full object-cover" />
                  ) : (
                    <div className="grid h-full place-items-center text-text-faint">no thumbnail</div>
                  )}
                </div>
                <div className="flex flex-1 flex-col gap-3 p-4">
                  <div className="flex-1">
                    <p className="line-clamp-2 font-medium text-text">{source.title || source.url}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      {tj?.duration_seconds ? <Badge>{fmtDuration(tj.duration_seconds)}</Badge> : null}
                      {transcribed && <Badge tone="ok">transcribed</Badge>}
                      {transcribing && <Badge tone="info">transcribing</Badge>}
                      {!tj && <Badge tone="warn">no transcript</Badge>}
                    </div>
                  </div>
                  {notice[source.id] ? (
                    <p className="text-xs text-accent">{notice[source.id]}</p>
                  ) : transcribed ? (
                    <Button onClick={() => findMoments(source)}>Find moments</Button>
                  ) : transcribing ? (
                    <Button variant="ghost" disabled>Transcribing…</Button>
                  ) : (
                    <Button variant="ghost" onClick={() => transcribe(source)}>Transcribe</Button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
