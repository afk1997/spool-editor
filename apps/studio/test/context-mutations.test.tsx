import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import type { EventsSnapshot } from "@spool/types";
import type { EngineSettings } from "@spool/api-client";

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
let settingsQuery: { data?: EngineSettings; error?: string; loading: boolean; reload: ReturnType<typeof vi.fn> };

const engineSettings = (overrides: Partial<EngineSettings> = {}): EngineSettings => ({
  fast_default: true,
  default_preset: "tiktok",
  offline: false,
  reasoning_provider: "none",
  reasoning_egress_consent: false,
  clip_workers: 2,
  max_workers: 4,
  mcp_transport: "stdio",
  ...overrides,
});

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("@/lib/engine-context", () => ({
  useEngine: () => client,
  useLive: () => ({ snapshot: FIXED_SNAPSHOT, connection: "online" }),
  useEngineQuery: (query: (candidate: Record<string, () => string>) => unknown) => {
    const method = query(new Proxy({}, { get: (_target, key) => () => String(key) }));
    return method === "getSettings"
      ? settingsQuery
      : { data: undefined, loading: false, reload: () => {} };
  },
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
  window.history.replaceState({}, "", "/");
  settingsQuery = { data: engineSettings(), loading: false, reload: vi.fn() };
});

describe("makeClipsFrom surfaces mutation-chain failures (no silent fire-and-forget)", () => {
  it("fresh path: waits for the full batch, then reports exact success/failure counts", async () => {
    let release!: () => void;
    const delayed = new Promise<{ id: string }>((resolve) => {
      release = () => resolve({ id: "j1" });
    });
    client = {
      renderPipeline: vi
        .fn()
        .mockReturnValueOnce(delayed)
        .mockRejectedValueOnce(new Error("409")),
    };
    const ctx = mountCtx();
    let work!: Promise<void>;

    act(() => {
      work = ctx.get().makeClipsFrom([
        { source_id: "s", start: 0, end: 1 },
        { source_id: "s", start: 2, end: 3 },
      ]);
    });

    expect(ctx.get().toasts).toEqual([]);
    expect(router.push).not.toHaveBeenCalled();
    expect(client.renderPipeline).toHaveBeenCalledTimes(2);

    await act(async () => {
      release();
      await work;
    });
    const warn = ctx.get().toasts.find((t) => /1 clip started · 1 failed/.test(t.title));
    expect(warn).toBeTruthy();
    expect(warn?.tone).toBe("warn");
    expect(warn?.body).toMatch(/action_failed/);
    expect(router.push).toHaveBeenCalledWith("/sources/s?tab=Clips");
  });

  it("fresh path: reports success only after every start settles", async () => {
    client = { renderPipeline: vi.fn().mockResolvedValue({ id: "j1" }) };
    const ctx = mountCtx();

    await act(async () => {
      await ctx.get().makeClipsFrom([{ source_id: "s", start: 0, end: 1 }]);
    });

    expect(ctx.get().toasts.some((t) => /1 clip started · 0 failed/.test(t.title))).toBe(true);
    expect(ctx.get().toasts.find((t) => /1 clip started/.test(t.title))?.tone).toBe("ok");
    expect(router.push).toHaveBeenCalledWith("/sources/s?tab=Clips");
  });

  it("fresh path: never redirects after the initiating route is left", async () => {
    let release!: () => void;
    const delayed = new Promise<{ id: string }>((resolve) => {
      release = () => resolve({ id: "j1" });
    });
    client = { renderPipeline: vi.fn().mockReturnValue(delayed) };
    const ctx = mountCtx();
    let work!: Promise<void>;
    window.history.replaceState({}, "", "/sources/s");

    act(() => {
      work = ctx.get().makeClipsFrom([{ source_id: "s", start: 0, end: 1 }]);
    });
    window.history.pushState({}, "", "/library");
    await act(async () => { release(); await work; });

    expect(router.push).not.toHaveBeenCalled();
  });

  it("existing path: a caption rejection reports exact counts and never calls render", async () => {
    client = {
      reframe: vi.fn().mockResolvedValue({ id: "r1" }),
      caption: vi.fn().mockRejectedValue(new Error("boom")),
      render: vi.fn().mockResolvedValue({ id: "x1" }),
      getClipJob: vi.fn().mockResolvedValue({ id: "any", status: "done" }),
    };
    const ctx = mountCtx();

    await act(async () => {
      await ctx.get().makeClipsFrom([{ id: "c1" }]); // no aspect/mode → skips reframe, goes straight to caption
    });

    const warn = ctx.get().toasts.find((t) => /0 renders started · 1 failed/.test(t.title));
    expect(warn).toBeTruthy();
    expect(warn?.tone).toBe("warn");
    expect(warn?.body).toMatch(/action_failed/);
    expect(client.caption).toHaveBeenCalledWith("c1", { style: "opus" });
    expect(client.render).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
  });

  it("existing path: a successful chain awaits caption then calls render with the preset", async () => {
    client = {
      caption: vi.fn().mockResolvedValue({ id: "cap1" }),
      render: vi.fn().mockResolvedValue({ id: "x1" }),
      getClipJob: vi.fn().mockResolvedValue({ id: "cap1", status: "done" }),
    };
    const ctx = mountCtx();

    await act(async () => {
      await ctx.get().makeClipsFrom([{ id: "c1" }], { preset: "reels", style: "karaoke" });
    });

    // caption ran with the chosen style, and the job was polled to terminal before render.
    expect(client.caption).toHaveBeenCalledWith("c1", { style: "karaoke" });
    expect(client.getClipJob).toHaveBeenCalledWith("cap1");
    expect(client.render).toHaveBeenCalledWith("c1", { preset: "reels" });
    expect(ctx.get().toasts.some((t) => /Render chain failed/.test(t.title))).toBe(false);
    expect(ctx.get().toasts.some((t) => t.title === "1 render started · 0 failed" && t.tone === "ok")).toBe(true);
    expect(router.push).toHaveBeenCalledWith("/queue");
  });

  it("existing path: a terminal polling failure stops the chain and surfaces a visible warning", async () => {
    client = {
      caption: vi.fn().mockResolvedValue({ id: "cap1" }),
      render: vi.fn().mockResolvedValue({ id: "x1" }),
      getClipJob: vi.fn().mockResolvedValue({
        id: "cap1",
        status: "error",
        error_category: "caption_failed",
        error_message: "Caption worker exited.",
      }),
    };
    const ctx = mountCtx();

    await act(async () => {
      await ctx.get().makeClipsFrom([{ id: "c1" }], { preset: "reels", style: "karaoke" });
    });

    expect(client.caption).toHaveBeenCalledWith("c1", { style: "karaoke" });
    expect(client.getClipJob).toHaveBeenCalledWith("cap1");
    expect(client.render).not.toHaveBeenCalled();
    expect(ctx.get().toasts).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: "0 renders started · 1 failed",
        tone: "warn",
        body: "Caption worker exited. (caption_failed)",
      }),
    ]));
    expect(router.push).not.toHaveBeenCalled();
  });
});

