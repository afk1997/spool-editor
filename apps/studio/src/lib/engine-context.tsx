"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
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
  /** Bumps each time the SSE stream *recovers* from a drop — one-shot queries watch it so
   *  a transient engine blip doesn't leave them stuck on a stale error (e.g. the doctor probe). */
  onlineEpoch: number;
}

const EngineContext = createContext<EngineContextValue | null>(null);

export function EngineProvider({ children }: { children: React.ReactNode }) {
  const [live, setLive] = useState<LiveState>({ snapshot: null, connection: "connecting" });
  const [onlineEpoch, setOnlineEpoch] = useState(0);
  const sawOffline = useRef(false);

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
          // Recovered after a drop → re-run the one-shot queries (doctor, capabilities, …).
          if (sawOffline.current) {
            sawOffline.current = false;
            setOnlineEpoch((e) => e + 1);
          }
        },
        {
          interval: 1,
          onError: () => {
            if (cancelled) return;
            sawOffline.current = true;
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

  return (
    <EngineContext.Provider value={{ client: engine, live, onlineEpoch }}>{children}</EngineContext.Provider>
  );
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
  /** Monotonic id of the newest request that has started, including one still in flight. */
  requestGeneration: number;
  /** Generation that produced `data`; older data may remain visible while a reload runs. */
  dataGeneration: number;
  /** Reads the live request id without waiting for React to publish the next render. */
  getRequestGeneration: () => number;
}

/** One-shot read against the engine, with loading/error/data + manual reload. */
export function useEngineQuery<T>(
  fn: (client: SpoolApiClient) => Promise<T>,
  deps: unknown[] = [],
): QueryState<T> {
  const { client, onlineEpoch } = useEngineContext();
  const requestGeneration = useRef(0);
  const getRequestGeneration = useCallback(() => requestGeneration.current, []);
  const [state, setState] = useState<{
    data?: T;
    error?: string;
    loading: boolean;
    requestGeneration: number;
    dataGeneration: number;
  }>({ loading: true, requestGeneration: 0, dataGeneration: 0 });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let active = true;
    const generation = ++requestGeneration.current;
    setState((current) => ({
      ...current,
      error: undefined,
      loading: true,
      requestGeneration: generation,
    }));
    fn(client)
      .then((data) => active && setState({
        data,
        loading: false,
        requestGeneration: generation,
        dataGeneration: generation,
      }))
      .catch((e) =>
        active && setState({
          error: e instanceof SpoolApiError ? e.code : "unreachable",
          loading: false,
          requestGeneration: generation,
          dataGeneration: 0,
        }),
      );
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, onlineEpoch, ...deps]);

  const reload = () => {
    setState((s) => ({ ...s, loading: true, error: undefined }));
    setTick((t) => t + 1);
  };
  return { ...state, reload, getRequestGeneration };
}
