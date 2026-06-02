"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLive } from "@/lib/engine-context";
import { cn, StatusDot } from "./ui";
import { CommandPalette } from "./command-palette";
import { AgentPanel } from "./agent-panel";

/** The persistent studio chrome: icon+label rail, top bar, and a live status/queue bar —
 *  all driven by the SSE snapshot (spec §6, the demo's layout). Labels sit under each rail
 *  icon (carried review item §6.6). The heavy editor leaves lazy-load as their own routes. */

const NAV = [
  { href: "/", label: "Home", icon: IconHome },
  { href: "/import", label: "Import", icon: IconImport },
  { href: "/library", label: "Library", icon: IconLibrary },
  { href: "/clips", label: "Clips", icon: IconClips },
  { href: "/queue", label: "Queue", icon: IconQueue },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="grid h-dvh grid-cols-[var(--rail-w)_1fr] grid-rows-[var(--top-h)_1fr_var(--status-h)] bg-bg">
      {/* rail */}
      <nav
        className="row-span-3 flex flex-col items-center gap-1 border-r border-line bg-bg-1 py-3"
        aria-label="Primary"
      >
        <Link href="/" className="mb-3 grid h-9 w-9 place-items-center rounded bg-accent text-accent-ink font-display text-lg font-bold" aria-label="Spool home">
          S
        </Link>
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex w-[calc(var(--rail-w)-16px)] flex-col items-center gap-1 rounded px-1 py-2 text-[11px] font-medium",
                "transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-accent",
                active ? "bg-accent-soft text-accent" : "text-text-dim hover:bg-bg-3 hover:text-text",
              )}
            >
              <Icon />
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* top bar */}
      <header className="col-start-2 flex items-center justify-between border-b border-line bg-bg-1 px-5">
        <span className="font-display text-lg font-semibold tracking-tight text-text">Spool</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            aria-label="Open command palette"
            className="flex items-center gap-2 rounded-sm border border-line bg-bg-2 px-2.5 py-1 text-xs text-text-faint hover:text-text-dim focus-visible:outline-2 focus-visible:outline-accent"
          >
            Search
            <kbd className="font-mono">⌘K</kbd>
          </button>
          <button
            type="button"
            onClick={() => setAgentOpen((o) => !o)}
            aria-label="Toggle agent panel"
            aria-pressed={agentOpen}
            className={cn(
              "rounded-sm border border-line px-2.5 py-1 text-xs font-medium focus-visible:outline-2 focus-visible:outline-accent",
              agentOpen ? "bg-accent text-accent-ink" : "bg-bg-2 text-text-dim hover:text-text",
            )}
          >
            Agent
          </button>
        </div>
      </header>

      {/* main */}
      <main className="col-start-2 overflow-y-auto px-6 py-6">{children}</main>

      {/* status / queue bar */}
      <StatusBar />

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
      {agentOpen && <AgentPanel onClose={() => setAgentOpen(false)} />}
    </div>
  );
}

function StatusBar() {
  const { snapshot, connection } = useLive();
  const jobs = snapshot?.jobs ?? [];
  const clips = snapshot?.clips ?? [];
  const activeDownloads = jobs.filter((j) => j.status === "downloading" || j.status === "queued").length;
  const activeClips = clips.filter((c) => c.status === "running" || c.status === "queued").length;
  const label =
    connection === "online" ? "Engine connected" : connection === "connecting" ? "Connecting…" : "Engine offline";

  return (
    <footer className="col-start-2 flex items-center gap-4 border-t border-line bg-bg-1 px-5 text-xs text-text-dim">
      <span className="flex items-center gap-1.5">
        <StatusDot status={connection} pulse={connection === "connecting"} />
        {label}
      </span>
      <span className="tabular-nums">{activeDownloads} downloading</span>
      <span className="tabular-nums">{activeClips} rendering</span>
      <span className="ml-auto font-mono text-text-faint">local-first</span>
    </footer>
  );
}

// ── minimal inline icons (no icon dep) ──
function svg(children: React.ReactNode) {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {children}
    </svg>
  );
}
function IconHome() {
  return svg(<><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9v11h14V9" /></>);
}
function IconImport() {
  return svg(<><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>);
}
function IconLibrary() {
  return svg(<><rect x="3" y="4" width="18" height="14" rx="2" /><path d="M10 9l5 3-5 3V9Z" /></>);
}
function IconClips() {
  return svg(<><circle cx="6" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M20 4 8.5 15.5" /><path d="M20 20 8.5 8.5" /></>);
}
function IconQueue() {
  return svg(<><path d="M4 6h16" /><path d="M4 12h16" /><path d="M4 18h10" /></>);
}
