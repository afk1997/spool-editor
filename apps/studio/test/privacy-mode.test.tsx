import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EngineSettings } from "@spool/api-client";
import { SpoolApiError } from "@spool/api-client";
import type { EventsSnapshot, ReasoningProvider } from "@spool/types";
import { describeActionError } from "@/lib/action-error";

const SNAPSHOT = { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot;
const router = { push: vi.fn() };

type QueryState = {
  data?: EngineSettings;
  error?: string;
  loading: boolean;
  reload: ReturnType<typeof vi.fn>;
  requestGeneration?: number;
  dataGeneration?: number;
  getRequestGeneration?: () => number;
};

let client: { updateSettings: ReturnType<typeof vi.fn> };
let settingsQuery: QueryState;

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("@/lib/engine-context", () => ({
  useEngine: () => client,
  useLive: () => ({ snapshot: SNAPSHOT, connection: "online" }),
  useEngineQuery: (query: (candidate: Record<string, () => string>) => unknown) => {
    const method = query(new Proxy({}, { get: (_target, key) => () => String(key) }));
    return method === "getSettings"
      ? settingsQuery
      : { data: undefined, error: undefined, loading: false, reload: vi.fn() };
  },
}));

const { SpoolProvider, useSpool } = await import("@/components/spool/context");
type Ctx = ReturnType<typeof useSpool>;

function engineSettings(overrides: Partial<EngineSettings> = {}): EngineSettings {
  const provider: ReasoningProvider = "none";
  return {
    fast_default: true,
    default_preset: "tiktok",
    offline: false,
    reasoning_provider: provider,
    reasoning_egress_consent: false,
    clip_workers: 2,
    max_workers: 4,
    mcp_transport: "stdio",
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function mountContext() {
  const ref: { current: Ctx | null } = { current: null };
  function Probe() {
    ref.current = useSpool();
    return null;
  }
  const view = render(<SpoolProvider><Probe /></SpoolProvider>);
  return { get: () => ref.current!, rerender: () => view.rerender(<SpoolProvider><Probe /></SpoolProvider>) };
}

beforeEach(() => {
  client = { updateSettings: vi.fn() };
  settingsQuery = { loading: true, reload: vi.fn() };
});

describe("authoritative privacy settings", () => {
  it("fails closed until the initial settings response becomes authoritative", async () => {
    const ctx = mountContext();

    expect(ctx.get().settings).toBeNull();
    expect(ctx.get().settingsReady).toBe(false);
    expect(ctx.get().settingsLoading).toBe(true);
    expect(ctx.get().settingsError).toBeNull();
    expect(ctx.get().reasoningProvider).toBeNull();
    expect(ctx.get().reasoningEgressConsent).toBe(false);
    expect(ctx.get().offline).toBe(true);

    const loaded = engineSettings({
      reasoning_provider: "codex",
      reasoning_egress_consent: true,
    });
    settingsQuery = { data: loaded, loading: false, reload: vi.fn() };
    ctx.rerender();

    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));
    expect(ctx.get().settings).toEqual(loaded);
    expect(ctx.get().reasoningProvider).toBe("codex");
    expect(ctx.get().reasoningEgressConsent).toBe(true);
    expect(ctx.get().offline).toBe(false);
  });

  it("keeps an initial settings error fail-closed and observable", () => {
    settingsQuery = { error: "unreachable", loading: false, reload: vi.fn() };
    const ctx = mountContext();

    expect(ctx.get().settings).toBeNull();
    expect(ctx.get().settingsReady).toBe(false);
    expect(ctx.get().settingsLoading).toBe(false);
    expect(ctx.get().settingsError).toBe("unreachable");
    expect(ctx.get().reasoningProvider).toBeNull();
    expect(ctx.get().reasoningEgressConsent).toBe(false);
    expect(ctx.get().offline).toBe(true);
  });

  it("keeps a PATCH response over an in-flight GET, then accepts a genuinely newer GET", async () => {
    const initial = engineSettings();
    const canonical = engineSettings({
      offline: true,
      reasoning_provider: "codex",
      reasoning_egress_consent: true,
    });
    const stale = engineSettings({ offline: false });
    const refreshed = engineSettings({
      offline: false,
      reasoning_provider: "codex",
      reasoning_egress_consent: false,
    });
    let startedGeneration = 1;
    const getRequestGeneration = () => startedGeneration;
    const write = deferred<EngineSettings>();
    settingsQuery = {
      data: initial,
      loading: false,
      reload: vi.fn(),
      requestGeneration: 1,
      dataGeneration: 1,
      getRequestGeneration,
    };
    client.updateSettings.mockReturnValue(write.promise);
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    let result!: Promise<EngineSettings>;
    act(() => { result = ctx.get().updateSettings({ offline: true }); });
    expect(ctx.get().settingsPending).toBe(true);

    // The query effect has started GET generation 2, but React has not rerendered the provider
    // with that generation yet. PATCH completion must still synchronously observe the live id.
    startedGeneration = 2;

    await act(async () => {
      write.resolve(canonical);
      await expect(result).resolves.toEqual(canonical);
    });
    expect(ctx.get().settings).toEqual(canonical);

    settingsQuery = {
      data: stale,
      loading: false,
      reload: vi.fn(),
      requestGeneration: 2,
      dataGeneration: 2,
      getRequestGeneration,
    };
    ctx.rerender();
    await act(async () => { await Promise.resolve(); });
    expect(ctx.get().settings).toEqual(canonical);
    expect(ctx.get().settingsPending).toBe(false);

    // A later reconnect/reload has a newer generation and becomes authoritative again.
    startedGeneration = 3;
    settingsQuery = {
      data: refreshed,
      loading: false,
      reload: vi.fn(),
      requestGeneration: 3,
      dataGeneration: 3,
      getRequestGeneration,
    };
    ctx.rerender();
    await waitFor(() => expect(ctx.get().settings).toEqual(refreshed));
  });

  it("retains the last confirmed settings when persistence fails", async () => {
    const initial = engineSettings();
    const failed = new SpoolApiError(500, "settings_persist_failed");
    settingsQuery = { data: initial, loading: false, reload: vi.fn() };
    client.updateSettings.mockRejectedValue(failed);
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    await act(async () => {
      await expect(ctx.get().updateSettings({ reasoning_provider: "codex" })).rejects.toBe(failed);
    });

    expect(ctx.get().settings).toEqual(initial);
    expect(ctx.get().settingsPending).toBe(false);
  });

  it("serializes settings writes and publishes only confirmed responses", async () => {
    const initial = engineSettings();
    const firstResponse = engineSettings({ reasoning_provider: "codex" });
    const secondResponse = engineSettings({
      reasoning_provider: "codex",
      reasoning_egress_consent: true,
    });
    const first = deferred<EngineSettings>();
    const second = deferred<EngineSettings>();
    settingsQuery = { data: initial, loading: false, reload: vi.fn() };
    client.updateSettings
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    let one!: Promise<EngineSettings>;
    let two!: Promise<EngineSettings>;
    act(() => {
      one = ctx.get().updateSettings({ reasoning_provider: "codex" });
      two = ctx.get().updateSettings({ reasoning_egress_consent: true });
    });
    expect(client.updateSettings).toHaveBeenCalledTimes(1);
    expect(ctx.get().settings).toEqual(initial);

    await act(async () => { first.resolve(firstResponse); await one; });
    expect(client.updateSettings).toHaveBeenCalledTimes(2);
    expect(ctx.get().settings).toEqual(firstResponse);
    expect(ctx.get().settingsPending).toBe(true);

    await act(async () => { second.resolve(secondResponse); await two; });
    expect(ctx.get().settings).toEqual(secondResponse);
    expect(ctx.get().settingsPending).toBe(false);
  });

  it("keeps repeated offline toggles single-flight and adopts the PATCH response", async () => {
    const initial = engineSettings();
    const canonical = engineSettings({ offline: true });
    const write = deferred<EngineSettings>();
    settingsQuery = { data: initial, loading: false, reload: vi.fn() };
    client.updateSettings.mockReturnValue(write.promise);
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    act(() => {
      ctx.get().toggleOffline();
      ctx.get().toggleOffline();
    });
    expect(client.updateSettings).toHaveBeenCalledTimes(1);
    expect(client.updateSettings).toHaveBeenCalledWith({ offline: true });
    expect(ctx.get().offlinePending).toBe(true);

    await act(async () => { write.resolve(canonical); await write.promise; });
    await waitFor(() => expect(ctx.get().offlinePending).toBe(false));
    expect(ctx.get().offline).toBe(true);
  });
});

describe("privacy action error copy", () => {
  const cases = {
    offline_network_disabled: "Turn off Offline mode before using this network action.",
    network_work_active: "Wait for active network work to finish before turning on Offline mode.",
    reasoning_provider_required: "Select Codex as the reasoning provider before using this action.",
    egress_consent_required: "Allow transcript text to be sent to Codex before using remote reasoning.",
    egress_consent_requires_codex: "Select Codex before granting remote-reasoning consent.",
    settings_persist_failed: "The engine could not save settings. Your confirmed settings were kept.",
  } as const;

  it.each(Object.entries(cases))("maps %s to actionable raw-code copy", (code, message) => {
    expect(describeActionError(new SpoolApiError(409, code, "raw"))).toEqual({ code, message });
  });
});
