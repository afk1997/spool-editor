"use client";

import { useEffect, useState } from "react";
import { SpoolApiError } from "@spool/api-client";
import { engine } from "@/lib/engine";

type EngineState =
  | { kind: "checking" }
  | { kind: "online"; version: string }
  | { kind: "offline"; reason: string };

export default function Home() {
  const [state, setState] = useState<EngineState>({ kind: "checking" });

  useEffect(() => {
    let active = true;
    engine
      .health()
      .then((h) => {
        if (active) setState({ kind: "online", version: h.version });
      })
      .catch((err: unknown) => {
        if (!active) return;
        setState({ kind: "offline", reason: err instanceof SpoolApiError ? err.code : "unreachable" });
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header className="space-y-3">
        <h1 className="text-4xl font-semibold tracking-tight">Spool</h1>
        <p className="text-pretty text-lg text-neutral-600">
          Local-first clip studio. Turn long videos into platform-ready vertical clips —
          entirely on your machine.
        </p>
      </header>

      <EngineStatus state={state} />

      <footer className="text-sm text-neutral-500">
        Phase 0 — foundation. The Phase-1 screens (import, library, transcript, discovery,
        reframe, captions, render queue) wire to this engine next.
      </footer>
    </main>
  );
}

function EngineStatus({ state }: { state: EngineState }) {
  const dot =
    state.kind === "online"
      ? "bg-green-500"
      : state.kind === "offline"
        ? "bg-amber-500"
        : "bg-neutral-300 animate-pulse";

  const label =
    state.kind === "online"
      ? "Engine connected"
      : state.kind === "offline"
        ? "Engine offline"
        : "Checking engine…";

  return (
    <section className="rounded-xl border border-neutral-200 p-5">
      <div className="flex items-center gap-2.5">
        <span className={`h-2.5 w-2.5 rounded-full ${dot}`} aria-hidden />
        <span className="font-medium">{label}</span>
      </div>

      {state.kind === "online" && (
        <p className="mt-2 text-sm text-neutral-600">
          JSON API <code className="font-mono">{state.version}</code> reachable.
        </p>
      )}

      {state.kind === "offline" && (
        <div className="mt-2 space-y-2 text-sm text-neutral-600">
          <p>
            Couldn&rsquo;t reach the engine (<code className="font-mono">{state.reason}</code>).
            Start it, then this will reconnect:
          </p>
          <pre className="overflow-x-auto rounded-lg bg-neutral-100 p-3 font-mono text-xs">
            cd engine &amp;&amp; ./trove.sh
          </pre>
        </div>
      )}
    </section>
  );
}
