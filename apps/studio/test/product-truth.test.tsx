import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import type { EventsSnapshot } from "@spool/types";
import { SpoolApiError, type EngineSettings } from "@spool/api-client";

const importHarness = vi.hoisted(() => ({
  ctx: null as null | Record<string, unknown>,
  search: "",
  pathname: "/import",
  params: {} as Record<string, string>,
  snapshot: { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot | null,
  router: {
    push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(),
    prefetch: vi.fn(), refresh: vi.fn(),
  },
  queryData: {} as Record<string, unknown>,
  queryReload: {} as Record<string, ReturnType<typeof vi.fn>>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(importHarness.search),
  useRouter: () => importHarness.router,
  usePathname: () => importHarness.pathname,
  useParams: () => importHarness.params,
}));

vi.mock("@/lib/engine-context", () => ({
  useLive: () => ({ snapshot: importHarness.snapshot, connection: "online" }),
  useEngine: () => importHarness.ctx?.client,
  useEngineQuery: (query: (client: Record<string, (...args: unknown[]) => unknown>) => unknown) => {
    const marker = "__spoolQueryKey";
    const probe = new Proxy<Record<string, (...args: unknown[]) => unknown>>({}, {
      get: (_target, property) => () => ({ [marker]: String(property) }),
    });
    const result = query(probe);
    const key = result && typeof result === "object" && marker in result
      ? String((result as Record<string, unknown>)[marker])
      : undefined;
    return {
      data: key ? importHarness.queryData[key] : undefined,
      loading: false,
      error: null,
      reload: key ? (importHarness.queryReload[key] ?? vi.fn()) : vi.fn(),
    };
  },
}));

vi.mock("@/components/spool/context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/spool/context")>();
  return { ...actual, useSpool: () => importHarness.ctx };
});

import {
  buildTranscript,
  mapClips,
  mapDownloads,
  mapJobs,
  mapSources,
  type SpoolClip,
  type SpoolJob,
  type SpoolSource,
} from "@/components/spool/context";
import { FutureScreen } from "@/components/spool/panels";
import { AgentPanel } from "@/components/spool/agent";
import { ClipCard, MediaCard } from "@/components/spool/cards";
import { CommandPalette } from "@/components/spool/overlays";
import { Shell } from "@/components/spool/shell";
import { actionError, describeActionError, formatActionError } from "@/lib/action-error";
import AnalyticsPage from "@/app/analytics/page";
import BrandScreen from "@/app/brand/page";
import CaptionScreen from "@/app/clips/[id]/caption/page";
import EditorScreen from "@/app/clips/[id]/page";
import ReframeScreen from "@/app/clips/[id]/reframe/page";
import ClipsScreen from "@/app/clips/page";
import ImportPage from "@/app/import/page";
import LibraryScreen from "@/app/library/page";
import QueueScreen from "@/app/queue/page";
import RecipesScreen from "@/app/recipes/page";
import SettingsScreen from "@/app/settings/page";
import ProjectScreen from "@/app/sources/[id]/page";
import WatchesScreen from "@/app/watches/page";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

const sourceFixture = (overrides: Partial<SpoolSource> = {}): SpoolSource => ({
  id: "source-1",
  title: "Test source",
  src: "youtube",
  dur: 90,
  status: "ready",
  prog: 100,
  clips: 0,
  kind: "1 speaker",
  channel: "youtube",
  res: "—",
  size: "10 MB",
  lang: "en",
  added: "—",
  transcriptId: "transcript-1",
  speakerCount: 1,
  ...overrides,
});

const clipFixture = (overrides: Partial<SpoolClip> = {}): SpoolClip => ({
  id: "clip-1",
  title: "Truthful clip title",
  src: "source-1",
  dur: 12,
  aspect: "9:16",
  style: "opus",
  platform: "tiktok",
  status: "ready",
  prog: 100,
  tags: [],
  ...overrides,
});

const jobFixture = (overrides: Partial<SpoolJob> = {}): SpoolJob => ({
  id: "job-1",
  type: "download",
  label: "Test job",
  src: "source-1",
  status: "running",
  prog: 25,
  stage: "downloading",
  eta: "1m",
  elapsed: "5s",
  domain: "download",
  ...overrides,
});

const clientFixture = (overrides: Record<string, unknown> = {}) => ({
  submitDownload: vi.fn().mockResolvedValue({ id: "download-1" }),
  renderFileUrl: vi.fn().mockReturnValue("https://files.example.test/render.mp4"),
  jobFileUrl: vi.fn().mockReturnValue("https://files.example.test/source.mp4"),
  clipArtifactUrl: vi.fn().mockReturnValue("https://files.example.test/clip.mp4"),
  ...overrides,
});

