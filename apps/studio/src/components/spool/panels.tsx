"use client";

import type { ReactNode } from "react";
import { Icon } from "@spool/ui";

/* Shared chrome for the Settings / Brand screens (SettingCard, Row) and the Phase 2/4
 * "designed — coming soon" screens (FutureScreen). Ported 1:1 from the demo (07). */

export function SettingCard({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card" style={{ padding: 18 }}><div className="eyebrow" style={{ marginBottom: 14 }}>{title}</div><div style={{ display: "flex", flexDirection: "column", gap: 14 }}>{children}</div></div>;
}

export function Row({ l, r, sub, subId }: { l?: ReactNode; r: ReactNode; sub?: string; subId?: string }) {
  return <div><div className="row" style={{ gap: 14, minHeight: 32 }}>{l && <span style={{ fontSize: 13.5, fontWeight: 500 }}>{l}</span>}<span className="spacer" />{r}</div>{sub && <div id={subId} style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 4 }}>{sub}</div>}</div>;
}

export function FutureScreen({ code, title, desc, phase, icon }: { code: string; title: string; desc: string; phase: string; icon: string; children?: ReactNode }) {
  return (
    <div className="mainpad fadein">
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>{code}</div><h1 style={{ fontSize: 30 }}>{title}</h1></div>
        <span className="spacer" /><span className="chip warn">Phase {phase}</span>
      </div>
      <p style={{ color: "var(--text-dim)", maxWidth: 560, marginTop: 0, marginBottom: 24, fontSize: 14.5, lineHeight: 1.6 }}>{desc}</p>
      <div className="card" style={{ padding: "18px 20px", display: "flex", gap: 11, alignItems: "center" }}>
        <Icon name={icon} size={18} style={{ color: "var(--accent)" }} />
        <div><div style={{ fontWeight: 600 }}>Unavailable in this phase</div><div style={{ marginTop: 3, color: "var(--text-faint)", fontSize: 12.5 }}>Planned for Phase {phase}. No preview controls are active yet.</div></div>
      </div>
    </div>
  );
}
