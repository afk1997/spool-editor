"use client";

import { useState } from "react";
import { useSpool } from "@/components/spool/context";
import { ClipCard } from "@/components/spool/cards";
import { Btn, Empty, Icon, Seg } from "@spool/ui";

/* S11 Clips Library — 1:1 port of the demo (06). Live clips grouped from the clip-job stream.
 * Aspect / tag / search filters are real; the "Best (85+)" collection lights up with the
 * Phase-3 opportunity score (no fabricated scores in Phase 1, so it stays empty until then). */

export default function ClipsScreen() {
  const ctx = useSpool();
  const [aspect, setAspect] = useState("all");
  const [coll, setColl] = useState("all");
  const [tag, setTag] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<string[]>([]);
  const allTags = [...new Set(ctx.clips.flatMap((c) => c.tags || []))];
  const clips = ctx.clips.filter((c) =>
    (aspect === "all" || c.aspect === aspect) &&
    (coll === "all" || (coll === "best" && (c.score ?? 0) >= 85) || coll === "week") &&
    (!tag || (c.tags || []).includes(tag)) &&
    (!q || c.title.toLowerCase().includes(q.toLowerCase())));
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  /** Download the selected clips' rendered files (skips clips that aren't rendered yet). */
  const exportSel = () => {
    const ready = ctx.clips.filter((c) => sel.includes(c.id) && c.renderId);
    if (!ready.length) { ctx.pushToast({ icon: "alert", tone: "warn", title: "Nothing to export", body: "None of the selected clips have a render yet." }); return; }
    for (const c of ready) {
      const a = document.createElement("a");
      a.href = ctx.client.renderFileUrl(c.id, c.renderId!);
      a.download = `${(c.title || c.id).replace(/[^\w.-]+/g, "_")}.mp4`;
      a.target = "_blank"; a.rel = "noreferrer";
      document.body.appendChild(a); a.click(); a.remove();
    }
    ctx.pushToast({ icon: "download", tone: "ok", title: `Exporting ${ready.length} clip${ready.length > 1 ? "s" : ""}`, body: "Saving to your downloads" });
  };

  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Clips</div><h1 style={{ fontSize: 30 }}>Finished clips</h1></div>
        <span className="spacer" />
        {sel.length > 0 && <><span className="chip acc">{sel.length} selected</span><Btn variant="ghost" size="sm" icon="download" onClick={exportSel}>Export</Btn><Btn variant="primary" size="sm" icon="send" onClick={() => ctx.nav("publish")}>Publish</Btn></>}
      </div>
      <div className="row" style={{ gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
        <Seg value={coll} onChange={setColl} neutral options={[{ value: "all", label: "All clips" }, { value: "best", label: "Best (85+)" }, { value: "week", label: "This week" }]} />
        <div className="divider" style={{ width: 1, height: 24, background: "var(--line)" }} />
        <Seg value={aspect} onChange={setAspect} neutral options={[{ value: "all", label: "All" }, { value: "9:16", label: "9:16" }, { value: "1:1", label: "1:1" }, { value: "16:9", label: "16:9" }]} />
        <div className="spacer" />
        <div className="cmdk" style={{ maxWidth: 220, flex: "0 1 220px" }}><Icon name="search" size={15} /><input value={q} onChange={(e) => setQ(e.target.value)} style={{ background: "transparent", border: 0, outline: "none", color: "var(--text)", flex: 1, fontFamily: "inherit" }} placeholder="Search clips…" /></div>
      </div>
      <div className="kbar" style={{ marginBottom: 20 }}>
        <button className={"chip" + (tag === null ? " solid" : "")} style={{ cursor: "pointer", height: 28 }} onClick={() => setTag(null)}>All tags</button>
        {allTags.map((t) => <button key={t} className={"chip" + (tag === t ? " solid" : "")} style={{ cursor: "pointer", height: 28 }} onClick={() => setTag(tag === t ? null : t)}><Icon name="pin" size={12} />{t}</button>)}
      </div>
      {clips.length === 0 ? (
        <Empty icon="scissors" title="No clips match these filters" action={<Btn variant="ghost" onClick={() => { setColl("all"); setAspect("all"); setTag(null); setQ(""); }}>Clear filters</Btn>} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(190px,1fr))", gap: 16 }}>
          {clips.map((c) => (
            // content-visibility skips render/layout for off-screen cards (native windowing for
            // a large library) with zero layout change; contain-intrinsic-size reserves space.
            <div key={c.id} style={{ position: "relative", contentVisibility: "auto", containIntrinsicSize: "auto 360px" }}>
              <div onClick={() => toggle(c.id)} className="checkbox" style={{ position: "absolute", top: 10, left: 10, zIndex: 5, background: sel.includes(c.id) ? "var(--accent)" : "rgba(0,0,0,0.5)", borderColor: sel.includes(c.id) ? "transparent" : "var(--line-str)", color: "var(--accent-ink)", cursor: "pointer" }}>{sel.includes(c.id) && <Icon name="check" size={12} />}</div>
              <ClipCard c={c} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