const settingsFixture = (overrides: Partial<EngineSettings> = {}): EngineSettings => ({
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

const baseCtx = (overrides: Record<string, unknown> = {}) => ({
  client: clientFixture(),
  sources: [],
  clips: [],
  jobs: [],
  downloads: [],
  deps: [],
  snapshot: importHarness.snapshot,
  nav: vi.fn(),
  agentOpen: false,
  openAgent: vi.fn(),
  toggleAgent: vi.fn(),
  closeAgent: vi.fn(),
  paletteOpen: false,
  openPalette: vi.fn(),
  closePalette: vi.fn(),
  shortcutsOpen: false,
  openShortcuts: vi.fn(),
  closeShortcuts: vi.fn(),
  agentMessages: [],
  working: false,
  askAgent: vi.fn(),
  answerElicit: vi.fn(),
  makeClipsFrom: vi.fn().mockResolvedValue(undefined),
  awaitClipJob: vi.fn().mockResolvedValue(undefined),
  toasts: [],
  pushToast: vi.fn(),
  settings: settingsFixture(),
  settingsReady: true,
  settingsLoading: false,
  settingsError: null,
  reasoningProvider: "none",
  reasoningEgressConsent: false,
  settingsPending: false,
  updateSettings: vi.fn().mockResolvedValue(settingsFixture()),
  offline: false,
  offlinePending: false,
  toggleOffline: vi.fn(),
  ...overrides,
});

const importClient = (ctxOverrides: Record<string, unknown> = {}) => {
  const submitDownload = vi.fn().mockResolvedValue({ id: "download-1" });
  const pushToast = vi.fn();
  importHarness.ctx = baseCtx({
    downloads: [],
    client: clientFixture({ submitDownload }),
    pushToast,
    nav: vi.fn(),
    ...ctxOverrides,
  });
  return { submitDownload, pushToast };
};

beforeEach(() => {
  importHarness.search = "";
  importHarness.pathname = "/import";
  importHarness.params = {};
  importHarness.snapshot = { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot;
  importHarness.queryData = {};
  importHarness.queryReload = {};
  window.history.replaceState({}, "", "/");
  Object.values(importHarness.router).forEach((method) => method.mockReset());
  importHarness.ctx = null;
});

describe("product truth: live view models", () => {
  it("does not turn unknown source metadata into a local talking-head file", () => {
    const snapshot = {
      ts: 0,
      jobs: [
        {
          id: "source-unknown",
          url: "https://media.example.test/watch/123",
          title: "Unknown source",
          status: "done",
          filename: "source.mp4",
          thumbnail: null,
          format_choice: "video",
          downloaded_bytes: 10,
          total_bytes: 10,
          speed_bps: 0,
          eta_seconds: 0,
          fragment_index: 0,
          fragment_count: 0,
          progress_pct: 100,
          elapsed_seconds: 1,
          auto_transcribe: false,
          error_category: null,
          error_message: null,
          human: { summary: "done", elapsed: "0:01", size: "10 B", speed: "—", eta: "—" },
        },
      ],
      transcripts: [],
      clips: [],
    } as unknown as EventsSnapshot;

    const [source] = mapSources(snapshot);
    expect(source).toBeDefined();
    expect(source).toMatchObject({
      src: "—",
      channel: "—",
      kind: "—",
      res: "—",
      lang: "—",
      added: "—",
      status: "downloaded",
    });
    expect(source!.fps).toBeUndefined();
    expect(source!.scenes).toBeUndefined();
    expect(source!.speakerCount).toBeUndefined();

    importHarness.ctx = baseCtx();
    const { container } = render(<MediaCard s={source!} onOpen={vi.fn()} />);
    expect(screen.queryByText("FILE")).not.toBeInTheDocument();
    expect(screen.queryByText("talking-head")).not.toBeInTheDocument();
    expect(container.querySelector(".thumb .tl")).toBeNull();
    expect(screen.getByText("downloaded")).toBeInTheDocument();
  });

  it("does not invent clip format metadata or call a failed clip queued", () => {
    const snapshot = {
      ts: 0,
      jobs: [],
      transcripts: [],
      clips: [
        {
          id: "cut-failed",
          kind: "cut",
          source_id: "source-1",
          clip_id: "clip-1",
          status: "error",
          progress_pct: 45,
          stage: null,
          elapsed_seconds: 1,
          params: {},
          result: { start: 0, end: 5 },
          error_category: "render",
          error_message: "failed",
          human: { summary: "failed", elapsed: "0:01" },
        },
      ],
    } as unknown as EventsSnapshot;

    const [clip] = mapClips(snapshot);
    expect(clip).toBeDefined();
    expect(clip).toMatchObject({
      aspect: undefined,
      style: undefined,
      platform: undefined,
      status: "error",
    });

    importHarness.ctx = baseCtx();
    const { container } = render(<ClipCard c={clip!} />);
    expect(screen.queryByText("9:16")).not.toBeInTheDocument();
    expect(screen.queryByText("opus")).not.toBeInTheDocument();
    expect(screen.queryByText("TikTok")).not.toBeInTheDocument();
    expect(container.querySelector(".thumb .tr")).toBeNull();
  });

  it("ignores dismissed clip failures without discarding successful artifact metadata", () => {
    const base = {
      source_id: "source-1",
      clip_id: "clip-1",
      progress_pct: 100,
      elapsed_seconds: 1,
      stage: null,
      error_category: null,
      error_message: null,
      human: { summary: "done", elapsed: "0:01" },
    };
    const snapshot = {
      ts: 2,
      jobs: [],
      transcripts: [],
      clips: [
        {
          ...base,
          id: "cut-done",
          kind: "cut",
          status: "done",
          params: {},
          result: { start: 2, end: 9 },
        },
        {
          ...base,
          id: "reframe-done",
          kind: "reframe",
          status: "done",
          dismissed: true,
          params: { aspect: "4:5" },
          result: { aspect: "4:5" },
        },
        {
          ...base,
          id: "caption-done",
          kind: "caption",
          status: "done",
          dismissed: true,
          params: { style: "minimal" },
          result: { style: "minimal" },
        },
        {
          ...base,
          id: "export-failed",
          kind: "export",
          status: "error",
          dismissed: true,
          progress_pct: 40,
          params: { preset: "tiktok" },
          result: {},
          error_category: "encode_failed",
          error_message: "Encoder stopped.",
        },
      ],
    } as unknown as EventsSnapshot;

    expect(mapClips(snapshot)[0]).toMatchObject({
      id: "clip-1",
      status: "ready",
      dur: 7,
      start: 2,
      end: 9,
      aspect: "4:5",
      style: "minimal",
    });
  });

  it.each(["queued", "done", "error"])(
    "keeps %s preview reframe jobs out of canonical clip truth",
    (previewStatus) => {
      const base = {
        source_id: "source-1",
        clip_id: "clip-1",
        progress_pct: 100,
        elapsed_seconds: 1,
        stage: null,
        error_category: null,
        error_message: null,
        human: { summary: "done", elapsed: "0:01" },
      };
      const snapshot = {
        ts: 4,
        jobs: [],
        transcripts: [],
        clips: [
          {
            ...base,
            id: "cut-done",
            kind: "cut",
            status: "done",
            params: {},
            result: { start: 0, end: 10 },
          },
          {
            ...base,
            id: "canonical-reframe",
            kind: "reframe",
            status: "done",
            params: { aspect: "9:16" },
            result: { aspect: "9:16" },
          },
          {
            ...base,
            id: "throwaway-preview",
            kind: "reframe",
            status: previewStatus,
            progress_pct: previewStatus === "queued" ? 0 : 100,
            params: { aspect: "1:1", preview: true },
            result: previewStatus === "done" ? { aspect: "1:1" } : {},
            error_category: previewStatus === "error" ? "preview_failed" : null,
            error_message: previewStatus === "error" ? "Preview failed." : null,
          },
        ],
      } as unknown as EventsSnapshot;

      expect(mapClips(snapshot)[0]).toMatchObject({
        id: "clip-1",
        aspect: "9:16",
        status: "ready",
      });
    },
  );

  it("does not claim a stop-after-reframe pipeline has caption styling", () => {
    const snapshot = {
      ts: 2,
      jobs: [],
      transcripts: [],
      clips: [
        {
          id: "pipeline-review",
          kind: "pipeline",
          source_id: "source-1",
          clip_id: "clip-1",
          status: "done",
          progress_pct: 100,
          stage: "reframe",
          elapsed_seconds: 1,
          params: { aspect: "9:16", style: "opus", stop_after: "reframe" },
          result: { start: 0, end: 10, aspect: "9:16" },
          error_category: null,
          error_message: null,
          human: { summary: "done", elapsed: "0:01" },
        },
      ],
    } as unknown as EventsSnapshot;

    expect(mapClips(snapshot)[0]).toMatchObject({
      id: "clip-1",
      aspect: "9:16",
      style: undefined,
    });
  });

  it("labels missing diarization as unknown instead of inventing Speaker A", () => {
    const transcript = buildTranscript([
      { idx: 0, w: "Hello", start: 0, end: 0.4 },
      { idx: 1, w: "world", start: 0.5, end: 0.9 },
    ]);

    expect(transcript.lines[0]?.sp).toBe("unknown");
    expect(transcript.speakers.unknown?.name).toBe("Unknown speaker");
    expect(transcript.speakers.A).toBeUndefined();
  });

  it("preserves queued, paused, and cancelled download states", () => {
    const job = {
      id: "download",
      url: "https://example.test/video",
      title: "Video",
      filename: null,
      thumbnail: null,
      format_choice: "video",
      downloaded_bytes: 0,
      total_bytes: 0,
      speed_bps: null,
      eta_seconds: null,
      fragment_index: null,
      fragment_count: null,
      progress_pct: 0,
      elapsed_seconds: 0,
      auto_transcribe: false,
      error_category: null,
      error_message: null,
      human: { summary: "waiting" },
    };
    const snapshot = (statuses: string[]) => ({
      ts: 0,
      jobs: statuses.map((status, index) => ({ ...job, id: `${job.id}-${index}`, status })),
      transcripts: [],
      clips: [],
    }) as unknown as EventsSnapshot;

    expect(mapDownloads(snapshot(["queued", "paused", "cancelled"])).map((item) => item.status).sort()).toEqual([
      "cancelled",
      "paused",
      "queued",
    ]);
  });

  it("uses the newest transcript attempt in snapshot order", () => {
    const job = {
      id: "source-retry", url: "https://media.example.test/retry", title: "Retry source",
      status: "done", filename: "source.mp4", downloaded_bytes: 10, total_bytes: 10,
      progress_pct: 100, elapsed_seconds: 5, auto_transcribe: true,
      human: { summary: "done", elapsed: "5s", size: "10 B" },
    };
    const transcript = {
      parent_job_id: job.id, progress_pct: 100, duration_seconds: 40, speaker_count: 1,
      error_category: null, error_message: null, human: { summary: "transcript" },
    };
    const snapshot = {
      ts: 2,
      jobs: [job],
      transcripts: [
        { ...transcript, id: "old-attempt", status: "done", elapsed_seconds: 100, language_detected: "en" },
        { ...transcript, id: "new-attempt", status: "queued", elapsed_seconds: 1, progress_pct: 0, language_detected: "hi" },
      ],
      clips: [],
    } as unknown as EventsSnapshot;

    expect(mapSources(snapshot)[0]).toMatchObject({
      status: "transcribing",
      lang: "en",
      transcriptId: "old-attempt",
    });
  });

  it("retains the newest successful transcript after a later attempt fails", () => {
    const job = {
      id: "source-retry", url: "https://media.example.test/retry", title: "Retry source",
      status: "done", filename: "source.mp4", downloaded_bytes: 10, total_bytes: 10,
      progress_pct: 100, elapsed_seconds: 5, auto_transcribe: true,
      human: { summary: "done", elapsed: "5s", size: "10 B" },
    };
    const transcript = {
      parent_job_id: job.id, progress_pct: 100, duration_seconds: 40, speaker_count: 1,
      error_category: null, error_message: null, human: { summary: "transcript" },
    };
    const snapshot = {
      ts: 3,
      jobs: [job],
      transcripts: [
        { ...transcript, id: "successful-attempt", status: "done", elapsed_seconds: 10, language_detected: "en" },
        { ...transcript, id: "failed-attempt", status: "error", elapsed_seconds: 1, progress_pct: 20, language_detected: null, error_category: "whisper_failed" },
      ],
      clips: [],
    } as unknown as EventsSnapshot;

    expect(mapSources(snapshot)[0]).toMatchObject({
      status: "ready",
      lang: "en",
      transcriptId: "successful-attempt",
      speakerCount: 1,
    });
  });

  it("keeps cancelled jobs visible and labels moment discovery as analysis", () => {
    const download = {
      id: "download-cancelled", url: "https://media.example.test/video", title: "Cancelled download",
      status: "cancelled", filename: null, downloaded_bytes: 2, total_bytes: 10,
      progress_pct: 20, elapsed_seconds: 3, auto_transcribe: false,
      error_message: "Cancelled by user", error_category: "cancelled",
      human: { summary: "cancelled", elapsed: "3s" },
    };
    const clip = {
      id: "clip-cancelled", kind: "export", source_id: "source-1", clip_id: "clip-1",
      status: "cancelled", progress_pct: 30, stage: "cancelled", elapsed_seconds: 2,
      params: {}, result: {}, error_category: "cancelled", error_message: "Cancelled by user",
      human: { summary: "cancelled", elapsed: "2s" },
    };
    const moments = {
      ...clip,
      id: "moments-running",
      kind: "moments",
      clip_id: null,
      status: "running",
      stage: "ranking moments",
      error_category: null,
      error_message: null,
    };
    const jobs = mapJobs({ ts: 3, jobs: [download], transcripts: [], clips: [clip, moments] } as unknown as EventsSnapshot);

    expect(jobs.find((job) => job.id === download.id)).toMatchObject({
      type: "download",
      status: "cancelled",
      stage: "Cancelled by user",
    });
    expect(jobs.find((job) => job.id === clip.id)).toMatchObject({
      type: "render",
      status: "cancelled",
      stage: "Cancelled by user",
      errorCode: "cancelled",
      errorMessage: "Cancelled by user",
    });
    expect(jobs.find((job) => job.id === moments.id)).toMatchObject({
      type: "analysis",
      status: "running",
      stage: "ranking moments",
    });
  });

  it("keeps terminal transcript failures and the real clip error details in Queue", () => {
    const failedTranscript = {
      id: "transcript-failed", parent_job_id: "source-1", status: "error",
      progress_pct: 72, elapsed_seconds: 4, error_category: "whisper_failed",
      error_message: "Whisper worker exited", human: { summary: "transcribing", elapsed: "4s" },
    };
    const cancelledTranscript = {
      ...failedTranscript, id: "transcript-cancelled", status: "cancelled",
      error_category: "cancelled", error_message: "Cancelled by user",
    };
    const failedClip = {
      id: "clip-failed", kind: "export", source_id: "source-1", clip_id: "clip-1",
      status: "error", progress_pct: 80, stage: "export", elapsed_seconds: 7,
      params: {}, result: {}, error_category: "ffmpeg_failed", error_message: "Encoder exited",
      human: { summary: "export", elapsed: "7s" },
    };
    const jobs = mapJobs({
      ts: 4, jobs: [], transcripts: [failedTranscript, cancelledTranscript], clips: [failedClip],
    } as unknown as EventsSnapshot);

    expect(jobs.find((job) => job.id === failedTranscript.id)).toMatchObject({
      type: "transcribe", status: "failed", stage: "Whisper worker exited",
      errorCode: "whisper_failed", errorMessage: "Whisper worker exited", err: true,
    });
    expect(jobs.find((job) => job.id === cancelledTranscript.id)).toMatchObject({
      type: "transcribe", status: "cancelled", stage: "Cancelled by user",
      errorCode: "cancelled", errorMessage: "Cancelled by user",
    });
    expect(jobs.find((job) => job.id === failedClip.id)).toMatchObject({
      type: "render", status: "failed", stage: "Encoder exited",
      errorCode: "ffmpeg_failed", errorMessage: "Encoder exited", err: true,
    });
  });
});

describe("product truth: visible control inventory", () => {
  it("renders only implemented shell destinations and opens real controls", () => {
    const openPalette = vi.fn();
    const openShortcuts = vi.fn();
    importHarness.pathname = "/";
    importHarness.ctx = baseCtx({ openPalette, openShortcuts });
    render(<Shell><p>Current page</p></Shell>);

    for (const name of ["Home", "Import", "Library", "Clips", "Queue", "Settings"])
      expect(screen.getByRole("link", { name })).toBeInTheDocument();
    for (const hidden of ["Recipes", "Watches", "Publish", "Analyze", "Analytics"])
      expect(screen.queryByRole("link", { name: hidden })).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Search sources, clips, and transcripts…"));
    fireEvent.click(screen.getByRole("button", { name: "Keyboard shortcuts" }));
    expect(openPalette).toHaveBeenCalledTimes(1);
    expect(openShortcuts).toHaveBeenCalledTimes(1);
  });

  it("renders a navigation-only command palette and routes a clicked result", () => {
    const nav = vi.fn();
    const closePalette = vi.fn();
    importHarness.ctx = baseCtx({ paletteOpen: true, nav, closePalette });
    render(<CommandPalette />);

    expect(screen.getByPlaceholderText("Search pages, sources, and transcripts…")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Brand Kit"));
    expect(nav).toHaveBeenCalledWith("brand");
    expect(closePalette).toHaveBeenCalledTimes(1);
    for (const unavailable of ["Recipes", "Watches", "Analytics", "Publish", "Analyze"])
      expect(screen.queryByText(unavailable)).not.toBeInTheDocument();
    for (const unavailable of ["Undo", "Redo", "Play / pause"])
      expect(screen.queryByRole("button", { name: unavailable })).not.toBeInTheDocument();
  });

  it("renders analytics as explicitly unavailable without fabricated metrics", () => {
    const { container } = render(<AnalyticsPage />);

    expect(screen.getByText("Analytics unavailable")).toBeInTheDocument();
    expect(screen.getByText(/does not collect or display performance analytics/i)).toBeInTheDocument();
    for (const fabricated of ["128.4k", "41%", "3,201", "+18%"])
      expect(screen.queryByText(fabricated)).not.toBeInTheDocument();
    expect(container.querySelector("polyline")).toBeNull();
  });

  it("describes MCP mutation schemas as fused off instead of allowed", () => {
    importHarness.queryData = {
      doctor: { tools: {}, machine: {}, encoders: [] },
      getSettings: { mcp_transport: "stdio" },
      listModels: { models: [], active: "" },
    };
    importHarness.ctx = baseCtx({
      client: clientFixture({ updateSettings: vi.fn(), getSettings: vi.fn() }),
    });
    render(<SettingsScreen />);
    fireEvent.click(screen.getByRole("button", { name: "MCP server" }));

    expect(screen.getByText("Mutation schemas · writes disabled")).toBeInTheDocument();
    expect(screen.getByText(/agent_mutation_disabled/)).toBeInTheDocument();
    expect(screen.queryByText("Tool allow-list")).not.toBeInTheDocument();
    expect(screen.queryByText(/manual mode and agent mode never diverge/i)).not.toBeInTheDocument();
  });

  it("renders the agent as inspection-only and submits a plain-language query", () => {
    const askAgent = vi.fn();
    importHarness.ctx = baseCtx({
      agentOpen: true,
      askAgent,
      agentMessages: [{ role: "agent", text: "I can inspect current engine state." }],
    });
    render(<AgentPanel />);

    expect(screen.getByText("Agent · read-only")).toBeInTheDocument();
    expect(screen.getByText("Inspection only")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("Ask about sources, clips, transcripts, or queue status…");
    fireEvent.change(input, { target: { value: "Which jobs are queued?" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(askAgent).toHaveBeenCalledWith("Which jobs are queued?");
    expect(input).toHaveValue("");
    for (const fake of ["Undo last agent action", "Detected 2 speakers", "Run recipe"])
      expect(screen.queryByText(fake)).not.toBeInTheDocument();
  });

  it("renders only the supported URL import and format controls", () => {
    importClient();
    render(<ImportPage />);

    expect(screen.getByText("Paste URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Video" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Audio" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.queryByText(/^Files$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/drop files/i)).not.toBeInTheDocument();
    for (const unavailable of ["Best", "1080p", "720p", "Retry"])
      expect(screen.queryByRole("button", { name: unavailable })).not.toBeInTheDocument();
    expect(screen.queryByText(/browser cookies/i)).not.toBeInTheDocument();
  });

  it("exports a selected rendered clip and hides unsupported collections", () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const renderFileUrl = vi.fn().mockReturnValue("https://files.example.test/clip-1.mp4");
    const clip = clipFixture({ renderId: "render-1", score: undefined });
    importHarness.ctx = baseCtx({ clips: [clip], client: { renderFileUrl } });
    const { container } = render(<ClipsScreen />);

    expect(screen.queryByRole("button", { name: "Best (85+)" })).not.toBeInTheDocument();
    expect(screen.queryByText("This week")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    fireEvent.click(container.querySelector(".checkbox")!);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(renderFileUrl).toHaveBeenCalledWith("clip-1", "render-1");
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
  });

  it("offers the Best collection only when real score data exists", () => {
    importHarness.ctx = baseCtx({ clips: [clipFixture({ score: 91 })] });
    render(<ClipsScreen />);
    expect(screen.getByRole("button", { name: "Best (85+)" })).toBeInTheDocument();
  });

  it("omits unknown origin and duration glyphs in the rendered Library table", () => {
    const source = sourceFixture({
      src: "—", channel: "—", kind: "—", dur: 0, status: "downloaded",
      transcriptId: undefined, speakerCount: undefined,
    });
    importHarness.ctx = baseCtx({ sources: [source] });
    const { container } = render(<LibraryScreen />);

    const viewSegment = container.querySelectorAll<HTMLElement>(".seg")[1]!;
    const gridButton = within(viewSegment).getByRole("button", { name: "Grid view" });
    const tableButton = within(viewSegment).getByRole("button", { name: "Table view" });
    expect(gridButton).toHaveAttribute("aria-pressed", "true");
    expect(tableButton).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(tableButton);
    expect(tableButton).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("Test source")).toBeInTheDocument();
    expect(screen.queryByText("FILE")).not.toBeInTheDocument();
    expect(screen.queryByText("0:00")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders truthful recovery actions for missing source and clip IDs", () => {
    const nav = vi.fn();
    importHarness.params = { id: "missing-source" };
    importHarness.ctx = baseCtx({ nav, sources: [], clips: [] });
    const sourceView = render(<ProjectScreen />);

    expect(screen.getByText("Source unavailable")).toBeInTheDocument();
    expect(screen.getByText(/import has not completed/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import a video" }));
    expect(nav).toHaveBeenCalledWith("import");
    sourceView.unmount();

    nav.mockClear();
    importHarness.params = { id: "missing-clip" };
    importHarness.ctx = baseCtx({ nav, sources: [], clips: [] });
    render(<EditorScreen />);
    expect(screen.getByText("Clip not found")).toBeInTheDocument();
    expect(screen.getByText(/render may still be incomplete/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to clips" }));
    expect(nav).toHaveBeenCalledWith("clips");
    expect(screen.queryByText(/cleared from the working set/i)).not.toBeInTheDocument();
  });

  it.each([
    { name: "Caption", Screen: CaptionScreen, mutation: "caption" },
    { name: "Reframe", Screen: ReframeScreen, mutation: "reframe" },
  ])("$name gates an unavailable clip ID and performs no mutation", ({ Screen, mutation }) => {
    const mutate = vi.fn();
    const nav = vi.fn();
    importHarness.params = { id: "missing-clip" };
    importHarness.ctx = baseCtx({
      clips: [],
      snapshot: importHarness.snapshot,
      client: clientFixture({ [mutation]: mutate }),
      nav,
    });

    render(<Screen />);

    expect(screen.getByText("Clip not found")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to clips" }));
    expect(nav).toHaveBeenCalledWith("clips");
    expect(mutate).not.toHaveBeenCalled();
  });

  it.each([
    { name: "Caption", Screen: CaptionScreen },
    { name: "Reframe", Screen: ReframeScreen },
  ])("$name shows loading while the clip snapshot is unresolved", ({ Screen }) => {
    importHarness.params = { id: "pending-clip" };
    importHarness.ctx = baseCtx({ clips: [], snapshot: null });

    render(<Screen />);

    expect(screen.getByText("Loading clip…")).toBeInTheDocument();
    expect(screen.queryByText("Clip not found")).not.toBeInTheDocument();
  });

  it("Reframe ignores a completed throwaway preview when selecting canonical track history", () => {
    const clipArtifactUrl = vi.fn((_id: string, name: string) => `https://files.example.test/${name}.mp4`);
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      client: clientFixture({ clipArtifactUrl }),
      snapshot: {
        ts: 3,
        jobs: [],
        transcripts: [],
        clips: [
          {
            id: "canonical-reframe",
            kind: "reframe",
            clip_id: "clip-1",
            source_id: "source-1",
            status: "done",
            params: { aspect: "9:16" },
            result: {
              source: "fused",
              segments: [{ start: 0, end: 4, speaker: "left" }],
            },
          },
          {
            id: "throwaway-preview",
            kind: "reframe",
            clip_id: "clip-1",
            source_id: "source-1",
            status: "done",
            params: { aspect: "1:1", preview: true },
            result: {
              source: "manual",
              segments: [{ start: 0, end: 4, speaker: "right" }],
            },
          },
        ],
      } as unknown as EventsSnapshot,
    });

    const view = render(<ReframeScreen />);

    expect(screen.getByTitle(/left ·/i)).toBeInTheDocument();
    expect(screen.queryByTitle(/right ·/i)).not.toBeInTheDocument();
    const renderedPreview = Array.from(view.container.querySelectorAll("video"))
      .find((video) => video.src.includes("reframed.mp4"));
    expect(renderedPreview?.src).toContain("v=canonical-reframe");
    expect(renderedPreview?.src).not.toContain("throwaway-preview");
  });

  it("does not leave unavailable future controls in the accessibility tree", () => {
    render(
      <FutureScreen code="Future" phase="4" icon="chart" title="Future" desc="Unavailable">
        <button type="button">Fabricated action</button>
      </FutureScreen>,
    );

    expect(screen.queryByRole("button", { name: "Fabricated action" })).not.toBeInTheDocument();
  });
});

describe("product truth: structured action errors", () => {
  const known = {
    queue_full: "The work queue is full. Wait for a job to finish, then try again.",
    invalid_url: "Enter a valid HTTP or HTTPS URL.",
    origin_forbidden: "That source is blocked by the engine's origin policy.",
    agent_mutation_disabled: "Agent changes are disabled until the Phase 4 approval and undo contract ships.",
    offline_network_disabled: "Turn off Offline mode before using this network action.",
    network_work_active: "Wait for active network work to finish before turning on Offline mode.",
    reasoning_provider_required: "Select Codex as the reasoning provider before using this action.",
    egress_consent_required: "Allow transcript text to be sent to Codex before using remote reasoning.",
    egress_consent_requires_codex: "Select Codex before granting remote-reasoning consent.",
    settings_persist_failed: "The engine could not save settings. Your confirmed settings were kept.",
    not_resumable: "This job cannot be resumed. Start it again instead.",
    timeout: "The engine took too long to respond. Try again.",
    unreachable: "The engine is unreachable. Make sure it is running, then try again.",
  } as const;

  it.each(Object.entries(known))("maps %s to exact actionable copy and retains its code", (code, message) => {
    const error = new SpoolApiError(409, code, "raw engine detail");
    expect(describeActionError(error)).toEqual({ code, message });
    expect(formatActionError(error)).toBe(`${message} (${code})`);
    expect(actionError(error)).toEqual({ code, rawCode: code, message });
  });

  it("preserves structured unknown diagnostics and classifies ordinary versus network failures", () => {
    expect(describeActionError({ code: "codec_failed", message: "Codec exited." })).toEqual({
      code: "codec_failed",
      message: "Codec exited.",
    });
    expect(describeActionError(new Error("Could not save the record."))).toEqual({
      code: "action_failed",
      message: "Could not save the record.",
    });
    expect(describeActionError("opaque failure", "Try the action again.")).toEqual({
      code: "action_failed",
      message: "Try the action again.",
    });
    expect(describeActionError(new TypeError("Failed to fetch"))).toEqual({
      code: "unreachable",
      message: known.unreachable,
    });
  });
});

describe("product truth: URL import", () => {
  it.each([
    {
      state: "while privacy settings are loading",
      ctx: { settings: null, settingsReady: false, settingsLoading: true, settingsError: null },
      reason: /checking privacy settings/i,
    },
    {
      state: "when privacy settings are unavailable",
      ctx: { settings: null, settingsReady: false, settingsLoading: false, settingsError: "unreachable" },
      reason: /privacy settings are unavailable/i,
    },
    {
      state: "in Offline mode",
      ctx: { settings: settingsFixture({ offline: true }), settingsReady: true, offline: true },
      reason: /offline mode blocks network downloads and resumes/i,
    },
  ])("disables URL submission $state without disabling typing or local controls", ({ ctx, reason }) => {
    const { submitDownload } = importClient(ctx);
    render(<ImportPage />);

    const input = screen.getByRole("textbox");
    const download = screen.getByRole("button", { name: "Download" });
    expect(input).toBeEnabled();
    fireEvent.change(input, { target: { value: "https://media.example.test/watch/1" } });
    expect(input).toHaveValue("https://media.example.test/watch/1");
    expect(screen.getByRole("button", { name: "Audio" })).toBeEnabled();
    expect(screen.getByRole("switch", { name: "Download subtitles if available" })).toBeEnabled();
    expect(download).toBeDisabled();
    fireEvent.click(download);

    expect(submitDownload).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(reason);
  });

  it.each([
    {
      state: "while privacy settings are unavailable",
      ctx: { settings: null, settingsReady: false, settingsLoading: false, settingsError: "unreachable" },
    },
    {
      state: "in Offline mode",
      ctx: { settings: settingsFixture({ offline: true }), settingsReady: true, offline: true },
    },
  ])("blocks paused-download resume $state while keeping pause usable", ({ ctx }) => {
    const pauseJob = vi.fn().mockResolvedValue(undefined);
    const resumeJob = vi.fn().mockResolvedValue(undefined);
    importHarness.ctx = baseCtx({
      ...ctx,
      downloads: [
        {
          id: "paused-download", title: "Paused download", src: "youtube", prog: 20,
          status: "paused", size: "10 MB", speed: "—", eta: "—",
        },
        {
          id: "active-download", title: "Active download", src: "youtube", prog: 40,
          status: "downloading", size: "20 MB", speed: "1 MB/s", eta: "10s",
        },
      ],
      client: clientFixture({ pauseJob, resumeJob }),
    });
    render(<ImportPage />);

    const resume = screen.getByRole("button", { name: "Resume download" });
    const pause = screen.getByRole("button", { name: "Pause download" });
    expect(resume).toBeDisabled();
    fireEvent.click(resume);
    expect(resumeJob).not.toHaveBeenCalled();

    expect(pause).toBeEnabled();
    fireEvent.click(pause);
    expect(pauseJob).toHaveBeenCalledTimes(1);
  });

  it("prevalidates the complete batch and submits nothing when one URL is invalid", async () => {
    const { submitDownload, pushToast } = importClient();
    render(<ImportPage />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, {
      target: { value: "https://media.example.test/watch/1 ftp://media.example.test/watch/2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => expect(screen.getByText(/invalid url/i)).toBeInTheDocument());
    expect(submitDownload).not.toHaveBeenCalled();
    expect(input).toHaveValue("https://media.example.test/watch/1 ftp://media.example.test/watch/2");
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("preserves the URL and renders the structured code when submission rejects", async () => {
    const { submitDownload, pushToast } = importClient();
    submitDownload.mockRejectedValue(new SpoolApiError(429, "queue_full", "capacity reached"));
    render(<ImportPage />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "https://media.example.test/watch/1" } });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => expect(screen.getByText(/queue_full/i)).toBeInTheDocument());
    expect(input).toHaveValue("https://media.example.test/watch/1");
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("single-flights same-tick URL batch submissions", async () => {
    const delayed = deferred<{ id: string }>();
    const { submitDownload } = importClient();
    submitDownload.mockReturnValue(delayed.promise);
    render(<ImportPage />);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "https://media.example.test/watch/1" },
    });
    const download = screen.getByRole("button", { name: "Download" });

    act(() => {
      download.click();
      download.click();
    });

    expect(submitDownload).toHaveBeenCalledTimes(1);

    await act(async () => {
      delayed.resolve({ id: "download-1" });
      await delayed.promise;
    });
  });

  it("keeps the URL and withholds success until every submission settles", async () => {
    const first = deferred<{ id: string }>();
    const second = deferred<{ id: string }>();
    const { submitDownload, pushToast } = importClient();
    submitDownload.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<ImportPage />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, {
      target: { value: "https://media.example.test/one https://media.example.test/two" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(input).toHaveValue("https://media.example.test/one https://media.example.test/two");
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => { first.resolve({ id: "download-1" }); await Promise.resolve(); });
    expect(input).toHaveValue("https://media.example.test/one https://media.example.test/two");
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => { second.resolve({ id: "download-2" }); await second.promise; });
    await waitFor(() => expect(input).toHaveValue(""));
    expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "2 succeeded, 0 failed",
      body: "Every download was accepted. Progress appears below and in the queue.",
    }));
  });
});

describe("product truth: visible mutations settle before success", () => {
  it("Library waits for the whole batch, reports exact counts, and retains failed selections", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const startTranscribe = vi.fn()
      .mockReturnValueOnce(delayed.promise)
      .mockRejectedValueOnce(new SpoolApiError(429, "queue_full", "capacity"));
    importHarness.ctx = baseCtx({
      sources: [sourceFixture({ id: "source-1" }), sourceFixture({ id: "source-2", title: "Second source" })],
      client: clientFixture({ startTranscribe }),
      pushToast,
    });
    const { container } = render(<LibraryScreen />);
    container.querySelectorAll(".checkbox").forEach((checkbox) => fireEvent.click(checkbox));
    fireEvent.click(screen.getByRole("button", { name: "Transcribe" }));

    expect(startTranscribe).toHaveBeenCalledTimes(2);
    expect(pushToast).not.toHaveBeenCalled();
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await act(async () => { delayed.resolve({ id: "transcript-1" }); await delayed.promise; });
    await waitFor(() => expect(pushToast).toHaveBeenCalledTimes(1));
    expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Transcribe requests settled",
      body: expect.stringMatching(/^1 succeeded · 1 failed · queue_full:/),
    }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("Queue batch cleanup emits no result until every dismiss settles", async () => {
    const delayed = deferred<void>();
    const pushToast = vi.fn();
    const dismissJob = vi.fn().mockReturnValue(delayed.promise);
    const dismissClipJob = vi.fn().mockRejectedValue(new SpoolApiError(503, "unreachable", "offline"));
    importHarness.ctx = baseCtx({
      jobs: [
        jobFixture({ id: "download-done", status: "done", domain: "download" }),
        jobFixture({ id: "clip-failed", status: "failed", domain: "clip", type: "render" }),
      ],
      client: clientFixture({ dismissJob, dismissClipJob }),
      pushToast,
    });
    render(<QueueScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Clear finished" }));

    expect(dismissJob).toHaveBeenCalledWith("download-done");
    expect(dismissClipJob).toHaveBeenCalledWith("clip-failed");
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => { delayed.resolve(); await delayed.promise; });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Finished-job cleanup settled",
      body: expect.stringMatching(/^1 succeeded · 1 failed · unreachable:/),
    })));
  });

  it("Source retranscription surfaces a structured rejection without early success", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    importHarness.params = { id: "source-1" };
    importHarness.queryData = {
      getTranscriptDoc: undefined,
      sourceEnergy: { bars: [], buckets: 0 },
    };
    importHarness.ctx = baseCtx({
      sources: [sourceFixture()],
      client: clientFixture({ startTranscribe: vi.fn().mockReturnValue(delayed.promise) }),
      pushToast,
      nav,
    });
    render(<ProjectScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Re-transcribe" }));

    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await act(async () => {
      delayed.reject(new SpoolApiError(429, "queue_full", "capacity"));
      await delayed.promise.catch(() => undefined);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("queue_full");
    expect(screen.getByRole("alert")).toHaveTextContent("The work queue is full");
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("Editor render surfaces a delayed rejection and never navigates or claims success", async () => {
    const delayed = deferred<void>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      sources: [],
      makeClipsFrom: vi.fn().mockReturnValue(delayed.promise),
      pushToast,
      nav,
      client: clientFixture(),
    });
    render(<EditorScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Render" }));

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
    await act(async () => {
      delayed.reject(new SpoolApiError(429, "queue_full", "capacity"));
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Render failed",
      body: expect.stringMatching(/queue_full/),
    })));
    expect(nav).not.toHaveBeenCalled();
  });

  it("Reframe renders no fake detection claims and exposes a structured submit failure", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      snapshot: importHarness.snapshot,
      client: clientFixture({ reframe: vi.fn().mockReturnValue(delayed.promise) }),
      pushToast,
      nav,
    });
    render(<StrictMode><ReframeScreen /></StrictMode>);

    for (const fake of ["Auto-detect", "speaker 2", "Live 9:16 preview", "Single scene"])
      expect(screen.queryByText(fake)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Verify ROI track" }));
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(new SpoolApiError(403, "origin_forbidden", "blocked"));
      await delayed.promise.catch(() => undefined);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("origin_forbidden");
    expect(screen.getByRole("alert")).toHaveTextContent("blocked by the engine's origin policy");
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("Reframe Verify stays locked after admission until the job is terminal", async () => {
    const terminal = deferred<void>();
    const reframe = vi.fn().mockResolvedValue({ id: "reframe-1" });
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      client: clientFixture({ reframe }),
      awaitClipJob,
    });
    render(<ReframeScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Verify ROI track" }));
    await waitFor(() => expect(reframe).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("reframe-1"));

    const verify = screen.getByRole("button", { name: "Submitting…" });
    const apply = screen.getByRole("button", { name: "Working…" });
    expect(verify).toBeDisabled();
    expect(apply).toBeDisabled();
    fireEvent.click(verify);
    fireEvent.click(apply);
    expect(reframe).toHaveBeenCalledTimes(1);

    await act(async () => {
      terminal.resolve();
      await terminal.promise;
    });
  });

  it("Reframe never reports completion or navigates after the initiating view is left", async () => {
    const terminal = deferred<void>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      snapshot: importHarness.snapshot,
      client: clientFixture({ reframe: vi.fn().mockResolvedValue({ id: "reframe-1" }) }),
      awaitClipJob,
      pushToast,
      nav,
    });
    window.history.replaceState({}, "", "/clips/clip-1/reframe");
    render(<ReframeScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Apply & continue to captions" }));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("reframe-1"));
    window.history.pushState({}, "", "/library");
    await act(async () => { terminal.resolve(); await terminal.promise; });

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
    window.history.replaceState({}, "", "/");
  });

  it("Reframe never reports completion or updates navigation after unmount", async () => {
    const terminal = deferred<void>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    importHarness.params = { id: "clip-1" };
    importHarness.ctx = baseCtx({
      clips: [clipFixture()],
      snapshot: importHarness.snapshot,
      client: clientFixture({ reframe: vi.fn().mockResolvedValue({ id: "reframe-1" }) }),
      awaitClipJob,
      pushToast,
      nav,
    });
    const view = render(<ReframeScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Apply & continue to captions" }));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("reframe-1"));
    view.unmount();
    await act(async () => { terminal.resolve(); await terminal.promise; });

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("Brand apply waits for all clips, then reports aggregate truth and navigates only for success", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    const caption = vi.fn()
      .mockReturnValueOnce(delayed.promise)
      .mockRejectedValueOnce(new SpoolApiError(429, "queue_full", "capacity"));
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const awaitClipJob = vi.fn().mockResolvedValue(undefined);
    importHarness.queryData = { listBrandKits: { brand_kits: [] } };
    importHarness.ctx = baseCtx({
      sources: [sourceFixture()],
      clips: [clipFixture({ id: "clip-1", platform: undefined }), clipFixture({ id: "clip-2", title: "Second clip" })],
      client: clientFixture({ caption, render: renderClip }),
      awaitClipJob,
      pushToast,
      nav,
    });
    render(<StrictMode><BrandScreen /></StrictMode>);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "source-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 clips" }));

    expect(caption).toHaveBeenCalledTimes(2);
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => { delayed.resolve({ id: "caption-1" }); await delayed.promise; });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Brand apply settled for 2 clips",
      body: expect.stringMatching(/^1 succeeded · 1 failed · queue_full:/),
    })));
    expect(awaitClipJob).toHaveBeenCalledWith("caption-1");
    expect(renderClip).toHaveBeenCalledTimes(1);
    expect(renderClip).toHaveBeenCalledWith("clip-1", {});
    expect(nav).toHaveBeenCalledWith("queue");
  });

  it("Recipes withhold navigation and expose the structured produce rejection", async () => {
    const delayed = deferred<unknown>();
    const pushToast = vi.fn();
    importHarness.queryData = {
      listRecipes: { recipes: [] },
      listBrandKits: { brand_kits: [] },
    };
    importHarness.ctx = baseCtx({
      sources: [sourceFixture()],
      client: clientFixture({ produce: vi.fn().mockReturnValue(delayed.promise) }),
      pushToast,
    });
    render(<RecipesScreen />);
    const projectSelect = screen.getAllByRole("combobox").at(-1)!;
    fireEvent.change(projectSelect, { target: { value: "source-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Run recipe" }));

    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(importHarness.router.push).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(new SpoolApiError(429, "queue_full", "capacity"));
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Couldn't run the recipe",
      body: expect.stringMatching(/^queue_full:/),
    })));
    expect(importHarness.router.push).not.toHaveBeenCalled();
  });

  it("Recipes never announce success or redirect after the initiating route is left", async () => {
    const delayed = deferred<unknown>();
    const pushToast = vi.fn();
    importHarness.queryData = {
      listRecipes: { recipes: [] },
      listBrandKits: { brand_kits: [] },
    };
    importHarness.ctx = baseCtx({
      sources: [sourceFixture()],
      client: clientFixture({ produce: vi.fn().mockReturnValue(delayed.promise) }),
      pushToast,
    });
    window.history.replaceState({}, "", "/recipes");
    render(<StrictMode><RecipesScreen /></StrictMode>);
    fireEvent.change(screen.getAllByRole("combobox").at(-1)!, { target: { value: "source-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Run recipe" }));
    window.history.pushState({}, "", "/library");

    await act(async () => { delayed.resolve({ jobs: [] }); await delayed.promise; });

    expect(pushToast).not.toHaveBeenCalled();
    expect(importHarness.router.push).not.toHaveBeenCalled();
  });

  it("Watches expose a delayed scan rejection without an early success toast", async () => {
    const delayed = deferred<unknown>();
    const pushToast = vi.fn();
    const watch = {
      id: "watch-1", name: "Incoming videos", kind: "folder", target: "/tmp/incoming",
      recipe_id: undefined, enabled: true, produced: [], seen: [],
    };
    importHarness.queryData = {
      listWatches: { watches: [watch] },
      listRecipes: { recipes: [] },
    };
    importHarness.ctx = baseCtx({
      client: clientFixture({ scanWatch: vi.fn().mockReturnValue(delayed.promise) }),
      pushToast,
    });
    render(<WatchesScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));

    expect(pushToast).not.toHaveBeenCalled();
    await act(async () => {
      delayed.reject(new SpoolApiError(0, "unreachable", "offline"));
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Scan failed",
      body: expect.stringMatching(/^unreachable:/),
    })));
  });

  it("Brand keeps the edited record stable while a save is pending", async () => {
    const delayed = deferred<Record<string, unknown>>();
    const kit = {
      id: "kit-1", name: "Primary kit", palette: ["#45556E"], caption_preset: "opus",
      caption_overrides: {}, watermark: "", lower_third: "",
    };
    importHarness.queryData = { listBrandKits: { brand_kits: [kit] } };
    importHarness.ctx = baseCtx({
      client: clientFixture({ updateBrandKit: vi.fn().mockReturnValue(delayed.promise) }),
    });
    render(<BrandScreen />);
    const name = await screen.findByPlaceholderText("Kit name");
    expect(name).toHaveValue("Primary kit");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "New kit" }));

    expect(name).toHaveValue("Primary kit");
    await act(async () => { delayed.resolve(kit); await delayed.promise; });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());
    expect(name).toHaveValue("Primary kit");
  });
});
