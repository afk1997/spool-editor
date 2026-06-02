"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { MomentCandidate } from "@spool/types";
import { useEngine, useEngineQuery, useLive } from "@/lib/engine-context";
import { Badge, Button, Card, EmptyState, Spinner, cn, fmtDuration } from "@/components/ui";
import { CandidateCard } from "@/components/candidate-card";

const MODES = ["funny", "insightful", "hot-take", "story", "how-to", "q&a"] as const;

/** S4 Project / Transcript + S5 Clip Discovery for one source. Transcript is read-only in
 *  Phase 1 (the editable transcript is S6, Phase 2). Everything wired to api_v1. */
export default function SourcePage() {
  const { id: sourceId } = useParams<{ id: string }>();
  const client = useEngine();
  const { snapshot } = useLive();

  const source = (snapshot?.jobs ?? []).find((j) => j.id === sourceId);
  const transcript = (snapshot?.transcripts ?? [])
    .filter((t) => t.parent_job_id === sourceId)
    .sort((a, b) => b.elapsed_seconds - a.elapsed_seconds)[0];
  const tid = transcript?.status === "done" ? transcript.id : undefined;

  // The latest find_moments job for this source (insertion order ⇒ last match is newest).
  const momentsJob = (snapshot?.clips ?? [])
    .filter((c) => c.kind === "moments" && c.source_id === sourceId)
    .at(-1);

  const [mode, setMode] = useState<string>("funny");
  const [notice, setNotice] = useState<Record<string, string>>({});
  const [finding, setFinding] = useState(false);

  async function findMoments() {
    setFinding(true);
    try {
      await client.findMoments(sourceId, { mode, count: 8 });
    } catch {
      /* surfaced via the job's error_message on the stream */
    } finally {
      setFinding(false);
    }
  }
  async function cut(c: MomentCandidate) {
    setNotice((n) => ({ ...n, [key(c)]: "Cutting — see Queue" }));
    await client.cut(sourceId, { start: c.start, end: c.end }).catch(() =>
      setNotice((n) => ({ ...n, [key(c)]: "Couldn't cut" })),
    );
  }
  async function quickRender(c: MomentCandidate) {
    setNotice((n) => ({ ...n, [key(c)]: "Rendering 9:16 — see Queue" }));
    await client
      .renderPipeline(sourceId, { start: c.start, end: c.end })
      .catch(() => setNotice((n) => ({ ...n, [key(c)]: "Couldn't render" })));
  }

  const candidates = (momentsJob?.result.candidates ?? []) as MomentCandidate[];
  const momentsRunning = momentsJob?.status === "running" || momentsJob?.status === "queued";

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <div>
        <Link href="/library" className="text-sm text-text-dim hover:text-accent">
          ← Library
        </Link>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight">
          {source?.title || source?.url || sourceId}
        </h1>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {transcript?.duration_seconds ? <Badge>{fmtDuration(transcript.duration_seconds)}</Badge> : null}
          {tid ? <Badge tone="ok">transcribed</Badge> : <Badge tone="warn">no transcript</Badge>}
          {transcript?.speaker_count ? <Badge tone="info">{transcript.speaker_count} speakers</Badge> : null}
        </div>
      </div>

      {!source && (
        <EmptyState title="Source not found" hint="It may have been dismissed. Check your Library." />
      )}

      {/* discovery (S5) */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-text-dim">Find moments</h2>
        {!tid ? (
          <EmptyState
            title="Transcribe this source first"
            hint="Moment-finding reads the transcript. Start a transcribe from the Library."
          />
        ) : (
          <Card className="flex flex-col gap-4 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex flex-wrap gap-1 rounded border border-line p-0.5" role="group" aria-label="Mode">
                {MODES.map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setMode(m)}
                    className={cn(
                      "min-h-9 rounded-sm px-2.5 text-sm font-medium capitalize",
                      mode === m ? "bg-accent text-accent-ink" : "text-text-dim hover:text-text",
                    )}
                  >
                    {m}
                  </button>
                ))}
              </div>
              <Button className="ml-auto" disabled={finding || momentsRunning} onClick={findMoments}>
                {finding || momentsRunning ? "Finding…" : "Find moments"}
              </Button>
            </div>
            {momentsRunning && <Spinner label={`Scanning transcript (${momentsJob?.stage || "moments"})…`} />}
            {momentsJob?.status === "error" && (
              <p className="text-sm text-err">Moment-finding failed: {momentsJob.error_message}</p>
            )}
          </Card>
        )}

        {candidates.length > 0 && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {candidates.map((c) => (
              <CandidateCard key={key(c)} candidate={c} onCut={cut} onRender={quickRender} busy={notice[key(c)]} />
            ))}
          </div>
        )}
      </section>

      {/* transcript (S4, read-only) */}
      {tid && <TranscriptView tid={tid} />}
    </div>
  );
}

function TranscriptView({ tid }: { tid: string }) {
  const doc = useEngineQuery((c) => c.getTranscriptDoc(tid), [tid]);
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-text-dim">Transcript</h2>
      <Card className="max-h-[28rem] overflow-y-auto p-4">
        {doc.loading ? (
          <Spinner label="Loading transcript…" />
        ) : doc.error ? (
          <p className="text-sm text-err">Couldn&rsquo;t load transcript ({doc.error})</p>
        ) : (
          <ol className="flex flex-col gap-3">
            {(doc.data?.segments ?? []).map((seg, i) => (
              <li key={i} className="flex gap-3">
                <span className="w-14 shrink-0 pt-0.5 font-mono text-xs text-text-faint tabular-nums">
                  {fmtDuration(seg.start)}
                </span>
                <p className="text-sm text-text">
                  {seg.speaker && <span className="mr-1.5 font-medium text-accent">{seg.speaker}</span>}
                  {seg.text}
                </p>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </section>
  );
}

function key(c: MomentCandidate): string {
  return `${c.start}-${c.end}`;
}
