"use client";

import { FutureScreen } from "@/components/spool/panels";
import { Stat } from "@spool/ui";

/* Analytics — Phase 4. 1:1 port of the demo's FutureScreen placeholder (07). */
export default function AnalyticsScreen() {
  return (
    <FutureScreen code="Analytics" phase="4" icon="chart" title="Analytics" desc="Close the loop: see views, watch-through and shares per platform, learn what your winners have in common, and feed that back into the ranking weights.">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 16, marginBottom: 16 }}>{([["128.4k", "Views"], ["41%", "Watch-through"], ["3,201", "Shares"], ["+18%", "vs last week"]] as const).map(([v, l]) => <div key={l} className="card" style={{ padding: 18 }}><Stat v={v} l={l} /></div>)}</div>
      <div className="card" style={{ padding: 18 }}><svg width="100%" height="120" viewBox="0 0 400 120" preserveAspectRatio="none"><polyline fill="none" stroke="var(--accent)" strokeWidth="2" points={Array.from({ length: 24 }).map((_, i) => `${i * 17},${Math.round(110 - Math.abs(Math.sin(i * 0.5)) * 90)}`).join(" ")} /></svg></div>
    </FutureScreen>
  );
}
