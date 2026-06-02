"use client";

import { useSpool } from "@/components/spool/context";
import { FutureScreen } from "@/components/spool/panels";
import { Thumb } from "@/components/spool/ui";

/* Publish & Calendar — Phase 4. 1:1 port of the demo's FutureScreen placeholder (07). */
export default function PublishScreen() {
  const ctx = useSpool();
  return (
    <FutureScreen code="Publish" phase="4" icon="send" title="Publish & Calendar" desc="Drag finished clips onto a calendar with per-platform lanes. Connect TikTok, Reels, Shorts, LinkedIn and X, schedule posts, and let the agent suggest best times.">
      <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: 18 }}>
        <div className="card" style={{ padding: 14 }}>{ctx.clips.slice(0, 3).map((c) => <div key={c.id} className="row" style={{ gap: 10, padding: "7px 0" }}><div style={{ width: 36, height: 48, borderRadius: 6, overflow: "hidden", flex: "none" }}><Thumb seed={c.id} vertical kind="" label={false} /></div><span style={{ fontSize: 12.5 }}>{c.title.split(" ").slice(0, 4).join(" ")}</span></div>)}</div>
        <div className="card" style={{ padding: 14 }}><div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 6 }}>{Array.from({ length: 21 }).map((_, i) => <div key={i} style={{ aspectRatio: "1", borderRadius: 7, background: "var(--bg-3)", display: "grid", placeItems: "center", fontSize: 11, color: "var(--text-faint)" }}>{i + 1}</div>)}</div></div>
      </div>
    </FutureScreen>
  );
}
