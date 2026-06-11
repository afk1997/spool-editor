import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { EventsSnapshot } from "@spool/types";

/* Identity/perf contract for SpoolProvider (the 4x-mapper recompute finding): unrelated state
 * changes — a toast, a panel toggle — must NOT churn the derived arrays. The mappers walk the
 * whole live snapshot; keyed on the snapshot, a non-snapshot re-render keeps the SAME array
 * objects, so useSpool() consumers don't re-render on every toast or ~1Hz SSE tick.
 *
 * The engine-context hooks (useEngine/useLive/useEngineQuery) and next's useRouter are stubbed
 * so the provider mounts against a FIXED snapshot — no SSE, no real client, deterministic. */

const FIXED_SNAPSHOT = {
  ts: 1,
  jobs: [
    {
      id: "j1", url: "https://youtu.be/x", title: "A talk", status: "done", progress_pct: 100,
      filename: "a.mp4", total_bytes: 1024, downloaded_bytes: 1024, elapsed_seconds: 10,
      error_message: null, human: { elapsed: "10s", summary: "", eta: "—", size: "1 KB", speed: "" },
    },
  ],
  transcripts: [],
  clips: [],
} as unknown as EventsSnapshot;

const router = { push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn(), refresh: vi.fn() };
const client = {} as never;

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("@/lib/engine-context", () => ({
  useEngine: () => client,
  useLive: () => ({ snapshot: FIXED_SNAPSHOT, connection: "online" }),
  useEngineQuery: () => ({ data: undefined, loading: false, reload: () => {} }),
}));

// Imported AFTER the mocks are registered (vi.mock is hoisted, but keep the order explicit).
const { SpoolProvider, useSpool } = await import("@/components/spool/context");

describe("SpoolProvider memoization (derived identity is stable across unrelated re-renders)", () => {
  it("derived arrays keep identity across unrelated state changes", () => {
    const identities: unknown[] = [];
    function Probe() {
      const ctx = useSpool();
      identities.push(ctx.sources);
      return <button onClick={() => ctx.pushToast({ title: "t" })}>toast</button>;
    }
    const { getByText } = render(
      <SpoolProvider>
        <Probe />
      </SpoolProvider>,
    );
    fireEvent.click(getByText("toast")); // a toast-driven re-render — snapshot is unchanged
    expect(identities.length).toBeGreaterThan(1);
    expect(identities[0]).toBe(identities[identities.length - 1]); // same array object
  });

  it("the snapshot still maps to real data (memoization didn't change the output)", () => {
    let captured: { sources: unknown[]; jobs: unknown[] } | null = null;
    function Probe() {
      const ctx = useSpool();
      captured = { sources: ctx.sources, jobs: ctx.jobs };
      return null;
    }
    render(
      <SpoolProvider>
        <Probe />
      </SpoolProvider>,
    );
    expect(captured!.sources).toHaveLength(1); // the one done job → one source
    expect(captured!.jobs).toHaveLength(1);
  });
});
