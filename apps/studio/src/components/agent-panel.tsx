"use client";

import { useState } from "react";
import { useLive } from "@/lib/engine-context";
import { Badge, StatusDot, cn } from "./ui";

const ENGINE_URL = process.env.NEXT_PUBLIC_SPOOL_ENGINE_URL ?? "http://127.0.0.1:8899";

/** The Agent panel (contextual + collapsible, review §6.6). Agent mode and manual mode
 *  are two clients of the same engine + queue (the golden rule), so this panel's P1 job is
 *  to make that shared work visible and to let you connect an MCP agent right now. The
 *  in-studio conversational surface (with inline elicitation cards) arrives with the
 *  server-side agent loop — until then the same tools are reachable from any MCP client. */
export function AgentPanel({ onClose }: { onClose: () => void }) {
  const { snapshot } = useLive();
  const [copied, setCopied] = useState(false);

  const activity = [...(snapshot?.clips ?? [])].slice(-8).reverse();

  const config = JSON.stringify(
    { mcpServers: { spool: { command: "trove-mcp", env: { TROVE_URL: ENGINE_URL } } } },
    null,
    2,
  );

  async function copy() {
    try {
      await navigator.clipboard.writeText(config);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the snippet is selectable in the <pre> */
    }
  }

  return (
    <aside
      className="fixed right-0 top-0 z-40 flex h-dvh w-agent flex-col border-l border-line bg-bg-1 shadow-pop"
      aria-label="Agent panel"
    >
      <header className="flex h-top items-center justify-between border-b border-line px-4">
        <span className="font-display font-semibold text-text">Agent</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close agent panel"
          className="rounded-sm px-2 py-1 text-text-faint hover:text-text focus-visible:outline-2 focus-visible:outline-accent"
        >
          ✕
        </button>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        <p className="text-sm text-text-dim">
          Spool&rsquo;s studio and any MCP agent drive the <strong className="text-text">same engine and queue</strong>.
          Connect an agent and its clips show up right here alongside yours.
        </p>

        <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-text-faint">Connect an agent</h3>
        <p className="mt-1 text-sm text-text-dim">
          Add this to your Claude Desktop / Cursor MCP config (the engine must be running):
        </p>
        <pre className="mt-2 overflow-x-auto rounded border border-line bg-bg-2 p-3 font-mono text-xs text-text">
          {config}
        </pre>
        <button
          type="button"
          onClick={copy}
          className="mt-2 rounded-sm border border-line bg-bg-2 px-2.5 py-1 text-xs font-medium text-text-dim hover:text-text focus-visible:outline-2 focus-visible:outline-accent"
        >
          {copied ? "Copied ✓" : "Copy config"}
        </button>

        <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-text-faint">Activity</h3>
        {activity.length === 0 ? (
          <p className="mt-1 text-sm text-text-faint">No clip activity yet.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1.5">
            {activity.map((c) => (
              <li key={c.id} className="flex items-center gap-2 text-sm">
                <StatusDot status={c.status} pulse={c.status === "running" || c.status === "queued"} />
                <Badge tone="neutral">{c.kind}</Badge>
                <span className="min-w-0 flex-1 truncate text-text-dim">{c.human?.summary ?? c.status}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <footer className="border-t border-line p-4">
        <p className={cn("rounded bg-bg-3 px-3 py-2 text-xs text-text-dim")}>
          In-studio chat with inline elicitation cards (pick candidates · aspect · caption
          style) arrives with the agent loop. Today, drive Spool from any connected MCP client.
        </p>
      </footer>
    </aside>
  );
}
