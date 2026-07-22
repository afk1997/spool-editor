import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/",
}));
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
const { default: SettingsScreen } = await import("@/app/settings/page");
const { default: OnboardingScreen } = await import("@/app/onboarding/page");
const { Shell } = await import("@/components/spool/shell");
const { metadata } = await import("@/app/layout");
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
  const view = render(
    <SpoolProvider>
      <Probe />
    </SpoolProvider>,
  );
  return {
    get: () => ref.current!,
    rerender: () =>
      view.rerender(
        <SpoolProvider>
          <Probe />
        </SpoolProvider>,
      ),
  };
}

function renderWithProvider(children: React.ReactNode) {
  return render(<SpoolProvider>{children}</SpoolProvider>);
}

async function renderPrivacySettings(initial: EngineSettings) {
  settingsQuery = { data: initial, loading: false, reload: vi.fn() };
  const view = renderWithProvider(<SettingsScreen />);
  fireEvent.click(screen.getByRole("button", { name: "Privacy" }));
  await waitFor(() => expect(screen.getByText("Unavailable in Phase 0", { exact: true })).toBeInTheDocument());
  return view;
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
    expect(ctx.get().settings).toEqual({
      ...loaded,
      reasoning_provider: "none",
      reasoning_egress_consent: false,
    });
    expect(ctx.get().reasoningProvider).toBe("none");
    expect(ctx.get().reasoningEgressConsent).toBe(false);
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
      fast_default: false,
    });
    const stale = engineSettings({ offline: false });
    const refreshed = engineSettings({
      offline: false,
      default_preset: "reels",
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
    act(() => {
      result = ctx.get().updateSettings({ offline: true });
    });
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
    await act(async () => {
      await Promise.resolve();
    });
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
      await expect(ctx.get().updateSettings({ fast_default: false })).rejects.toBe(failed);
    });

    expect(ctx.get().settings).toEqual(initial);
    expect(ctx.get().settingsPending).toBe(false);
  });

  it.each([
    { reasoning_provider: "codex" as const },
    { reasoning_provider: "CoDeX" as unknown as ReasoningProvider },
    { reasoning_egress_consent: true },
  ])("rejects unsupported remote settings locally without issuing PATCH: $reasoning_provider$reasoning_egress_consent", async (patch) => {
    settingsQuery = { data: engineSettings(), loading: false, reload: vi.fn() };
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    await expect(ctx.get().updateSettings(patch)).rejects.toMatchObject({
      status: 409,
      code: "remote_reasoning_unavailable",
    });
    expect(client.updateSettings).not.toHaveBeenCalled();
  });

  it("serializes settings writes and publishes only confirmed responses", async () => {
    const initial = engineSettings();
    const firstResponse = engineSettings({ fast_default: false });
    const secondResponse = engineSettings({
      fast_default: false,
      default_preset: "reels",
    });
    const first = deferred<EngineSettings>();
    const second = deferred<EngineSettings>();
    settingsQuery = { data: initial, loading: false, reload: vi.fn() };
    client.updateSettings.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const ctx = mountContext();
    await waitFor(() => expect(ctx.get().settingsReady).toBe(true));

    let one!: Promise<EngineSettings>;
    let two!: Promise<EngineSettings>;
    act(() => {
      one = ctx.get().updateSettings({ fast_default: false });
      two = ctx.get().updateSettings({ default_preset: "reels" });
    });
    expect(client.updateSettings).toHaveBeenCalledTimes(1);
    expect(ctx.get().settings).toEqual(initial);

    await act(async () => {
      first.resolve(firstResponse);
      await one;
    });
    expect(client.updateSettings).toHaveBeenCalledTimes(2);
    expect(ctx.get().settings).toEqual(firstResponse);
    expect(ctx.get().settingsPending).toBe(true);

    await act(async () => {
      second.resolve(secondResponse);
      await two;
    });
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

    await act(async () => {
      write.resolve(canonical);
      await write.promise;
    });
    await waitFor(() => expect(ctx.get().offlinePending).toBe(false));
    expect(ctx.get().offline).toBe(true);
  });
});

