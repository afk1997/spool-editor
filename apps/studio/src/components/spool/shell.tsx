"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLive } from "@/lib/engine-context";
import { Icon, SpoolMark } from "./ui";

/* Faithful port of the demo's shell — Rail · TopBar · StatusBar · .shell grid (app.jsx).
 * Same markup + class names so spool.css renders it 1:1. Nav maps to Next routes; the
 * status bar reads the live SSE snapshot. Overlays (agent panel, ⌘K) port next. */

const NAV = [
  { href: "/", icon: "home", label: "Home" },
  { href: "/import", icon: "import", label: "Import" },
  { href: "/library", icon: "film", label: "Library" },
  { href: "/clips", icon: "scissors", label: "Clips" },
  { href: "/queue", icon: "layers", label: "Queue" },
  { sep: true },
  { href: "/publish", icon: "send", label: "Publish" },
  { href: "/analytics", icon: "chart", label: "Analyze" },
] as const;

function useRunningCount() {
  const { snapshot } = useLive();
  const dl = (snapshot?.jobs ?? []).filter((j) => j.status === "downloading" || j.status === "queued").length;
  const rn = (snapshot?.clips ?? []).filter((c) => c.status === "running" || c.status === "queued").length;
  return dl + rn;
}

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="shell">
      <Rail />
      <TopBar />
      <div className="bodywrap">
        <div className="main">{children}</div>
      </div>
      <StatusBar />
    </div>
  );
}

function Rail() {
  const pathname = usePathname();
  const active = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);
  const running = useRunningCount();
  return (
    <div className="rail">
      <Link href="/" className="brand" style={{ cursor: "pointer" }}>
        <SpoolMark size={40} />
      </Link>
      <div className="railnav">
        {NAV.map((n, i) =>
          "sep" in n ? (
            <div key={i} className="railsep" />
          ) : (
            <Link key={n.href} href={n.href} className={"railbtn" + (active(n.href) ? " active" : "")}>
              <Icon name={n.icon} size={20} />
              <span className="rlabel">{n.label}</span>
              {n.href === "/queue" && running > 0 && <span className="dotbadge">{running}</span>}
            </Link>
          ),
        )}
      </div>
      <div className="spacer" />
      <Link href="/settings" className={"railbtn" + (active("/settings") ? " active" : "")}>
        <Icon name="settings" size={20} />
        <span className="rlabel">Settings</span>
      </Link>
    </div>
  );
}

function TopBar() {
  const running = useRunningCount();
  return (
    <div className="topbar">
      <div className="row" style={{ gap: 9, paddingRight: 8 }}>
        <span className="wordmark" style={{ fontSize: 22, lineHeight: 1 }}>Spool</span>
      </div>
      <div className="cmdk">
        <Icon name="search" size={15} />
        <span style={{ fontSize: 13 }}>Search, run actions, or ask the agent…</span>
        <span className="kbd">⌘K</span>
      </div>
      <span className="spacer" />
      <Link href="/settings" className="pill" title="Privacy"><span className="led" />On-device</Link>
      <Link href="/queue" className="pill"><Icon name="layers" size={14} />Queue<span className="chip acc" style={{ height: 18, padding: "0 6px" }}>{running}</span></Link>
      <Link href="/settings" className="iconbtn" aria-label="Settings"><Icon name="settings" size={17} /></Link>
    </div>
  );
}

function StatusBar() {
  const { snapshot, connection } = useLive();
  const clips = snapshot?.clips ?? [];
  const jobs = snapshot?.jobs ?? [];
  const lead =
    clips.find((c) => c.status === "running") ??
    jobs.find((j) => j.status === "downloading");
  const queued =
    clips.filter((c) => c.status === "queued").length + jobs.filter((j) => j.status === "queued").length;
  const leadPct = lead ? ("progress_pct" in lead ? lead.progress_pct : 0) : 0;
  const leadLabel = lead ? ("kind" in lead ? lead.kind : "download") : "";
  return (
    <div className="statusbar">
      {lead ? (
        <div className="row" style={{ gap: 10 }}>
          <div style={{ width: 90, height: 5 }} className="bar striped"><i style={{ width: leadPct + "%" }} /></div>
          <span className="mono">{leadLabel} {Math.round(leadPct)}%</span>
        </div>
      ) : (
        <span className="mono">{connection === "online" ? "idle" : connection}</span>
      )}
      <span style={{ color: "var(--text-faint)" }}>·</span>
      <span className="mono">{queued} queued</span>
      <span className="spacer" />
      <span className="row" style={{ gap: 6 }} title="On-device"><Icon name="shield" size={13} style={{ color: "var(--ok)" }} />offline · on-device</span>
      <span style={{ color: "var(--text-faint)" }}>·</span>
      <span className="mono">{connection === "online" ? "engine connected" : "engine offline"}</span>
    </div>
  );
}