describe("offline setting mutation", () => {
  it("single-flights repeated toggles and exposes a pending state until persistence settles", async () => {
    let release!: () => void;
    const delayed = new Promise<EngineSettings>((resolve) => {
      release = () => resolve(engineSettings({ offline: true }));
    });
    client = { updateSettings: vi.fn().mockReturnValue(delayed) };
    const ctx = mountCtx();

    act(() => {
      ctx.get().toggleOffline();
      ctx.get().toggleOffline();
    });

    expect(client.updateSettings).toHaveBeenCalledTimes(1);
    expect(client.updateSettings).toHaveBeenCalledWith({ offline: true });
    expect(ctx.get().offlinePending).toBe(true);

    await act(async () => {
      release();
      await delayed;
      await Promise.resolve();
    });

    expect(ctx.get().offlinePending).toBe(false);
  });
});

describe("agent Phase 0 remote-reasoning fuse", () => {
  it("fails locally without calling the Agent endpoint", async () => {
    const agentCall = vi.fn();
    client = { agent: agentCall };
    const ctx = mountCtx();

    act(() => { ctx.get().askAgent("inspect my library"); });

    expect(agentCall).not.toHaveBeenCalled();
    expect(ctx.get().working).toBe(false);
    expect(ctx.get().agentMessages.at(-1)?.text).toBe(
      "Remote reasoning is unavailable in Phase 0 until a supported zero-tool transport ships. (remote_reasoning_unavailable)",
    );
  });

  it("a stale confirmation card cannot trigger a second Agent turn", async () => {
    client = {
      getSettings: vi.fn().mockResolvedValue({ offline: false }),
      agent: vi.fn(),
    };
    const ctx = mountCtx();
    const stale = {
      role: "elicit" as const,
      id: "stale-confirm",
      kind: "confirm" as const,
      q: "Allow delete_recipe?",
      options: ["Confirm", "Cancel"],
      sourceId: "source-1",
      confirmFor: { text: "delete my recipe", tool: "delete_recipe" },
    };

    act(() => { ctx.get().answerElicit(stale, "yes"); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(client.agent).not.toHaveBeenCalled();
    expect(ctx.get().agentMessages.some((x) =>
      x.text === "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
    )).toBe(true);
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

  it("awaitClipJob propagates a getClipJob rejection", async () => {
    client = { getClipJob: vi.fn().mockRejectedValue(new Error("poll transport failed")) };
    const ctx = mountCtx();

    await expect(ctx.get().awaitClipJob("j-reject")).rejects.toThrow("poll transport failed");
    expect(client.getClipJob).toHaveBeenCalledTimes(1);
    expect(client.getClipJob).toHaveBeenCalledWith("j-reject");
  });

  it("awaitClipJob rejects with the engine's terminal error details", async () => {
    client = {
      getClipJob: vi.fn().mockResolvedValue({
        id: "j-error",
        status: "error",
        error_category: "encode_failed",
        error_message: "Encoder stopped.",
      }),
    };
    const ctx = mountCtx();

    await expect(ctx.get().awaitClipJob("j-error")).rejects.toMatchObject({
      code: "encode_failed",
      message: "Encoder stopped.",
    });
    expect(client.getClipJob).toHaveBeenCalledTimes(1);
  });

  it("awaitClipJob rejects a cancelled job instead of treating it as complete", async () => {
    client = { getClipJob: vi.fn().mockResolvedValue({ id: "j-cancel", status: "cancelled" }) };
    const ctx = mountCtx();

    await expect(ctx.get().awaitClipJob("j-cancel")).rejects.toMatchObject({
      code: "cancelled",
      message: "Clip job j-cancel was cancelled.",
    });
    expect(client.getClipJob).toHaveBeenCalledTimes(1);
  });

  it("awaitClipJob times out after its bounded polling window", async () => {
    vi.useFakeTimers();
    try {
      client = { getClipJob: vi.fn().mockResolvedValue({ id: "j-running", status: "running" }) };
      const ctx = mountCtx();
      const rejection = ctx.get().awaitClipJob("j-running").catch((error: unknown) => error);

      await vi.runAllTimersAsync();

      await expect(rejection).resolves.toMatchObject({
        code: "timeout",
        message: "Timed out waiting for clip job j-running.",
      });
      expect(client.getClipJob).toHaveBeenCalledTimes(600);
    } finally {
      vi.useRealTimers();
    }
  });
});
