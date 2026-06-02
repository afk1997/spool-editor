"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSpool } from "./context";
import { useLive } from "@/lib/engine-context";
import { AgentPanel } from "./agent";
import { CommandPalette, ShortcutSheet, Toasts } from "./overlays";
import { Icon, SpoolMark } from "@spool/ui";

/* Faithful port of the demo's shell (app.jsx) — Rail · TopBar · bodywrap(main + AgentPanel) ·
 * StatusBar — plus the ⌘K palette, ? shortcut sheet and toasts. Same markup + class names so
 * spool.css renders it 1:1. Onboarding renders full-screen with no shell (matches App.jsx). */

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

export function Shell({ children }: { children: React.ReactNode }) {
  const ctx = useSpool();
  const pathname = usePathname();
  const onboarding = pathname === "/onboarding";

  // global keys — ⌘K palette · ? shortcuts · / focus agent · Esc dismiss (app.jsx)
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      const typing = el && ["INPUT", "TEXTAREA"].includes(el.tagName);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if (ctx.paletteOpen) ctx.closePalette(); else ctx.openPalette(); }
      else if (e.key === "?" && !typing) { e.preventDefault(); if (ctx.shortcutsOpen) ctx.closeShortcuts(); else ctx.openShortcuts(); }
      else if (e.key === "Escape") { ctx.closeShortcuts(); }
      else if (e.key === "/" && !typing) { e.preventDefault(); ctx.openAgent(); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [ctx]);

  if (onboarding) {
    return <>{children}<Toasts /><CommandPalette /><ShortcutSheet /></>;
  }

  return (
    <>
      <div className="shell">
        <Rail />
        <TopBar />
        <div className="bodywrap">
          <div className="main">{children}</div>
          <AgentPanel />
        </div>
        <StatusBar />
      </div>
      <Toasts />
      <CommandPalette />
      <ShortcutSheet />
    </>
  );
}

function useRunningCount() {
  const { snapshot } = useLive();
  const dl = (snapshot?.jobs ?? []).filter((j) => j.status === "downloading" || j.status === "queued").length;
  const rn = (snapshot?.clips ?? []).filter((c) => c.status === "running" || c.status === "queued").length;
  const tx = (snapshot?.transcripts ?? []).filter((t) => t.status === "running" || t.status === "queued").length;
  return dl + rn + tx;
}

function Rail() {
  const pathname = usePathname();
  const isWork = pathname.startsWith("/sources") || pathname.startsWith("/clips");
  const active = (href: string) =>
    href === "/" ? pathname === "/" : href === "/library" ? (pathname.startsWith("/library") || isWork) : pathname.startsWith(href);
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
  const ctx = useSpool();
  const running = useRunningCount();
  return (
    <div className="topbar">
      <div className="row" style={{ gap: 9, paddingRight: 8 }}>
        <span className="wordmark" style={{ fontSize: 22, lineHeight: 1 }}>Spool</span>
      </div>
      <div className="cmdk" onClick={ctx.openPalette}>
        <Icon name="search" size={15} />
        <span style={{ fontSize: 13 }}>Search, run actions, or ask the agent…</span>
        <span className="kbd">⌘K</span>
      </div>
      <span className="spacer" />
      <div className="pill" onClick={() => ctx.nav("settings")} title="Privacy"><span className="led" />{ctx.offline ? "On-device" : "Online"}</div>
      <div className="pill" onClick={() => ctx.nav("queue")}><Icon name="layers" size={14} />Queue<span className="chip acc" style={{ height: 18, padding: "0 6px" }}>{running}</span></div>
      <button className="iconbtn" aria-label="Settings" onClick={() => ctx.nav("settings")}><Icon name="settings" size={17} /></button>
      <button className="iconbtn" aria-label="Keyboard shortcuts" onClick={ctx.openShortcuts}><Icon name="help" size={17} /></button>
      <div className="divider" style={{ width: 1, height: 22, background: "var(--line)", margin: "0 2px" }} />
      <button className="iconbtn" aria-label="Toggle agent panel" onClick={ctx.toggleAgent} title="Toggle agent" style={{ color: ctx.agentOpen ? "var(--accent)" : "var(--text-dim)" }}><Icon name="sparkles" size={18} /></button>
    </div>
  );
}

function StatusBar() {
  const ctx = useSpool();
  const { connection } = useLive();
  const running = ctx.jobs.filter((j) => j.status === "running");
  const queued = ctx.jobs.filter((j) => j.status === "queued").length;
  const lead = running[0];
  return (
    <div className="statusbar">
      {lead ? (
        <div className="row" style={{ gap: 10 }}>
          <div style={{ width: 90, height: 5 }} className="bar striped"><i style={{ width: lead.prog + "%" }} /></div>
          <span className="mono">{lead.type} “{lead.label.split("·")[0].trim().slice(0, 26)}” {Math.round(lead.prog)}%</span>
        </div>
      ) : (
        <span className="mono">{connection === "online" ? "idle" : connection}</span>
      )}
      <span style={{ color: "var(--text-faint)" }}>·</span>
      <span className="mono">{queued} queued</span>
      <span className="spacer" />
      <span className="row" style={{ gap: 6 }} title="On-device"><Icon name="shield" size={13} style={{ color: "var(--ok)" }} />{ctx.offline ? "offline · on-device" : "online"}</span>
      <span style={{ color: "var(--text-faint)" }}>·</span>
      <span className="mono">{connection === "online" ? "engine connected" : "engine offline"}</span>
    </div>
  );
}
