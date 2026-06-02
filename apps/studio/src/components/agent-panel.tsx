"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { SpoolApiError } from "@spool/api-client";
import type { ClipJobView } from "@spool/types";
import { useEngine } from "@/lib/engine-context";
import { Button, cn } from "./ui";

const ENGINE_URL = process.env.NEXT_PUBLIC_SPOOL_ENGINE_URL ?? "http://127.0.0.1:8899";

interface Msg {
  role: "user" | "assistant";
  text: string;
  jobs?: ClipJobView[];
  options?: string[];
}

/** The Agent panel (contextual + collapsible, review §6.6). A real chat that drives the
 *  engine's /agent endpoint: your message → the LLM picks a clip-tool action → the engine
 *  runs it on the same queue the UI uses (the golden rule). A `clarify` reply renders as an
 *  elicitation card (option buttons); spawned work shows as chips into the queue/clips. On a
 *  source page the open source is passed as context automatically. */
export function AgentPanel({ onClose }: { onClose: () => void }) {
  const client = useEngine();
  const pathname = usePathname();
  const sourceId = pathname.match(/^\/sources\/([^/]+)/)?.[1];

  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function send(text: string) {
    const msg = text.trim();
    if (!msg || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: msg }]);
    setBusy(true);
    try {
      const res = await client.agent(msg, { sourceId });
      setMessages((m) => [
        ...m,
        { role: "assistant", text: res.reply, jobs: res.jobs, options: res.action === "clarify" ? res.options : undefined },
      ]);
    } catch (e) {
      const code = e instanceof SpoolApiError ? e.code : "unreachable";
      setMessages((m) => [
        ...m,
        { role: "assistant", text: code === "llm_unavailable"
            ? "The moment-finding LLM isn't reachable. Is Codex installed + logged in (or set SPOOL_LLM_PROVIDER)?"
            : `Something went wrong (${code}).` },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="fixed right-0 top-0 z-40 flex h-dvh w-agent flex-col border-l border-line bg-bg-1 shadow-pop" aria-label="Agent panel">
      <header className="flex h-top items-center justify-between border-b border-line px-4">
        <span className="font-display font-semibold text-text">Agent</span>
        <button type="button" onClick={onClose} aria-label="Close agent panel"
          className="rounded-sm px-2 py-1 text-text-faint hover:text-text focus-visible:outline-2 focus-visible:outline-accent">✕</button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 && (
          <div className="text-sm text-text-dim">
            <p>Ask me to find moments or cut a clip — I drive the same engine the buttons do.</p>
            <ul className="mt-3 space-y-1.5">
              {["Find the funniest moments", "Make a 9:16 clip of the best line with captions", "What's this video about?"].map((s) => (
                <li key={s}>
                  <button onClick={() => send(s)} className="text-left text-accent hover:underline">“{s}”</button>
                </li>
              ))}
            </ul>
            {!sourceId && <p className="mt-3 text-xs text-text-faint">Tip: open a source to give me its transcript as context.</p>}
          </div>
        )}

        <ul className="flex flex-col gap-3">
          {messages.map((m, i) => (
            <li key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div className={cn(
                "max-w-[90%] rounded-lg px-3 py-2 text-sm",
                m.role === "user" ? "bg-accent text-accent-ink" : "border border-line bg-bg-2 text-text",
              )}>
                <p className="whitespace-pre-wrap">{m.text}</p>
                {m.jobs && m.jobs.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {m.jobs.map((j) => (
                      <Link key={j.id} href={j.kind === "moments" && j.source_id ? `/sources/${j.source_id}` : j.clip_id ? `/clips/${j.clip_id}` : "/queue"}
                        className="rounded-sm bg-bg-3 px-2 py-0.5 font-mono text-xs text-accent hover:underline">
                        {j.kind} →
                      </Link>
                    ))}
                  </div>
                )}
                {m.options && m.options.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {m.options.map((o) => (
                      <button key={o} onClick={() => send(o)}
                        className="rounded-sm border border-line bg-bg-1 px-2 py-1 text-xs font-medium text-text hover:bg-bg-3">{o}</button>
                    ))}
                  </div>
                )}
              </div>
            </li>
          ))}
          {busy && (
            <li className="flex justify-start">
              <div className="flex items-center gap-2 rounded-lg border border-line bg-bg-2 px-3 py-2 text-sm text-text-dim">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-line border-t-accent" aria-hidden />
                thinking…
              </div>
            </li>
          )}
          <div ref={endRef} />
        </ul>
      </div>

      <form
        onSubmit={(e) => { e.preventDefault(); send(input); }}
        className="border-t border-line p-3"
      >
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
            rows={2}
            placeholder={sourceId ? "Ask about this source…" : "Ask the agent…"}
            aria-label="Message the agent"
            className="min-h-[44px] flex-1 resize-none rounded border border-line bg-bg-2 px-3 py-2 text-sm text-text placeholder:text-text-faint focus-visible:outline-2 focus-visible:outline-accent"
          />
          <Button type="submit" disabled={busy || !input.trim()} className="min-h-[44px]">Send</Button>
        </div>
        <details className="mt-2 text-xs text-text-faint">
          <summary className="cursor-pointer hover:text-text-dim">Connect an external agent (Claude Desktop / Cursor)</summary>
          <pre className="mt-2 overflow-x-auto rounded border border-line bg-bg-2 p-2 font-mono text-text">
{JSON.stringify({ mcpServers: { spool: { command: "trove-mcp", env: { TROVE_URL: ENGINE_URL } } } }, null, 2)}
          </pre>
        </details>
      </form>
    </aside>
  );
}
