"use client";

import { useSpool, type SpoolClip, type SpoolSource } from "./context";
import { AspectBadge, Chip, Icon, Progress, SourceGlyph, Thumb, fmtDur } from "@spool/ui";

/* 1:1 ports of the demo's MediaCard + ClipCard (03), fed by the live-mapped shapes. */

export function MediaCard({ s, onOpen }: { s: SpoolSource; onOpen: (s: SpoolSource) => void }) {
  const ctx = useSpool();
  const statusChip: Record<string, React.ReactNode> = {
    ready: <Chip tone="ok" dot>ready</Chip>,
    downloaded: <Chip dot>downloaded</Chip>,
    transcribing: <Chip tone="info" dot>transcribing</Chip>,
    downloading: <Chip tone="info" dot>downloading</Chip>,
    "no-candidates": <Chip tone="warn" dot>no clips yet</Chip>,
  };
  const knownOrigin = s.src && s.src !== "—";
  const knownKind = s.kind && s.kind !== "—";
  return (
    <div className="mcard" onClick={() => onOpen(s)}>
      <Thumb seed={s.id} kind={knownKind ? s.kind : ""}>
        {knownOrigin && <div className="tl"><SourceGlyph type={s.src} /></div>}
        {s.dur > 0 && <div className="br"><span className="badge mono">{fmtDur(s.dur)}</span></div>}
        {s.status === "transcribing" && (
          <div className="hoveractions" style={{ opacity: 1, background: "rgba(8,9,11,0.45)" }}>
            <div style={{ textAlign: "center", width: "78%" }}>
              <div className="mono" style={{ fontSize: 11, color: "#fff", marginBottom: 8 }}>transcribing · {s.prog}%</div>
              <Progress value={s.prog ?? 0} tone="info" striped />
            </div>
          </div>
        )}
        <div className="hoveractions">
          <button className="roundbtn" title="Open project" onClick={(e) => { e.stopPropagation(); onOpen(s); }}><Icon name="play" size={16} /></button>
          <button className="roundbtn" title="Open transcript to cut manually" onClick={(e) => { e.stopPropagation(); ctx.nav("project", { id: s.id, tab: "Transcript" }); }}><Icon name="scissors" size={16} /></button>
          <button className="roundbtn" title="Transcript" onClick={(e) => { e.stopPropagation(); ctx.nav("project", { id: s.id }); }}><Icon name="type" size={16} /></button>
        </div>
      </Thumb>
      <div className="meta">
        <div className="ttl">{s.title}</div>
        <div className="subline">
          {statusChip[s.status]}
          {(s.clips > 0 || knownKind) && (
            <span style={{ marginLeft: "auto" }} className="mono">
              {s.clips > 0 ? `${s.clips} clips` : s.kind.split(" · ")[0]}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export function ClipCard({ c, onOpen }: { c: SpoolClip; onOpen?: (c: SpoolClip) => void }) {
  const ctx = useSpool();
  const plat: Record<string, string> = { tiktok: "TikTok", reels: "Reels", shorts: "Shorts", linkedin: "LinkedIn", youtube: "YouTube", x: "X" };
  return (
    <div className="mcard" onClick={() => (onOpen ? onOpen(c) : ctx.nav("editor", { id: c.id }))}>
      <Thumb seed={c.id} vertical={c.aspect === "9:16"} kind={c.style} label={false}>
        {c.aspect && <div className="tr"><AspectBadge a={c.aspect} /></div>}
        {c.dur > 0 && <div className="br"><span className="badge mono">{fmtDur(c.dur)}</span></div>}
        <div style={{ position: "absolute", left: 0, right: 0, bottom: "24%", textAlign: "center", padding: "0 10%" }}>
          <span style={{ fontFamily: "var(--font-caption)", fontSize: c.aspect === "9:16" ? 15 : 13, color: "#fff", textShadow: "0 2px 6px #000", lineHeight: 1.1 }}>
            {c.title.split(" ").slice(0, 3).map((w, i) => <span key={i} style={{ color: i === 1 ? "var(--caption-hl)" : "#fff" }}>{w} </span>)}
          </span>
        </div>
        {c.status === "rendering" && (
          <div className="hoveractions" style={{ opacity: 1, background: "rgba(8,9,11,0.5)" }}>
            <div style={{ textAlign: "center", width: "78%" }}>
              <div className="mono" style={{ fontSize: 11, color: "#fff", marginBottom: 8 }}>rendering · {c.prog}%</div>
              <Progress value={c.prog ?? 0} striped />
            </div>
          </div>
        )}
        {c.status === "queued" && <div className="hoveractions" style={{ opacity: 1, background: "rgba(8,9,11,0.55)" }}><Chip tone="warn" dot>queued</Chip></div>}
        <div className="hoveractions">
          <button className="roundbtn" title="Open in editor" onClick={(e) => { e.stopPropagation(); ctx.nav("editor", { id: c.id }); }}><Icon name="pen" size={15} /></button>
        </div>
      </Thumb>
      <div className="meta">
        <div className="ttl" style={{ WebkitLineClamp: 1 }}>{c.title}</div>
        {(c.style || c.tags?.[0] || (c.platform && plat[c.platform])) && (
          <div className="subline">
            {c.style && <span className="chip">{c.style}</span>}
            {c.tags?.[0] && <span className="chip acc">{c.tags[0]}</span>}
            {c.platform && plat[c.platform] && <span style={{ marginLeft: "auto" }}>{plat[c.platform]}</span>}
          </div>
        )}
      </div>
    </div>
  );
}