describe("privacy settings UI", () => {
  it("keeps Offline disabled until settings load and never renders a remote selector or consent action", () => {
    renderWithProvider(<SettingsScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Privacy" }));

    expect(screen.getByRole("switch", { name: "Offline mode" })).toBeDisabled();
    expect(screen.getByText("None", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Unavailable in Phase 0", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Codex" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", {
        name: /Allow your message and any attached transcript text to leave/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("stays fail-closed when an old engine record still contains Codex and consent", async () => {
    await renderPrivacySettings(engineSettings({
      reasoning_provider: "codex",
      reasoning_egress_consent: true,
    }));

    expect(screen.getByText("None", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Unavailable in Phase 0", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Codex" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", {
        name: /Allow your message and any attached transcript text to leave/i,
      }),
    ).not.toBeInTheDocument();
    expect(client.updateSettings).not.toHaveBeenCalled();
  });
});

describe("canonical privacy status label", () => {
  it.each([
    {
      settings: engineSettings({ offline: true }),
      label: "Offline",
    },
    {
      settings: engineSettings({ reasoning_provider: "codex", reasoning_egress_consent: true }),
      label: "Fully local",
    },
    {
      settings: engineSettings(),
      label: "Fully local",
    },
  ])("renders exactly one $label label", async ({ settings, label }) => {
    settingsQuery = { data: settings, loading: false, reload: vi.fn() };
    renderWithProvider(
      <Shell>
        <p>Current page</p>
      </Shell>,
    );

    await waitFor(() => expect(screen.getAllByText(label, { exact: true })).toHaveLength(1));
    for (const other of ["Offline", "Remote reasoning enabled", "Fully local"].filter(
      (item) => item !== label,
    )) {
      expect(screen.queryByText(other, { exact: true })).not.toBeInTheDocument();
    }
  });

  it("does not claim local safety while privacy settings are loading or unavailable", async () => {
    const view = renderWithProvider(
      <Shell>
        <p>Current page</p>
      </Shell>,
    );
    expect(screen.getByText("Privacy status loading", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("Offline", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("Fully local", { exact: true })).not.toBeInTheDocument();

    settingsQuery = { error: "unreachable", loading: false, reload: vi.fn() };
    view.rerender(
      <SpoolProvider>
        <Shell>
          <p>Current page</p>
        </Shell>
      </SpoolProvider>,
    );
    await waitFor(() =>
      expect(screen.getByText("Privacy status unavailable", { exact: true })).toBeInTheDocument(),
    );
  });
});

describe("truthful privacy copy", () => {
  it("states that Phase 0 has no remote reasoning transport or egress", async () => {
    settingsQuery = { data: engineSettings(), loading: false, reload: vi.fn() };
    renderWithProvider(<SettingsScreen />);

    await waitFor(() =>
      expect(
        screen.getByText(
          "Remote moment-finding is unavailable in Phase 0.",
          { exact: true },
        ),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(
        "Spool has no supported zero-tool, zero-machine-context remote transport yet.",
        { exact: true },
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Privacy" }));
    expect(
      screen.getByText(
        "Remote reasoning stays disabled until Spool has a supported transport that sends no local tools or machine context.",
        { exact: true },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("unavailable · no egress", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText(/Codex/i)).not.toBeInTheDocument();
  });

  it("discloses network downloads and unavailable remote reasoning during onboarding", async () => {
    settingsQuery = {
      data: engineSettings({ reasoning_provider: "codex", reasoning_egress_consent: true }),
      loading: false,
      reload: vi.fn(),
    };
    renderWithProvider(<OnboardingScreen />);

    expect(screen.getByText(/URL downloads use the network/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        /Remote reasoning is unavailable in Phase 0 and sends nothing/i,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Codex/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Everything runs on your machine/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/entirely on your machine/i)).not.toBeInTheDocument();
  });

  it("keeps the product metadata local-first without claiming every operation stays local", () => {
    expect(metadata.description).toBe(
      "Local-first clip studio for platform-ready vertical clips with on-device transcription and rendering.",
    );
  });

  it("keeps remote reasoning unavailable even when an old record contains Codex consent", async () => {
    const offlineCodex = engineSettings({
      offline: true,
      reasoning_provider: "codex",
      reasoning_egress_consent: true,
    });
    const settingsView = await renderPrivacySettings(offlineCodex);

    expect(
      screen.getByText("unavailable · no egress", { exact: true }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Models" }));
    expect(screen.getByText("Unavailable", { exact: true })).toBeInTheDocument();
    settingsView.unmount();

    settingsQuery = { data: offlineCodex, loading: false, reload: vi.fn() };
    renderWithProvider(<OnboardingScreen />);
    fireEvent.click(screen.getByRole("button", { name: /Let.s set up/i }));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      screen.getByText("Remote reasoning unavailable in Phase 0 · no egress", {
        exact: true,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("unavailable", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("enabled", { exact: true })).not.toBeInTheDocument();
  });
});

describe("privacy action error copy", () => {
  const cases = {
    offline_network_disabled: "Turn off Offline mode before using this network action.",
    network_work_active: "Wait for active network work to finish before turning on Offline mode.",
    remote_reasoning_unavailable:
      "Remote reasoning is unavailable in Phase 0 until a supported zero-tool transport ships.",
    settings_persist_failed:
      "The engine could not save settings. Your confirmed settings were kept.",
  } as const;

  it.each(Object.entries(cases))("maps %s to actionable raw-code copy", (code, message) => {
    expect(describeActionError(new SpoolApiError(409, code, "raw"))).toEqual({ code, message });
  });
});
