"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { useSpool, buildTranscript, mapCandidates } from "@/components/spool/context";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { describeActionError } from "@/lib/action-error";
import { ClipCard } from "@/components/spool/cards";
import { DiscoveryBody, TranscriptView } from "@/components/spool/work";
import { EnergyWave } from "@/components/spool/energy";
import { Btn, Chip, Empty, Icon, Progress, SourceGlyph, Stat, Thumb, fmtDur } from "@spool/ui";

/* S4 Project detail — 1:1 port of the demo (04). Source + transcript + candidates are all
 * live: the transcript view reads words.json; the Candidates tab embeds the real Discovery
 * body; the at-a-glance stats are all real source fields (no fabricated scene-cut/fps). */
export default function ProjectScreen() {
  const ctx = useSpool();
  const { snapshot } = useLive();
  const id = String(useParams().id);
  // Seed the tab from ?tab= so "Make clips" can land you straight on the Clips tab to review.
  const [tab, setTab] = useState(useSearchParams().get("tab") || "Overview");
  const [retranscribing, setRetranscribing] = useState(false);
  const [pendingRetranscribeId, setPendingRetranscribeId] = useState<string | null>(null);
  const retranscribingRef = useRef<"idle" | "submitting" | "waiting">("idle");
  const [actionError, setActionError] = useState<ReturnType<typeof describeActionError> | null>(null);

  const s = ctx.sources.find((x) => x.id === id);
  const doc = useEngineQuery((c) => (s?.transcriptId ? c.getTranscriptDoc(s.transcriptId) : Promise.resolve(undefined)), [s?.transcriptId]);
  const { lines, speakers } = buildTranscript(doc.data?.words);
  const candidates = mapCandidates(snapshot, id, doc.data?.words);
  // Real audio-energy waveform (loudness envelope) for the source — only once it's downloaded.
  const energyQ = useEngineQuery((c) => (s && s.status !== "transcribing" ? c.sourceEnergy(id, 96) : Promise.resolve({ bars: [], buckets: 0 })), [id, s?.status]);
  const finding = (snapshot?.clips ?? []).some((c) => c.kind === "moments" && c.source_id === id && (c.status === "running" || c.status === "queued"));
  const myClips = ctx.clips.filter((c) => c.src === id);
  const pendingRetranscribe = pendingRetranscribeId
    ? snapshot?.transcripts.find((job) => job.id === pendingRetranscribeId)
    : undefined;
  const pendingRetranscribeTerminal = !!pendingRetranscribe
    && pendingRetranscribe.status !== "queued"
    && pendingRetranscribe.status !== "running";
  const pendingRetranscribeActive = pendingRetranscribeId !== null && !pendingRetranscribeTerminal;
  const pendingRetranscribeError = pendingRetranscribe?.status === "error"
    ? {
        code: pendingRetranscribe.error_category || "transcribe_failed",
        message: pendingRetranscribe.error_message || "The transcription job failed.",
      }
    : pendingRetranscribe?.status === "cancelled"
      ? {
          code: pendingRetranscribe.error_category || "cancelled",
          message: pendingRetranscribe.error_message || "The transcription job was cancelled.",
        }
      : null;
  const visibleActionError = actionError ?? pendingRetranscribeError;

  useEffect(() => {
    if (pendingRetranscribeTerminal) retranscribingRef.current = "idle";
  }, [pendingRetranscribeTerminal]);

  const retranscribe = async () => {
    if (!s || retranscribingRef.current !== "idle") return;
    retranscribingRef.current = "submitting";
    setPendingRetranscribeId(null);
    setRetranscribing(true); setActionError(null);
    try {
      const accepted = await ctx.client.startTranscribe(s.id);
      retranscribingRef.current = "waiting";
      setPendingRetranscribeId(accepted.id);
      ctx.pushToast({ icon: "type", tone: "info", title: "Transcription queued", body: s.title });
    } catch (error) {
      retranscribingRef.current = "idle";
      setActionError(describeActionError(error));
    } finally {
      setRetranscribing(false);
    }
  };

  if (!s) {
    if (!snapshot) return <div className="mainpad fadein" style={{ color: "var(--text-faint)" }}>Loading source…</div>;
    return (
      <div className="mainpad fadein">
        <button className="btn subtle sm" style={{ marginBottom: 14, paddingLeft: 0 }} onClick={() => ctx.nav("library")}><Icon name="chevL" size={15} /> Library</button>
        <Empty icon="film" title="Source unavailable" action={<Btn variant="primary" icon="import" onClick={() => ctx.nav("import")}>Import a video</Btn>}>This source ID is unavailable, or its import has not completed.</Empty>
      </div>
    );
  }

  const transcribing = s.status === "transcribing";
  const retranscribeBusy = retranscribing || pendingRetranscribeActive || transcribing;
  const retranscribeLabel = retranscribing
    ? "Starting…"
    : pendingRetranscribeActive
      ? pendingRetranscribe?.status === "running" ? "Transcribing…" : "Queued…"
      : transcribing ? "Transcribing…" : "Re-transcribe";
  const duration = s.dur > 0 ? fmtDur(s.dur) : "—";
  const knownOrigin = s.src !== "—";

  return (
    <div className="mainpad fadein">
      <button className="btn subtle sm" style={{ marginBottom: 14, paddingLeft: 0 }} onClick={() => ctx.nav("library")}><Icon name="chevL" size={15} /> Library</button>
      <div className="row" style={{ gap: 18, marginBottom: 22, alignItems: "flex-start" }}>
        <div style={{ width: 220, flex: "none", borderRadius: "var(--radius)", overflow: "hidden" }}><Thumb seed={s.id} kind={s.kind}>{knownOrigin && <div className="tl"><SourceGlyph type={s.src} /></div>}<div className="br"><span className="badge mono">{duration}</span></div></Thumb></div>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 9, marginBottom: 8 }}>
            {transcribing ? <Chip tone="info" dot>transcribing · {s.prog == null ? "—" : `${Math.round(s.prog)}%`}</Chip> : s.transcriptId ? <Chip tone="ok" dot>transcript ready</Chip> : <Chip dot>source available</Chip>}
            <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>{s.size} · {s.kind}</span>
          </div>
          <h1 style={{ fontSize: 24, marginBottom: 8, lineHeight: 1.2 }}>{s.title}</h1>
          <div className="row" style={{ gap: 8, color: "var(--text-faint)", fontSize: 13, marginBottom: 16 }}>{knownOrigin && <SourceGlyph type={s.src} />} {s.channel} · {s.lang} · added {s.added}</div>
          <div className="row" style={{ gap: 9, flexWrap: "wrap" }}>
            <Btn variant="primary" icon="scissors" onClick={() => setTab("Transcript")}>Cut from transcript</Btn>
            <Btn variant="ghost" icon="refresh" disabled={retranscribeBusy} onClick={retranscribe}>{retranscribeLabel}</Btn>
          </div>
          {visibleActionError && <div role="alert" className="card" style={{ marginTop: 12, padding: 10, color: "var(--err)", borderColor: "rgba(190,81,73,0.4)", background: "var(--err-soft)", fontSize: 12.5 }}><span className="mono">{visibleActionError.code}</span> · {visibleActionError.message}</div>}
        </div>
      </div>

      <div className="tabs" role="tablist" aria-label="Source sections" style={{ marginBottom: 22 }}>
        {["Overview", "Transcript", "Candidates", "Clips"].map((t) => (
          <button type="button" role="tab" aria-selected={tab === t} key={t} className={"tab" + (tab === t ? " on" : "")} style={{ fontFamily: "inherit", background: "transparent", borderTop: 0, borderLeft: 0, borderRight: 0 }} onClick={() => setTab(t)}>{t}{t === "Candidates" && candidates.length > 0 && <span className="chip acc" style={{ marginLeft: 7, height: 18, padding: "0 6px" }}>{candidates.length}</span>}{t === "Clips" && myClips.length > 0 && <span className="chip" style={{ marginLeft: 7, height: 18, padding: "0 6px" }}>{myClips.length}</span>}</button>
        ))}
      </div>

      {tab === "Overview" && (
        transcribing ? (
          <div className="card" style={{ padding: 18, marginBottom: 20 }}>
            <div className="row" style={{ gap: 10, marginBottom: 12 }}><Icon name="type" size={16} style={{ color: "var(--info)" }} /><b>Transcribing…</b><span className="spacer" /><span className="mono" style={{ color: "var(--text-dim)" }}>{s.prog}%</span></div>
            <Progress value={s.prog ?? 0} tone="info" striped />
            {/* whisper.cpp writes words.json once it finishes (no streaming partials), so the
                word-level transcript lands in the Transcript tab on completion — say that
                honestly rather than promise a stream that never arrives. */}
            <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 10 }}>whisper · on-device · the word-level transcript opens in the Transcript tab when it finishes</div>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 300px", gap: 24 }}>
            <div>
              <div className="card" style={{ overflow: "hidden", marginBottom: 20, background: "#000", aspectRatio: "16/9" }}>
                {/* the real downloaded source streamed from /jobs/<id>/file */}
                <video src={ctx.client.jobFileUrl(s.id)} controls playsInline preload="metadata" style={{ width: "100%", height: "100%", objectFit: "contain", background: "#000" }} />
              </div>
              <div className="card" style={{ padding: 16 }}>
                <div className="row" style={{ marginBottom: 12 }}><div className="eyebrow">Audio energy</div></div>
                {energyQ.loading ? (
                  <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>analyzing loudness…</div>
                ) : energyQ.data?.bars?.length ? (
                  <>
                    <EnergyWave bars={energyQ.data.bars} height={68} groups={4} color="#7c89a8" />
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 12 }}>Relative audio amplitude across the source.</div>
                  </>
                ) : (
                  <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)" }}>Audio energy unavailable.</div>
                )}
              </div>
            </div>
            <div className="card" style={{ padding: 18 }}>
              <div className="eyebrow" style={{ marginBottom: 16 }}>At a glance</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
                <Stat v={duration} l="Length" />
                <Stat v={s.lang} l="Language" />
                <Stat v={candidates.length} l="Candidates" />
                <Stat v={myClips.length} l="Clips made" />
                {(s.speakerCount ?? 0) > 0 && <Stat v={s.speakerCount!} l="Speakers" />}
                <Stat v={s.size} l="Size" />
              </div>
              <div className="divider" style={{ margin: "18px 0" }} />
              <Btn variant="primary" icon="scissors" onClick={() => setTab("Transcript")} style={{ width: "100%" }}>Cut from transcript</Btn>
            </div>
          </div>
        )
      )}
      {tab === "Transcript" && <TranscriptView lines={lines} speakers={speakers} tid={s.transcriptId} sourceId={s.id} onEdited={doc.reload} />}
      {tab === "Candidates" && (candidates.length === 0 && !finding
        ? <Empty icon="scan" title="Remote discovery unavailable" action={<Btn variant="primary" icon="scissors" onClick={() => setTab("Transcript")}>Cut from transcript</Btn>}>Select words in the Transcript tab, then cut a clip from that selection.</Empty>
        : <DiscoveryBody key={candidates[0]?.id.split("-")[0] ?? "none"} candidates={candidates} sourceId={s.id} finding={finding} />)}
      {tab === "Clips" && (myClips.length === 0
        ? <Empty icon="film" title="No clips from this source yet" action={<Btn variant="primary" icon="scissors" onClick={() => setTab("Transcript")}>Cut from transcript</Btn>} />
        : <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(180px,1fr))", gap: 16 }}>{myClips.map((c) => <ClipCard key={c.id} c={c} />)}</div>)}
    </div>
  );
}
