"use client";

import { useRef, useState } from "react";
import { useSpool } from "@/components/spool/context";
import { describeActionError } from "@/lib/action-error";
import { MediaCard } from "@/components/spool/cards";
import { Btn, Chip, Icon, Seg, SourceGlyph, fmtDur } from "@spool/ui";

/* LibraryScreen — sources are live-mapped; batch transcription and opted-in discovery
 * use the same engine job queue as the source detail screen. */
export default function LibraryScreen() {
  const ctx = useSpool();
  const [view, setView] = useState("grid");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const [sel, setSel] = useState<string[]>([]);
  const [batching, setBatching] = useState(false);
  const batchingRef = useRef(false);
  const list = ctx.sources.filter((s) => (status === "all" || s.status === status) && s.title.toLowerCase().includes(q.toLowerCase()));
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const runBatch = async (
    label: string,
    icon: string,
    action: (id: string) => Promise<unknown>,
  ) => {
    const ids = [...sel];
    if (!ids.length || batchingRef.current) return;
    batchingRef.current = true;
    setBatching(true);
    try {
      const results = await Promise.allSettled(ids.map(action));
      const succeededIds = ids.filter((_, i) => results[i]?.status === "fulfilled");
      const failedResults = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
      const firstFailure = failedResults[0] ? describeActionError(failedResults[0].reason) : null;
      setSel((current) => current.filter((id) => !succeededIds.includes(id)));
      ctx.pushToast({
        icon: failedResults.length ? "alert" : icon,
        tone: failedResults.length ? "warn" : "info",
        title: label,
        body: `${succeededIds.length} succeeded · ${failedResults.length} failed${firstFailure ? ` · ${firstFailure.code}: ${firstFailure.message}` : ""}`,
      });
    } finally {
      batchingRef.current = false;
      setBatching(false);
    }
  };

  const batchTranscribe = async () => {
    await runBatch("Transcribe requests settled", "type", (id) => ctx.client.startTranscribe(id));
  };
  const discoveryReady = ctx.settingsReady
    && ctx.reasoningProvider === "codex"
    && ctx.reasoningEgressConsent
    && !ctx.offline;
  const batchFindClips = async () => {
    await runBatch("Moment scans requested", "scissors", (id) => ctx.client.findMoments(id, { mode: "funny" }));
  };
  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 20, gap: 14 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Library</div><h1 style={{ fontSize: 30 }}>Sources</h1></div>
        <div className="spacer" />
        <Btn variant="primary" icon="import" onClick={() => ctx.nav("import")}>Import</Btn>
      </div>

      <div className="row" style={{ gap: 10, marginBottom: 20, flexWrap: "wrap" }}>
        <div className="cmdk" style={{ maxWidth: 300, flex: "0 1 300px" }}>
          <Icon name="search" size={15} />
          <input style={{ background: "transparent", border: 0, outline: "none", color: "var(--text)", flex: 1, fontFamily: "inherit" }} placeholder="Search title or transcript…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <Seg value={status} onChange={setStatus} neutral options={[{ value: "all", label: "All" }, { value: "ready", label: "Ready" }, { value: "transcribing", label: "Processing" }]} />
        <div className="spacer" />
        {sel.length > 0 && (
          <div className="row" style={{ gap: 8 }}>
            <span className="chip acc">{sel.length} selected</span>
            <Btn variant="ghost" size="sm" icon="type" onClick={batchTranscribe} disabled={batching}>Transcribe</Btn>
            <Btn variant="ghost" size="sm" icon="scissors" onClick={batchFindClips} disabled={batching || !discoveryReady} title={discoveryReady ? "Ask Codex for moment suggestions" : "Enable Codex moment suggestions in Settings → Privacy"}>Find clips</Btn>
            {!discoveryReady && <span className="mono" style={{ color: "var(--warn)", fontSize: 10.5 }}>Enable Codex suggestions in Settings.</span>}
          </div>
        )}
        <Seg value={view} onChange={setView} neutral options={[{ value: "grid", icon: "grid", label: "", ariaLabel: "Grid view" }, { value: "table", icon: "list", label: "", ariaLabel: "Table view" }]} />
      </div>

      {list.length === 0 && <div style={{ color: "var(--text-faint)", fontSize: 13, padding: "30px 0" }}>No sources yet — import a video to begin.</div>}

      {view === "grid" ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(255px,1fr))", gap: 16 }}>
          {list.map((s) => (
            // Native off-screen render-skip for a large library (zero layout change);
            // contain-intrinsic-size reserves the card's space so scroll height is stable.
            <div key={s.id} style={{ position: "relative", contentVisibility: "auto", containIntrinsicSize: "auto 230px" }}>
              <button type="button" aria-label={`Select ${s.title}`} aria-pressed={sel.includes(s.id)} onClick={(e) => { e.stopPropagation(); toggle(s.id); }} className="checkbox" style={{ position: "absolute", top: 10, left: 10, zIndex: 5, background: sel.includes(s.id) ? "var(--accent)" : "rgba(0,0,0,0.5)", borderColor: sel.includes(s.id) ? "transparent" : "var(--line-str)", color: "var(--accent-ink)", cursor: "pointer" }}>{sel.includes(s.id) && <Icon name="check" size={12} />}</button>
              <MediaCard s={s} onOpen={() => ctx.nav("project", { id: s.id })} />
            </div>
          ))}
        </div>
      ) : (
        <div className="panel" style={{ overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr style={{ color: "var(--text-faint)", fontSize: 11.5, textAlign: "left" }}>
              {["Title", "Status", "Clips", "Duration"].map((h) => <th key={h} style={{ padding: "11px 14px", fontWeight: 600, borderBottom: "1px solid var(--line)" }}>{h}</th>)}
            </tr></thead>
            <tbody>
              {list.map((s) => (
                <tr key={s.id} style={{ cursor: "pointer", borderBottom: "1px solid var(--line-2)" }} onClick={() => ctx.nav("project", { id: s.id })}>
                  <td style={{ padding: "10px 14px", fontWeight: 600, maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}><span className="row" style={{ gap: 9 }}>{s.src !== "—" && <SourceGlyph type={s.src} />}{s.title}</span></td>
                  <td style={{ padding: "10px 14px" }}><Chip tone={s.status === "ready" ? "ok" : s.status === "transcribing" ? "info" : "warn"} dot>{s.status}</Chip></td>
                  <td style={{ padding: "10px 14px" }} className="mono">{s.clips}</td>
                  <td style={{ padding: "10px 14px" }} className="mono">{s.dur > 0 ? fmtDur(s.dur) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
