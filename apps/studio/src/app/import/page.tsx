"use client";

import { useState } from "react";
import { SpoolApiError } from "@spool/api-client";
import { useEngine, useLive } from "@/lib/engine-context";
import { Button, Card, EmptyState, Input, cn } from "@/components/ui";
import { JobRow, type JobActions } from "@/components/queue";

/** S2 — Import / Downloader. Paste a URL → `ingest.download`; the downloads list below is
 *  the live SSE jobs snapshot (no fake progress). Wired to api_v1 (spec §6.2). */
export default function ImportPage() {
  const client = useEngine();
  const { snapshot } = useLive();
  const [url, setUrl] = useState("");
  const [format, setFormat] = useState<"video" | "audio">("video");
  const [autoTranscribe, setAutoTranscribe] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const jobs = snapshot?.jobs ?? [];

  const actions: JobActions = {
    onPause: (id) => void client.pauseJob(id).catch(() => {}),
    onResume: (id) => void client.resumeJob(id).catch(() => {}),
    onCancel: (id) => void client.cancelJob(id).catch(() => {}),
    onDismiss: (id) => void client.dismissJob(id).catch(() => {}),
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;
    setSubmitting(true);
    setError(null);
    try {
      await client.submitDownload({ url: trimmed, format, auto_transcribe: autoTranscribe });
      setUrl(""); // the new job appears via the SSE stream
    } catch (err) {
      setError(err instanceof SpoolApiError ? err.code : "unreachable");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Import</h1>
        <p className="mt-1 text-sm text-text-dim">
          Paste a video URL — any site yt-dlp supports. It downloads locally, then transcribes
          so you can find and cut clips.
        </p>
      </header>

      <Card className="p-5">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.youtube.com/watch?v=…"
            aria-label="Video URL"
            inputMode="url"
          />
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex rounded border border-line p-0.5" role="group" aria-label="Format">
              {(["video", "audio"] as const).map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFormat(f)}
                  className={cn(
                    "min-h-9 rounded-sm px-3 text-sm font-medium capitalize",
                    format === f ? "bg-accent text-accent-ink" : "text-text-dim hover:text-text",
                  )}
                >
                  {f}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-sm text-text-dim">
              <input
                type="checkbox"
                checked={autoTranscribe}
                onChange={(e) => setAutoTranscribe(e.target.checked)}
                className="h-4 w-4 accent-[var(--accent)]"
              />
              Auto-transcribe on download
            </label>
            <Button type="submit" disabled={submitting || !url.trim()} className="ml-auto">
              {submitting ? "Submitting…" : "Download"}
            </Button>
          </div>
          {error && (
            <p className="text-sm text-err">
              Couldn&rsquo;t submit (<span className="font-mono">{error}</span>). Is the engine running?
            </p>
          )}
        </form>
      </Card>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-text-dim">Downloads</h2>
        {jobs.length === 0 ? (
          <EmptyState title="No downloads yet" hint="Paste a URL above to pull your first source." />
        ) : (
          <Card>
            <ul className="divide-y divide-line">
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} actions={actions} />
              ))}
            </ul>
          </Card>
        )}
      </section>
    </div>
  );
}
