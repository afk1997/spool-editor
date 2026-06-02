"use client";

import { useParams } from "next/navigation";
import { useSpool, mapCandidates } from "@/components/spool/context";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { DiscoveryBody } from "@/components/spool/work";
import { Btn, Icon } from "@/components/spool/ui";

/* S5 Clip Discovery — 1:1 port of the demo (04). "Proposed moments": real candidates from
 * the source's find_moments result (glass-box = named signals + transcript excerpt). */
export default function DiscoveryScreen() {
  const ctx = useSpool();
  const { snapshot } = useLive();
  const id = String(useParams().id);
  const s = ctx.sources.find((x) => x.id === id);
  const doc = useEngineQuery((c) => (s?.transcriptId ? c.getTranscriptDoc(s.transcriptId) : Promise.resolve(undefined)), [s?.transcriptId]);
  const candidates = mapCandidates(snapshot, id, doc.data?.words);
  const finding = (snapshot?.clips ?? []).some((c) => c.kind === "moments" && c.source_id === id && (c.status === "running" || c.status === "queued"));

  return (
    <div className="mainpad fadein">
      <button className="btn subtle sm" style={{ marginBottom: 12, paddingLeft: 0 }} onClick={() => ctx.nav("project", { id })}><Icon name="chevL" size={15} /> {s?.title ?? "Source"}</button>
      <div className="row" style={{ marginBottom: 6 }}><div className="eyebrow">Clip Discovery</div></div>
      <h1 style={{ fontSize: 30, marginBottom: 18 }}>Proposed moments</h1>
      {!s ? (
        <Btn variant="primary" icon="import" onClick={() => ctx.nav("import")}>Import a video</Btn>
      ) : (
        <DiscoveryBody key={candidates[0]?.id.split("-")[0] ?? "none"} candidates={candidates} sourceId={id} finding={finding} />
      )}
    </div>
  );
}
