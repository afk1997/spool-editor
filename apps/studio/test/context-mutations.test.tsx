import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "@testing-library/react";
import type { EventsSnapshot } from "@spool/types";

/* Mutation-chain failure surfacing (the "fire-and-forget" finding): a dependent render chain
 * (reframe → caption → export) must AWAIT each submit, surface a submit/validation failure as a
 * warn toast, and never silently toast success on an empty queue. These exercise the provider's
 * makeClipsFrom directly (both paths); the page-level burn/apply share the same awaitClipJob /
 * try-catch shape, covered by typecheck + the shared path here.
 *
 * The engine-context hooks + next's useRouter are stubbed so the provider mounts against a FIXED
 * snapshot with a per-test STUBBED client — no SSE, deterministic, no real network. */

const FIXED_SNAPSHOT = { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot;

const router = { push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn(), refresh: vi.fn() };

// A per-test mutable client: each test assigns the method stubs it needs before mounting.
let client: Record<string, ReturnType<typeof vi.fn>>;

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("@/lib/engine-context", () => ({
  useEngine: () => client,
  useLive: () => ({ snapshot: FIXED_SNAPSHOT, connection: "online" }),
  useEngineQuery: () => ({ data: undefined, loading: false, reload: () => {} }),
}));

// Imported AFTER the mocks are registered (vi.mock is hoisted, but keep the order explicit).
const { SpoolProvider, useSpool } = await import("@/components/spool/context");
type Ctx = ReturnType<typeof useSpool>;

// Mount the provider and hand the live context back via a ref, so a test can drive
// ctx.makeClipsFrom(...) and read ctx.toasts after async chains settle.
function mountCtx(): { get: () => Ctx } {
  const ref: { current: Ctx | null } = { current: null };
  function Probe() {
    ref.current = useSpool();
    return null;
  }
  render(
    <SpoolProvider>
      <Probe />
    </SpoolProvider>,
  );
  return { get: () => ref.current! };
}

beforeEach(() => {
  router.push.mockClear();
});

describe("makeClipsFrom surfaces mutation-chain failures (no silent fire-and-forget)", () => {
  it("fresh path: a partially-failed batch warns '1 of 2' AND still fires the info toast", async () => {
    client = {
      renderPipeline: vi
        .fn()
        .mockResolvedValueOnce({ id: "j1" })
        .mockRejectedValueOnce(new Error("409")),
    };
    const ctx = mountCtx();

    act(() => {
      ctx.get().makeClipsFrom([
        { source_id: "s", start: 0, end: 1 },
        { source_id: "s", start: 2, end: 3 },
      ]);
    });

    // The info "Cutting" toast is synchronous; the user lands in the Clips tab immediately.
    expect(ctx.get().toasts.some((t) => /Cutting 2 clips/.test(t.title))).toBe(true);
    expect(router.push).toHaveBeenCalledWith("/sources/s?tab=Clips");
    expect(client.renderPipeline).toHaveBeenCalledTimes(2);

    // The warn toast appears once Promise.allSettled resolves the rejected start.
    await waitFor(() =>
      expect(ctx.get().toasts.some((t) => /1 of 2 clips failed to start/.test(t.title))).toBe(true),
    );
    const warn = ctx.get().toasts.find((t) => /1 of 2 clips failed to start/.test(t.title));
    expect(warn?.tone).toBe("warn");
  });

  it("fresh path: an all-success batch fires NO warn toast", async () => {
    client = { renderPipeline: vi.fn().mockResolvedValue({ id: "j1" }) };
    const ctx = mountCtx();

    act(() => {
      ctx.get().makeClipsFrom([{ source_id: "s", start: 0, end: 1 }]);
    });
    // Let any pending allSettled().then() flush.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(ctx.get().toasts.some((t) => /Cutting 1 clip\b/.test(t.title))).toBe(true);
    expect(ctx.get().toasts.some((t) => /failed to start/.test(t.title))).toBe(false);
  });

  it("existing path: a caption submit rejection warns 'Render chain failed' and never calls render", async () => {
    client = {
      reframe: vi.fn().mockResolvedValue({ id: "r1" }),
      caption: vi.fn().mockRejectedValue(new Error("boom")),
      render: vi.fn().mockResolvedValue({ id: "x1" }),
      getClipJob: vi.fn().mockResolvedValue({ id: "any", status: "done" }),
    };
    const ctx = mountCtx();

    act(() => {
      ctx.get().makeClipsFrom([{ id: "c1" }]); // no aspect/mode → skips reframe, goes straight to caption
    });

    await waitFor(() =>
      expect(ctx.get().toasts.some((t) => /Render chain failed/.test(t.title))).toBe(true),
    );
    expect(ctx.get().toasts.find((t) => /Render chain failed/.test(t.title))?.tone).toBe("warn");
    expect(client.caption).toHaveBeenCalledWith("c1", { style: "opus" });
    expect(client.render).not.toHaveBeenCalled();
  });

  it("existing path: a successful chain awaits caption then calls render with the preset", async () => {
    client = {
      caption: vi.fn().mockResolvedValue({ id: "cap1" }),
      render: vi.fn().mockResolvedValue({ id: "x1" }),
      getClipJob: vi.fn().mockResolvedValue({ id: "cap1", status: "done" }),
    };
    const ctx = mountCtx();

    act(() => {
      ctx.get().makeClipsFrom([{ id: "c1" }], { preset: "reels", style: "karaoke" });
    });

    await waitFor(() => expect(client.render).toHaveBeenCalledWith("c1", { preset: "reels" }));
    // caption ran with the chosen style, and the job was polled to terminal before render.
    expect(client.caption).toHaveBeenCalledWith("c1", { style: "karaoke" });
    expect(client.getClipJob).toHaveBeenCalledWith("cap1");
    expect(ctx.get().toasts.some((t) => /Render chain failed/.test(t.title))).toBe(false);
  });
});

describe("SpoolCtx exposes awaitClipJob for pages to sequence dependent jobs", () => {
  it("awaitClipJob is on the context and resolves when the job is already terminal", async () => {
    client = { getClipJob: vi.fn().mockResolvedValue({ id: "j", status: "done" }) };
    const ctx = mountCtx();
    expect(typeof ctx.get().awaitClipJob).toBe("function");
    await act(async () => { await ctx.get().awaitClipJob("j"); });
    expect(client.getClipJob).toHaveBeenCalledWith("j");
  });

  it("awaitClipJob is a no-op for an undefined id (no client call)", async () => {
    client = { getClipJob: vi.fn() };
    const ctx = mountCtx();
    await act(async () => { await ctx.get().awaitClipJob(undefined); });
    expect(client.getClipJob).not.toHaveBeenCalled();
  });
});
