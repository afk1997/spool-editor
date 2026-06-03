"use client";

import { useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { useSpool, buildTranscript, mapCandidates } from "@/components/spool/context";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { ClipCard } from "@/components/spool/cards";
import { DiscoveryBody, TranscriptView } from "@/components/spool/work";
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

  const s = ctx.sources.find((x) => x.id === id);
  const doc = useEngineQuery((c) => (s?.transcriptId ? c.getTranscriptDoc(s.transcriptId) : Promise.resolve(undefined)), [s?.transcriptId]);
  const { lines, speakers } = buildTranscript(doc.data?.words);
  const candidates = mapCandidates(snapshot, id, doc.data?.words);
  const finding = (snapshot?.clips ?? []).some((c) => c.kind === "moments" && c.source_id === id && (c.status === "running" || c.status === "queued"));
  const myClips = ctx.clips.filter((c) => c.src === id);

  if (!s) return (
    <div className="mainpad fadein">
      <button className="btn subtle sm" style={{ marginBottom: 14, paddingLeft: 0 }} onClick={() => ctx.nav("library")}><Icon name="chevL" size={15} /> Library</button>
      <Empty icon="film" title="Source not found" action={<Btn variant="primary" icon="import" onClick={() => ctx.nav("import")}>Import a video</Btn>}>It may have been cleared from the working set — re-import to continue.</Empty>
    </div>
  );

  const transcribing = s.status === "transcribing";

  return (
    <div className="mainpad fadein">
      <button className="btn subtle sm" style={{ marginBottom: 14, paddingLeft: 0 }} onClick={() => ctx.nav("library")}><Icon name="chevL" size={15} /> Library</button>
      <div className="row" style={{ gap: 18, marginBottom: 22, alignItems: "flex-start" }}>
        <div style={{ width: 220, flex: "none", borderRadius: "var(--radius)", overflow: "hidden" }}><Thumb seed={s.id} kind={s.kind}><div className="tl"><SourceGlyph type={s.src} /></div><div className="br"><span className="badge mono">{fmtDur(s.dur)}</span></div></Thumb></div>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 9, marginBottom: 8 }}>
            {transcribing ? <Chip tone="info" dot>transcribing · {s.prog}%</Chip> : <Chip tone="ok" dot>ready</Chip>}
            <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>{s.size} · {s.kind}</span>
          </div>
          <h1 style={{ fontSize: 24, marginBottom: 8, lineHeight: 1.2 }}>{s.title}</h1>
          <div className="row" style={{ gap: 8, color: "var(--text-faint)", fontSize: 13, marginBottom: 16 }}><SourceGlyph type={s.src} /> {s.channel} · {s.lang} · added {s.added}</div>
          <div className="row" style={{ gap: 9, flexWrap: "wrap" }}>
            <Btn variant="primary" icon="scissors" onClick={() => ctx.nav("discovery", { id: s.id })}>Find clips</Btn>
            <Btn variant="ghost" icon="bolt" onClick={() => ctx.askAgent("Make 3 funny shorts from this source", s.id)}>Make with recipe ▾</Btn>
            <Btn variant="ghost" icon="refresh" onClick={() => { ctx.client.startTranscribe(s.id).catch(() => {}); ctx.pushToast({ icon: "type", tone: "info", title: "Re-transcribing", body: s.title }); }}>Re-transcribe</Btn>
          </div>
        </div>
      </div>

      <div className="tabs" style={{ marginBottom: 22 }}>
        {["Overview", "Transcript", "Candidates", "Clips"].map((t) => (
          <div key={t} className={"tab" + (tab === t ? " on" : "")} onClick={() => setTab(t)}>{t}{t === "Candidates" && candidates.length > 0 && <span className="chip acc" style={{ marginLeft: 7, height: 18, padding: "0 6px" }}>{candidates.length}</span>}{t === "Clips" && myClips.length > 0 && <span className="chip" style={{ marginLeft: 7, height: 18, padding: "0 6px" }}>{myClips.length}</span>}</div>
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
                <div className="row" style={{ marginBottom: 10 }}><div className="eyebrow">Audio energy</div><span className="spacer" /><span className="chip warn">Phase 3</span></div>
                <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>Audio-peak analysis — loud / high-energy moments feeding the glass-box ranking — arrives in Phase 3. Today, candidates come from the transcript via find-moments.</div>
              </div>
            </div>
            <div className="card" style={{ padding: 18 }}>
              <div className="eyebrow" style={{ marginBottom: 16 }}>At a glance</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
                <Stat v={fmtDur(s.dur)} l="Length" />
                <Stat v={s.lang} l="Language" />
                <Stat v={candidates.length} l="Candidates" />
                <Stat v={myClips.length} l="Clips made" />
                <Stat v={s.speakerCount || 1} l="Speakers" />
                <Stat v={s.size} l="Size" />
              </div>
              <div className="divider" style={{ margin: "18px 0" }} />
              <Btn variant="primary" icon="scissors" onClick={() => ctx.nav("discovery", { id: s.id })} style={{ width: "100%" }}>Find clips</Btn>
            </div>
          </div>
        )
      )}
      {tab === "Transcript" && <TranscriptView lines={lines} speakers={speakers} tid={s.transcriptId} sourceId={s.id} onEdited={doc.reload} />}
      {tab === "Candidates" && (candidates.length === 0 && !finding
        ? <Empty icon="scan" title="No candidates yet" action={<Btn variant="primary" icon="scissors" onClick={() => ctx.nav("discovery", { id: s.id })}>Find clips</Btn>}>Run discovery to scan the transcript for clip-worthy moments.</Empty>
        : <DiscoveryBody key={candidates[0]?.id.split("-")[0] ?? "none"} candidates={candidates} sourceId={s.id} finding={finding} />)}
      {tab === "Clips" && (myClips.length === 0
        ? <Empty icon="film" title="No clips from this source yet" action={<Btn variant="primary" icon="scissors" onClick={() => ctx.nav("discovery", { id: s.id })}>Make clips</Btn>} />
        : <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(180px,1fr))", gap: 16 }}>{myClips.map((c) => <ClipCard key={c.id} c={c} />)}</div>)}
    </div>
  );
}
