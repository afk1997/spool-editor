"use client";

import { Suspense, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useSpool } from "@/components/spool/context";
import { describeActionError } from "@/lib/action-error";
import { Btn, Chip, Icon, Progress, Seg, Switch, Thumb } from "@spool/ui";

type VisibleError = { code: string; message: string };
type BatchError = VisibleError & { url: string };
type DownloadAction = "pause" | "resume";
type PendingDownloadAction = { action: DownloadAction; acknowledged: boolean };

const downloadActionReflected = (action: DownloadAction, status: string) =>
  action === "pause"
    ? status === "paused" || status === "done" || status === "error" || status === "cancelled"
    : status !== "paused";

/* ImportScreen — 1:1 port of the demo (03), wired: Resolve submits real downloads via
 * ingest.download; the Downloads list is the live jobs snapshot. A `?url=` query (e.g. from
 * Home's "Import / Paste URL") pre-fills the box. */
function ImportScreen() {
  const ctx = useSpool();
  const params = useSearchParams();
  const [url, setUrl] = useState(() => params.get("url") ?? "");
  const [format, setFormat] = useState("Video");
  const [opts, setOpts] = useState({ subs: true, chapters: true, meta: false });
  const [legal, setLegal] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [submitError, setSubmitError] = useState<{ summary: string; failures: BatchError[] } | null>(null);
  const [pauseErrors, setPauseErrors] = useState<Record<string, VisibleError>>({});
  const pendingDownloadActionsRef = useRef<Record<string, PendingDownloadAction>>({});
  const [pendingDownloadActions, setPendingDownloadActions] = useState<Record<string, PendingDownloadAction>>({});
  const downloads = ctx.downloads;

  const resolve = async () => {
    if (submittingRef.current) return;
    const urls = url.split(/\s+/).filter(Boolean);
    if (!urls.length) {
      setSubmitError({
        summary: "The URL batch was not submitted.",
        failures: [{ url: "", code: "invalid_url", message: "Invalid URL input. Enter at least one HTTP or HTTPS URL." }],
      });
      return;
    }

    const invalid = urls.find((value) => {
      try {
        const parsed = new URL(value);
        return parsed.protocol !== "http:" && parsed.protocol !== "https:";
      } catch {
        return true;
      }
    });
    if (invalid) {
      setSubmitError({
        summary: "The URL batch was not submitted.",
        failures: [{
          url: invalid,
          code: "invalid_url",
          message: `Invalid URL "${invalid}". Enter complete HTTP or HTTPS URLs separated by whitespace.`,
        }],
      });
      return;
    }

    submittingRef.current = true;
    setSubmitError(null);
    setSubmitting(true);
    try {
      const results = await Promise.allSettled(
        urls.map((value) => ctx.client.submitDownload({
          url: value,
          format: format === "Audio" ? "audio" : "video",
          auto_transcribe: true,
          subtitles: opts.subs,
          chapters: opts.chapters,
          embed: opts.meta,
        })),
      );
      const failures = results.flatMap<BatchError>((result, index) => {
        if (result.status === "fulfilled") return [];
        return [{ ...describeActionError(result.reason), url: urls[index]! }];
      });
      const succeeded = results.length - failures.length;

      if (failures.length) {
        setUrl(failures.map((failure) => failure.url).join("\n"));
        setSubmitError({
          summary: `${succeeded} succeeded, ${failures.length} failed. Only the failed URLs remain so you can retry them without duplicating successful imports.`,
          failures,
        });
        return;
      }

      setUrl("");
      ctx.pushToast({
        icon: "download",
        tone: "info",
        title: `${succeeded} succeeded, 0 failed`,
        body: "Every download was accepted. Progress appears below and in the queue.",
      });
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const runDownloadAction = async (id: string, action: DownloadAction) => {
    const existing = pendingDownloadActionsRef.current[id];
    if (existing) {
      const currentDownload = downloads.find((download) => download.id === id);
      if (!existing.acknowledged || !currentDownload
        || !downloadActionReflected(existing.action, currentDownload.status)) return;
      delete pendingDownloadActionsRef.current[id];
    }
    const pending = { action, acknowledged: false };
    pendingDownloadActionsRef.current[id] = pending;
    setPendingDownloadActions((current) => ({ ...current, [id]: pending }));
    setPauseErrors((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    try {
      if (action === "pause") await ctx.client.pauseJob(id);
      else await ctx.client.resumeJob(id);
      const current = pendingDownloadActionsRef.current[id];
      if (current?.action === action) {
        const acknowledged = { action, acknowledged: true };
        pendingDownloadActionsRef.current[id] = acknowledged;
        setPendingDownloadActions((entries) => ({ ...entries, [id]: acknowledged }));
      }
    } catch (error) {
      delete pendingDownloadActionsRef.current[id];
      setPendingDownloadActions((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      setPauseErrors((current) => ({ ...current, [id]: describeActionError(error) }));
    }
  };

  return (
    <div className="mainpad fadein">
      <div className="eyebrow" style={{ marginBottom: 6 }}>Import</div>
      <h1 style={{ fontSize: 30, marginBottom: 4 }}>Import a source</h1>
      <p style={{ color: "var(--text-faint)", marginTop: 0, marginBottom: 22 }}>Paste one or more HTTP or HTTPS media URLs. Downloads run through yt-dlp on this machine.</p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 24, alignItems: "start" }}>
        <div className="panel" style={{ overflow: "hidden" }}>
          <div className="tabs" style={{ padding: "0 16px" }}>
            <div className="tab on">Paste URL</div>
          </div>
          <div style={{ padding: 20 }}>
            <div>
              <textarea
                className="input"
                rows={3}
                placeholder={"https://youtube.com/watch?v=…\nPaste multiple URLs separated by whitespace"}
                value={url}
                disabled={submitting}
                onChange={(e) => {
                  setUrl(e.target.value);
                  setSubmitError(null);
                }}
                aria-describedby={submitError ? "import-error" : undefined}
              />
              {submitError && (
                <div id="import-error" role="alert" style={{ marginTop: 10, padding: 12, borderRadius: "var(--radius-sm)", background: "var(--err-soft)", color: "var(--err)", fontSize: 12.5 }}>
                  <b>{submitError.summary}</b>
                  {submitError.failures.map((failure, index) => (
                    <div key={`${failure.url}-${index}`} style={{ marginTop: 5 }}>
                      <span className="mono" style={{ fontWeight: 700 }}>{failure.code}</span>
                      {failure.url && <span> · {failure.url}</span>}
                      <div style={{ color: "var(--text-dim)", marginTop: 2 }}>{failure.message}</div>
                    </div>
                  ))}
                </div>
              )}
              <div className="row" style={{ gap: 14, marginTop: 14, flexWrap: "wrap" }}>
                <div><span className="field-label">Format</span><Seg value={format} onChange={setFormat} neutral options={["Video", "Audio"]} /></div>
                <div className="spacer" />
                <Btn variant="primary" icon="download" onClick={resolve} disabled={submitting} style={{ alignSelf: "flex-end" }}>
                  {submitting ? "Submitting…" : "Download"}
                </Btn>
              </div>
              <div className="divider" style={{ margin: "18px 0" }} />
              <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
                {([["subs", "Download subtitles if available"], ["chapters", "Keep chapters as clip boundaries"], ["meta", "Embed metadata & thumbnail"]] as const).map(([k, l]) => (
                  <label key={k} className="row" style={{ gap: 9, cursor: "pointer", fontSize: 13 }}>
                    <Switch label={l} on={opts[k]} onClick={() => setOpts((o) => ({ ...o, [k]: !o[k] }))} /> {l}
                  </label>
                ))}
              </div>
            </div>
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
              const cancelled = d.status === "cancelled";
              const terminalFailure = err || cancelled;
              const pendingEntry = pendingDownloadActions[d.id];
              const pendingAction = pendingEntry
                && !(pendingEntry.acknowledged && downloadActionReflected(pendingEntry.action, d.status))
                ? pendingEntry.action
                : undefined;
              return (
                <div key={d.id} className="card" style={{ display: "flex", gap: 14, padding: 12, alignItems: "center", borderColor: terminalFailure ? "rgba(190,81,73,0.4)" : "var(--line)", background: terminalFailure ? "var(--err-soft)" : "var(--bg-2)" }}>
                  <div style={{ width: 96, flex: "none", borderRadius: 8, overflow: "hidden" }}><Thumb seed={d.id} kind={d.src} label={false} /></div>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="row" style={{ marginBottom: 7, gap: 8 }}>
                      <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.title}</span>
                      <span className="spacer" />
                      {d.status === "done" ? <Chip tone="ok" dot>done</Chip>
                        : err ? <Chip tone="err" dot>failed</Chip>
                        : cancelled ? <Chip tone="err" dot>cancelled</Chip>
                        : d.status === "paused" ? <Chip tone="warn" dot>paused</Chip>
                        : d.status === "queued" ? <Chip dot>queued</Chip>
                        : <span className="mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>{Math.round(d.prog)}% · {d.size} · {d.speed} · ETA {d.eta}</span>}
                    </div>
                    {terminalFailure ? <div className="row" style={{ gap: 7, fontSize: 12, color: "var(--err)" }}><Icon name="alert" size={13} />{d.err || (cancelled ? "Download was cancelled." : "Download failed.")}</div>
                      : <Progress value={d.prog} tone={d.status === "done" ? "ok" : "info"} striped={d.status === "downloading"} />}
                    {pauseErrors[d.id] && (
                      <div role="alert" style={{ marginTop: 7, fontSize: 12, color: "var(--err)" }}>
                        <span className="mono" style={{ fontWeight: 700 }}>{pauseErrors[d.id]!.code}</span> · {pauseErrors[d.id]!.message}
                      </div>
                    )}
                  </div>
                  {d.status === "done"
                    ? <div className="row" style={{ gap: 8 }}><Btn variant="ghost" size="sm" onClick={() => ctx.nav("project", { id: d.id })}>Open</Btn></div>
                    : terminalFailure || d.status === "queued" ? null
                    : pendingAction
                      ? <button
                          className="iconbtn"
                          aria-label={`${pendingAction === "pause" ? "Pausing" : "Resuming"} download…`}
                          aria-busy="true"
                          title={`${pendingAction === "pause" ? "Pausing" : "Resuming"} download…`}
                          disabled
                        ><Icon name={pendingAction === "pause" ? "pause" : "play"} size={16} /></button>
                    : d.status === "paused"
                      ? <button className="iconbtn" aria-label="Resume download" onClick={() => void runDownloadAction(d.id, "resume")}><Icon name="play" size={16} /></button>
                      : <button className="iconbtn" aria-label="Pause download" onClick={() => void runDownloadAction(d.id, "pause")}><Icon name="pause" size={16} /></button>}
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
