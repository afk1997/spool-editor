"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { SpoolApiClient, SpoolApiError } from "@spool/api-client";
import type { EventsSnapshot } from "@spool/types";
import { engine } from "./engine";

/**
 * The studio's live-data layer. The engine pushes a full `{jobs, transcripts, clips}`
 * snapshot over SSE, so a single subscription feeding this context is the right model
 * (vs per-query polling): every progress/queue UI reads the same live state — never
 * fabricated (spec §6.2). One-shot reads (capabilities, doctor, …) use `useEngineQuery`.
 */

export type Connection = "connecting" | "online" | "offline";

interface LiveState {
  snapshot: EventsSnapshot | null;
  connection: Connection;
}

interface EngineContextValue {
  client: SpoolApiClient;
  live: LiveState;
}

const EngineContext = createContext<EngineContextValue | null>(null);

export function EngineProvider({ children }: { children: React.ReactNode }) {
  const [live, setLive] = useState<LiveState>({ snapshot: null, connection: "connecting" });

  useEffect(() => {
    let cancelled = false;
    let stop = () => {};
    let backoff = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      stop = engine.subscribeEvents(
        (snapshot) => {
          backoff = 1000;
          setLive({ snapshot, connection: "online" });
        },
        {
          interval: 1,
          onError: () => {
            if (cancelled) return;
            setLive((s) => ({ ...s, connection: "offline" }));
            timer = setTimeout(connect, backoff);
            backoff = Math.min(backoff * 2, 15000);
          },
        },
      );
    };
    connect();

    return () => {
      cancelled = true;
      stop();
      if (timer) clearTimeout(timer);
    };
  }, []);

  return <EngineContext.Provider value={{ client: engine, live }}>{children}</EngineContext.Provider>;
}

function useEngineContext(): EngineContextValue {
  const ctx = useContext(EngineContext);
  if (!ctx) throw new Error("useEngine* must be used within <EngineProvider>");
  return ctx;
}

/** The singleton typed client. */
export function useEngine(): SpoolApiClient {
  return useEngineContext().client;
}

/** The latest live snapshot + connection status (driven by the SSE stream). */
export function useLive(): LiveState {
  return useEngineContext().live;
}

export interface QueryState<T> {
  data?: T;
  error?: string;
  loading: boolean;
  reload: () => void;
}

/** One-shot read against the engine, with loading/error/data + manual reload. */
export function useEngineQuery<T>(
  fn: (client: SpoolApiClient) => Promise<T>,
  deps: unknown[] = [],
): QueryState<T> {
  const client = useEngine();
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let active = true;
    fn(client)
      .then((data) => active && setState({ data, loading: false }))
      .catch((e) =>
        active && setState({ error: e instanceof SpoolApiError ? e.code : "unreachable", loading: false }),
      );
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  const reload = () => {
    setState((s) => ({ ...s, loading: true, error: undefined }));
    setTick((t) => t + 1);
  };
  return { ...state, reload };
}
