import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { SpoolApiError } from "@spool/api-client";
import type { EventsSnapshot } from "@spool/types";
import type { SpoolClip, SpoolJob, SpoolSource, TranscriptLine } from "@/components/spool/context";

const harness = vi.hoisted(() => ({
  ctx: null as null | Record<string, unknown>,
  params: { id: "clip-1" } as Record<string, string>,
  snapshot: { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot,
  queryData: {} as Record<string, unknown>,
  queryReload: {} as Record<string, ReturnType<typeof vi.fn>>,
  router: {
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
    refresh: vi.fn(),
  },
}));

vi.mock("next/navigation", () => ({
  useParams: () => harness.params,
  useRouter: () => harness.router,
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/engine-context", () => ({
  useLive: () => ({ snapshot: harness.snapshot, connection: "online" }),
  useEngine: () => harness.ctx?.client,
  useEngineQuery: (query: (client: unknown) => unknown) => {
    let method = "";
    const recordingClient = new Proxy(
      {},
      {
        get: (_target, property) => () => {
          method = String(property);
          return undefined;
        },
      },
    );
    query(recordingClient);
    return {
      data: harness.queryData[method],
      loading: false,
      error: null,
      reload: harness.queryReload[method] ?? vi.fn(),
    };
  },
}));

vi.mock("@/components/spool/context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/spool/context")>();
  return { ...actual, useSpool: () => harness.ctx };
});

import BrandScreen from "@/app/brand/page";
import CaptionScreen from "@/app/clips/[id]/caption/page";
import EditorScreen from "@/app/clips/[id]/page";
import LibraryScreen from "@/app/library/page";
import QueueScreen from "@/app/queue/page";
import RecipesScreen from "@/app/recipes/page";
import SettingsScreen from "@/app/settings/page";
import ProjectScreen from "@/app/sources/[id]/page";
import WatchesScreen from "@/app/watches/page";
import { DiscoveryBody, TranscriptView } from "@/components/spool/work";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const structuredFailure = () => new SpoolApiError(429, "queue_full", "capacity");

const sourceFixture = (overrides: Partial<SpoolSource> = {}): SpoolSource => ({
  id: "source-1",
  title: "Test source",
  src: "youtube",
  dur: 90,
  status: "ready",
  prog: 100,
  clips: 1,
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
  title: "Test clip",
  src: "source-1",
  dur: 10,
  aspect: "9:16",
  style: "opus",
  platform: "tiktok",
  status: "ready",
  prog: 100,
  tags: [],
  start: 0,
  end: 10,
  ...overrides,
});

const jobFixture = (overrides: Partial<SpoolJob> = {}): SpoolJob => ({
  id: "download-1",
  type: "download",
  label: "Test download",
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
  jobFileUrl: vi.fn().mockReturnValue("https://files.example.test/source.mp4"),
  clipArtifactUrl: vi.fn().mockReturnValue("https://files.example.test/clip.mp4"),
  renderFileUrl: vi.fn().mockReturnValue("https://files.example.test/render.mp4"),
  cancelJob: vi.fn().mockResolvedValue(undefined),
  cancelClipJob: vi.fn().mockResolvedValue(undefined),
  dismissJob: vi.fn().mockResolvedValue(undefined),
  dismissClipJob: vi.fn().mockResolvedValue(undefined),
  pauseJob: vi.fn().mockResolvedValue(undefined),
  resumeJob: vi.fn().mockResolvedValue(undefined),
  ...overrides,
});

const baseCtx = (overrides: Record<string, unknown> = {}) => ({
  client: clientFixture(),
  sources: [],
  clips: [],
  jobs: [],
  downloads: [],
  deps: [],
  snapshot: harness.snapshot,
  nav: vi.fn(),
  pushToast: vi.fn(),
  makeClipsFrom: vi.fn().mockResolvedValue(undefined),
  awaitClipJob: vi.fn().mockResolvedValue(undefined),
  ...overrides,
});

const transcriptLines: TranscriptLine[] = [
  {
    id: 0,
    sp: "speaker-1",
    t: 0,
    words: "hello world",
    tokens: [
      { w: "hello", ti: 0, te: 1, idx: 0 },
      { w: "world", ti: 1, te: 2, idx: 1 },
    ],
  },
];

beforeEach(() => {
  harness.ctx = null;
  harness.params = { id: "clip-1" };
  harness.snapshot = { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot;
  harness.queryData = {};
  harness.queryReload = {};
  Object.values(harness.router).forEach((method) => method.mockReset());
  window.history.replaceState({}, "", "/");
});

describe("visible mutation inventory: Library", () => {
  it("waits for every find-clips request and reports a structured partial failure", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const findMoments = vi
      .fn()
      .mockReturnValueOnce(delayed.promise)
      .mockRejectedValueOnce(structuredFailure());
    harness.ctx = baseCtx({
      sources: [sourceFixture(), sourceFixture({ id: "source-2", title: "Second source" })],
      client: clientFixture({ findMoments }),
      pushToast,
    });
    render(<LibraryScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Select Test source" }));
    fireEvent.click(screen.getByRole("button", { name: "Select Second source" }));
    fireEvent.click(screen.getByRole("button", { name: "Find clips" }));

    expect(findMoments).toHaveBeenCalledTimes(2);
    expect(pushToast).not.toHaveBeenCalled();
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await act(async () => {
      delayed.resolve({ id: "moments-1" });
      await delayed.promise;
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Find-clips requests settled",
          body: expect.stringMatching(/^1 succeeded · 1 failed · queue_full:/),
        }),
      ),
    );
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("starts one find-clips batch across same-tick repeated clicks", async () => {
    const delayed = deferred<{ id: string }>();
    const findMoments = vi.fn().mockReturnValue(delayed.promise);
    harness.ctx = baseCtx({
      sources: [sourceFixture()],
      client: clientFixture({ findMoments }),
    });
    render(<LibraryScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Select Test source" }));
    const find = screen.getByRole("button", { name: "Find clips" });
    act(() => {
      find.click();
      find.click();
    });
    const callsBeforeSettlement = findMoments.mock.calls.length;

    await act(async () => {
      delayed.resolve({ id: "moments-1" });
      await delayed.promise;
      await Promise.resolve();
    });
    expect(callsBeforeSettlement).toBe(1);
  });
});

describe("visible mutation inventory: Queue", () => {
  it.each([
    {
      action: "Cancel",
      status: "queued",
      method: "cancelJob",
      failureTitle: "Couldn't cancel job",
    },
    {
      action: "Pause",
      status: "running",
      method: "pauseJob",
      failureTitle: "Couldn't pause download",
    },
    {
      action: "Resume",
      status: "paused",
      method: "resumeJob",
      failureTitle: "Couldn't resume download",
    },
  ])(
    "withholds a result for $action until its request rejects",
    async ({ action, status, method, failureTitle }) => {
      const delayed = deferred<void>();
      const pushToast = vi.fn();
      const nav = vi.fn();
      const mutation = vi.fn().mockReturnValue(delayed.promise);
      harness.ctx = baseCtx({
        jobs: [jobFixture({ status })],
        client: clientFixture({ [method]: mutation }),
        pushToast,
        nav,
      });
      render(<QueueScreen />);

      fireEvent.click(screen.getByRole("button", { name: action }));
      expect(mutation).toHaveBeenCalledWith("download-1");
      expect(pushToast).not.toHaveBeenCalled();
      expect(nav).not.toHaveBeenCalled();

      await act(async () => {
        delayed.reject(structuredFailure());
        await delayed.promise.catch(() => undefined);
      });
      await waitFor(() =>
        expect(pushToast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: failureTitle,
            body: expect.stringMatching(/^queue_full:/),
          }),
        ),
      );
      expect(nav).not.toHaveBeenCalled();
    },
  );

  it("waits for the whole Pause all batch before reporting a structured failure", async () => {
    const delayed = deferred<void>();
    const pushToast = vi.fn();
    const pauseJob = vi
      .fn()
      .mockReturnValueOnce(delayed.promise)
      .mockRejectedValueOnce(structuredFailure());
    harness.ctx = baseCtx({
      jobs: [jobFixture(), jobFixture({ id: "download-2" })],
      client: clientFixture({ pauseJob }),
      pushToast,
    });
    render(<QueueScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Pause all" }));
    expect(pauseJob).toHaveBeenCalledTimes(2);
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      delayed.resolve();
      await delayed.promise;
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Pause requests settled",
          body: expect.stringMatching(/^1 succeeded · 1 failed · queue_full:/),
        }),
      ),
    );
  });
});

describe("visible mutation inventory: Source work", () => {
  it("starts one retranscription across same-tick repeated clicks", async () => {
    const delayed = deferred<{ id: string }>();
    const startTranscribe = vi.fn().mockReturnValue(delayed.promise);
    harness.params = { id: "source-1" };
    harness.queryData = {
      getTranscriptDoc: undefined,
      sourceEnergy: { bars: [], buckets: 0 },
    };
    harness.ctx = baseCtx({
      sources: [sourceFixture()],
      client: clientFixture({ startTranscribe }),
    });
    render(<ProjectScreen />);

    const retranscribe = screen.getByRole("button", { name: "Re-transcribe" });
    act(() => {
      retranscribe.click();
      retranscribe.click();
    });
    const callsBeforeSettlement = startTranscribe.mock.calls.length;

    await act(async () => {
      delayed.resolve({ id: "transcript-2" });
      await delayed.promise;
      await Promise.resolve();
    });
    expect(callsBeforeSettlement).toBe(1);
  });

  it("waits for all discovery modes before surfacing a structured scan failure", async () => {
    const delayed = deferred<{ id: string }>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    const findMoments = vi.fn((_sourceId: string, options: { mode: string }) => {
      if (options.mode === "funny") return delayed.promise;
      if (options.mode === "insightful") return Promise.reject(structuredFailure());
      return Promise.resolve({ id: `moments-${options.mode}` });
    });
    harness.ctx = baseCtx({ client: clientFixture({ findMoments }), pushToast, nav });
    render(<DiscoveryBody candidates={[]} sourceId="source-1" finding={false} />);

    fireEvent.click(screen.getAllByRole("button", { name: "Scan all modes" })[0]!);
    expect(findMoments).toHaveBeenCalledTimes(6);
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.resolve({ id: "moments-funny" });
      await delayed.promise;
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Some scans could not start",
          body: expect.stringMatching(/^5 succeeded · 1 failed\./),
        }),
      ),
    );
    expect(pushToast.mock.calls[0]?.[0].body).toMatch(/queue_full/);
    expect(nav).not.toHaveBeenCalled();
  });

  it("does not reload or claim a transcript edit before a structured rejection", async () => {
    const delayed = deferred<void>();
    const pushToast = vi.fn();
    const reload = vi.fn();
    const editWord = vi.fn().mockReturnValue(delayed.promise);
    const cut = vi.fn().mockResolvedValue({ id: "cut-1" });
    harness.ctx = baseCtx({ client: clientFixture({ editWord, cut }), pushToast });
    render(
      <TranscriptView
        lines={transcriptLines}
        speakers={{ "speaker-1": { name: "Speaker 1", color: "teal" } }}
        tid="transcript-1"
        sourceId="source-1"
        onEdited={reload}
      />,
    );

    fireEvent.click(screen.getByText("hello"));
    fireEvent.doubleClick(screen.getByText("hello"));
    const save = screen.getByTitle("save");
    const deleteWord = screen.getByTitle("delete word");
    const cutSelection = screen.getByRole("button", { name: "Cut clip from selection" });
    act(() => {
      save.click();
      save.click();
      deleteWord.click();
      cutSelection.click();
    });
    expect(editWord).toHaveBeenCalledTimes(1);
    expect(cut).not.toHaveBeenCalled();
    expect(editWord).toHaveBeenCalledWith("transcript-1", 0, { op: "set_text", w: "hello" });
    expect(save).toBeDisabled();
    expect(deleteWord).toBeDisabled();
    expect(cutSelection).toBeDisabled();
    expect(reload).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Transcript edit failed",
          body: expect.stringMatching(/queue_full/),
        }),
      ),
    );
    expect(reload).not.toHaveBeenCalled();
  });

  it("blocks transcript word edits while a selection cut is pending", async () => {
    const delayed = deferred<void>();
    const cut = vi.fn().mockReturnValue(delayed.promise);
    const editWord = vi.fn().mockResolvedValue(undefined);
    harness.ctx = baseCtx({ client: clientFixture({ cut, editWord }) });
    render(
      <TranscriptView
        lines={transcriptLines}
        speakers={{ "speaker-1": { name: "Speaker 1", color: "teal" } }}
        tid="transcript-1"
        sourceId="source-1"
      />,
    );

    fireEvent.click(screen.getByText("hello"));
    fireEvent.click(screen.getByRole("button", { name: "Cut clip from selection" }));
    fireEvent.doubleClick(screen.getByText("hello"));
    const save = screen.getByTitle("save");
    fireEvent.click(save);

    expect(cut).toHaveBeenCalledTimes(1);
    expect(editWord).not.toHaveBeenCalled();
    expect(save).toBeDisabled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
  });

  it("does not navigate or claim a transcript cut before a structured rejection", async () => {
    const delayed = deferred<void>();
    const pushToast = vi.fn();
    const nav = vi.fn();
    const cut = vi.fn().mockReturnValue(delayed.promise);
    harness.ctx = baseCtx({ client: clientFixture({ cut }), pushToast, nav });
    render(
      <TranscriptView
        lines={transcriptLines}
        speakers={{ "speaker-1": { name: "Speaker 1", color: "teal" } }}
        tid="transcript-1"
        sourceId="source-1"
      />,
    );

    fireEvent.click(screen.getByText("hello"));
    fireEvent.click(screen.getByRole("button", { name: "Cut clip from selection" }));
    expect(cut).toHaveBeenCalledWith("source-1", { start: 0, end: 1 });
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Clip cut failed",
          body: expect.stringMatching(/queue_full/),
        }),
      ),
    );
    expect(nav).not.toHaveBeenCalled();
  });
});

const editorWords = [
  { idx: 0, w: "hello", start: 0, end: 1, deleted: false, speaker: "speaker-1" },
  { idx: 1, w: "world", start: 1, end: 2, deleted: true, speaker: "speaker-1" },
];

function renderEditor(client: Record<string, unknown>, pushToast = vi.fn(), nav = vi.fn()) {
  harness.queryData = {
    getTranscriptDoc: { words: editorWords, segments: [] },
    sourceEnergy: { bars: [], buckets: 0 },
    sourceScenes: { cuts: [] },
    sourceFilmstrip: { strip: null, frames: 0 },
  };
  harness.queryReload = { getTranscriptDoc: vi.fn() };
  harness.ctx = baseCtx({
    sources: [sourceFixture()],
    clips: [clipFixture()],
    client: clientFixture(client),
    pushToast,
    nav,
  });
  render(<EditorScreen />);
  return { pushToast, nav, reload: harness.queryReload.getTranscriptDoc! };
}

describe("visible mutation inventory: Editor", () => {
  it("does not claim a real preview before a structured reframe rejection", async () => {
    const delayed = deferred<{ id: string }>();
    const reframe = vi.fn().mockReturnValue(delayed.promise);
    const { pushToast, nav } = renderEditor({ reframe });

    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview real reframe" }));
    expect(reframe).toHaveBeenCalledWith("clip-1", {
      aspect: "9:16",
      mode: "split",
      preview: true,
    });
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Preview failed",
          body: expect.stringMatching(/^queue_full:/),
        }),
      ),
    );
    expect(nav).not.toHaveBeenCalled();
  });

  it("does not reload or claim an editor word edit before a structured rejection", async () => {
    const delayed = deferred<void>();
    const editWord = vi.fn().mockReturnValue(delayed.promise);
    const cut = vi.fn().mockResolvedValue({ id: "cut-1" });
    const makeClipsFrom = vi.fn().mockResolvedValue(undefined);
    const { pushToast, nav, reload } = renderEditor({ editWord, cut });
    Object.assign(harness.ctx!, { makeClipsFrom });

    const deleteWord = screen.getByTitle("delete word (ripple-cut on Re-cut)");
    const recut = screen.getByRole("button", { name: "Re-cut (drop 1)" });
    const renderButton = screen.getByRole("button", { name: "Render" });
    act(() => {
      deleteWord.click();
      deleteWord.click();
      recut.click();
      renderButton.click();
    });
    expect(editWord).toHaveBeenCalledTimes(1);
    expect(cut).not.toHaveBeenCalled();
    expect(makeClipsFrom).not.toHaveBeenCalled();
    expect(editWord).toHaveBeenCalledWith("transcript-1", 0, { op: "delete" });
    expect(deleteWord).toBeDisabled();
    expect(recut).toBeDisabled();
    expect(renderButton).toBeDisabled();
    expect(reload).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Word edit failed",
          body: expect.stringMatching(/^queue_full:/),
        }),
      ),
    );
    expect(reload).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("does not navigate or claim an editor re-cut before a structured rejection", async () => {
    const delayed = deferred<void>();
    const cut = vi.fn().mockReturnValue(delayed.promise);
    const editWord = vi.fn().mockResolvedValue(undefined);
    const makeClipsFrom = vi.fn().mockResolvedValue(undefined);
    const { pushToast, nav } = renderEditor({ cut, editWord });
    Object.assign(harness.ctx!, { makeClipsFrom });

    const recut = screen.getByRole("button", { name: "Re-cut (drop 1)" });
    const deleteWord = screen.getByTitle("delete word (ripple-cut on Re-cut)");
    const renderButton = screen.getByRole("button", { name: "Render" });
    act(() => {
      recut.click();
      deleteWord.click();
      renderButton.click();
    });
    expect(cut).toHaveBeenCalledWith("source-1", { start: 0, end: 10 });
    expect(editWord).not.toHaveBeenCalled();
    expect(makeClipsFrom).not.toHaveBeenCalled();
    expect(deleteWord).toBeDisabled();
    expect(renderButton).toBeDisabled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Re-cut failed",
          body: expect.stringMatching(/^queue_full:/),
        }),
      ),
    );
    expect(nav).not.toHaveBeenCalled();
  });
});

const persistedFixtures = {
  brand: {
    queries: {
      listBrandKits: {
        brand_kits: [
          {
            id: "kit-1",
            name: "Primary kit",
            palette: ["#45556E"],
            caption_preset: "opus",
            caption_overrides: {},
            watermark: "",
            lower_third: "",
          },
        ],
      },
    },
    Screen: BrandScreen,
    saveMethod: "updateBrandKit",
    deleteMethod: "deleteBrandKit",
    deleteTitle: null,
    saveFailure: "Couldn't save the kit",
    deleteFailure: "Couldn't delete the kit",
  },
  recipe: {
    queries: {
      listRecipes: {
        recipes: [
          {
            id: "recipe-1",
            name: "Highlights",
            content_mode: "funny",
            count: 6,
            aspect: "9:16",
            reframe_mode: "pan",
            caption_preset: "opus",
            platform: "tiktok",
            fast: true,
            weights: {},
          },
        ],
      },
      listBrandKits: { brand_kits: [] },
    },
    Screen: RecipesScreen,
    saveMethod: "updateRecipe",
    deleteMethod: "deleteRecipe",
    deleteTitle: "Delete recipe",
    saveFailure: "Couldn't save the recipe",
    deleteFailure: "Couldn't delete the recipe",
  },
  watch: {
    queries: {
      listWatches: {
        watches: [
          {
            id: "watch-1",
            name: "Incoming videos",
            kind: "folder",
            target: "/tmp/incoming",
            enabled: true,
            produced: [],
            seen: [],
          },
        ],
      },
      listRecipes: { recipes: [] },
    },
    Screen: WatchesScreen,
    saveMethod: "updateWatch",
    deleteMethod: "deleteWatch",
    deleteTitle: "Delete watch",
    saveFailure: "Couldn't save the watch",
    deleteFailure: "Couldn't delete the watch",
  },
} as const;

describe.each(Object.entries(persistedFixtures))(
  "visible mutation inventory: %s persistence",
  (_kind, fixture) => {
    it.each([
      { operation: "save", method: fixture.saveMethod, failureTitle: fixture.saveFailure },
      { operation: "delete", method: fixture.deleteMethod, failureTitle: fixture.deleteFailure },
    ])(
      "withholds $operation success and exposes a structured rejection",
      async ({ operation, method, failureTitle }) => {
        const delayed = deferred<Record<string, unknown>>();
        const mutation = vi.fn().mockReturnValue(delayed.promise);
        const pushToast = vi.fn();
        const nav = vi.fn();
        harness.queryData = fixture.queries;
        harness.queryReload = Object.fromEntries(
          Object.keys(fixture.queries).map((key) => [key, vi.fn()]),
        );
        harness.ctx = baseCtx({
          client: clientFixture({ [method]: mutation }),
          pushToast,
          nav,
        });
        const view = render(<fixture.Screen />);

        if (operation === "save") {
          fireEvent.click(await screen.findByRole("button", { name: "Save" }));
        } else if (fixture.deleteTitle) {
          fireEvent.click(await screen.findByTitle(fixture.deleteTitle));
        } else {
          const deleteButton =
            view.container.querySelector<HTMLButtonElement>("button.btn.subtle.sm");
          expect(deleteButton).not.toBeNull();
          fireEvent.click(deleteButton!);
        }

        expect(mutation).toHaveBeenCalledTimes(1);
        expect(pushToast).not.toHaveBeenCalled();
        expect(nav).not.toHaveBeenCalled();
        expect(harness.router.push).not.toHaveBeenCalled();

        await act(async () => {
          delayed.reject(structuredFailure());
          await delayed.promise.catch(() => undefined);
        });
        await waitFor(() =>
          expect(pushToast).toHaveBeenCalledWith(
            expect.objectContaining({
              title: failureTitle,
              body: expect.stringMatching(/^queue_full:/),
            }),
          ),
        );
        expect(nav).not.toHaveBeenCalled();
        expect(harness.router.push).not.toHaveBeenCalled();
      },
    );
  },
);

describe.each([
  {
    kind: "recipe",
    fixture: persistedFixtures.recipe,
    inputPlaceholder: "Recipe name",
    deleteTitle: "Delete recipe",
    successTitle: "Recipe deleted",
  },
  {
    kind: "watch",
    fixture: persistedFixtures.watch,
    inputPlaceholder: "Watch name",
    deleteTitle: "Delete watch",
    successTitle: "Watch deleted",
  },
])(
  "visible mutation inventory: $kind delete completion",
  ({ fixture, inputPlaceholder, deleteTitle, successTitle }) => {
    it("clears the deleted selection and form only after deletion succeeds", async () => {
      const delayed = deferred<void>();
      const mutation = vi.fn().mockReturnValue(delayed.promise);
      const pushToast = vi.fn();
      harness.queryData = fixture.queries;
      harness.queryReload = Object.fromEntries(
        Object.keys(fixture.queries).map((key) => [key, vi.fn()]),
      );
      harness.ctx = baseCtx({
        client: clientFixture({ [fixture.deleteMethod]: mutation }),
        pushToast,
      });
      render(<fixture.Screen />);

      const input = await screen.findByPlaceholderText(inputPlaceholder);
      const originalName = input.getAttribute("value");
      fireEvent.click(screen.getByTitle(deleteTitle));

      expect(mutation).toHaveBeenCalledTimes(1);
      expect(input).toHaveValue(originalName);
      expect(pushToast).not.toHaveBeenCalled();

      await act(async () => {
        delayed.resolve();
        await delayed.promise;
      });
      await waitFor(() =>
        expect(pushToast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: successTitle,
          }),
        ),
      );
      expect(input).toHaveValue("");
      expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
      expect(screen.queryByTitle(deleteTitle)).not.toBeInTheDocument();
    });
  },
);

describe("visible mutation inventory: same-tick persistence locks", () => {
  it.each(["save", "delete", "apply"] as const)(
    "Brand %s excludes its repeat and competing mutations",
    async (primary) => {
      const savePending = deferred<Record<string, unknown>>();
      const deletePending = deferred<void>();
      const applyPending = deferred<{ id: string }>();
      const updateBrandKit = vi.fn().mockReturnValue(savePending.promise);
      const deleteBrandKit = vi.fn().mockReturnValue(deletePending.promise);
      const caption = vi.fn().mockReturnValue(applyPending.promise);
      harness.queryData = persistedFixtures.brand.queries;
      harness.queryReload = { listBrandKits: vi.fn() };
      harness.ctx = baseCtx({
        sources: [sourceFixture()],
        clips: [clipFixture()],
        client: clientFixture({
          updateBrandKit,
          deleteBrandKit,
          caption,
          render: vi.fn().mockResolvedValue({ id: "render-1" }),
        }),
      });
      const view = render(<BrandScreen />);
      fireEvent.change(screen.getByRole("combobox"), { target: { value: "source-1" } });

      const deleteButton = view.container.querySelector<HTMLButtonElement>("button.btn.subtle.sm");
      expect(deleteButton).not.toBeNull();
      const buttons = {
        save: screen.getByRole("button", { name: "Save" }),
        delete: deleteButton!,
        apply: screen.getByRole("button", { name: "Apply to 1 clip" }),
      };
      act(() => {
        buttons[primary].click();
        buttons[primary].click();
        Object.entries(buttons).forEach(([name, button]) => {
          if (name !== primary) button.click();
        });
      });
      const callsBeforeSettlement = {
        save: updateBrandKit.mock.calls.length,
        delete: deleteBrandKit.mock.calls.length,
        apply: caption.mock.calls.length,
      };

      await act(async () => {
        savePending.resolve(persistedFixtures.brand.queries.listBrandKits.brand_kits[0]);
        deletePending.resolve();
        applyPending.resolve({ id: "caption-1" });
        await Promise.all([savePending.promise, deletePending.promise, applyPending.promise]);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(callsBeforeSettlement).toEqual({
        save: primary === "save" ? 1 : 0,
        delete: primary === "delete" ? 1 : 0,
        apply: primary === "apply" ? 1 : 0,
      });
    },
  );

  it.each(["save", "delete", "run"] as const)(
    "Recipes %s excludes its repeat and competing mutations",
    async (primary) => {
      const savePending = deferred<Record<string, unknown>>();
      const deletePending = deferred<void>();
      const runPending = deferred<void>();
      const updateRecipe = vi.fn().mockReturnValue(savePending.promise);
      const deleteRecipe = vi.fn().mockReturnValue(deletePending.promise);
      const produce = vi.fn().mockReturnValue(runPending.promise);
      harness.queryData = persistedFixtures.recipe.queries;
      harness.queryReload = { listRecipes: vi.fn(), listBrandKits: vi.fn() };
      harness.ctx = baseCtx({
        sources: [sourceFixture()],
        client: clientFixture({ updateRecipe, deleteRecipe, produce }),
      });
      render(<RecipesScreen />);
      fireEvent.change(screen.getAllByRole("combobox").at(-1)!, {
        target: { value: "source-1" },
      });

      const buttons = {
        save: screen.getByRole("button", { name: "Save" }),
        delete: screen.getByTitle("Delete recipe"),
        run: screen.getByRole("button", { name: "Run recipe" }),
      };
      act(() => {
        buttons[primary].click();
        buttons[primary].click();
        Object.entries(buttons).forEach(([name, button]) => {
          if (name !== primary) button.click();
        });
      });
      const callsBeforeSettlement = {
        save: updateRecipe.mock.calls.length,
        delete: deleteRecipe.mock.calls.length,
        run: produce.mock.calls.length,
      };

      await act(async () => {
        savePending.resolve(persistedFixtures.recipe.queries.listRecipes.recipes[0]);
        deletePending.resolve();
        runPending.resolve();
        await Promise.all([savePending.promise, deletePending.promise, runPending.promise]);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(callsBeforeSettlement).toEqual({
        save: primary === "save" ? 1 : 0,
        delete: primary === "delete" ? 1 : 0,
        run: primary === "run" ? 1 : 0,
      });
    },
  );

  it.each(["save", "delete", "scan"] as const)(
    "Watches %s excludes its repeat and competing mutations",
    async (primary) => {
      const savePending = deferred<Record<string, unknown>>();
      const deletePending = deferred<void>();
      const scanPending = deferred<{
        ingested: string[];
        produced: string[];
        producing: Record<string, unknown>;
        pending: Record<string, unknown>;
        ingesting: Record<string, unknown>;
      }>();
      const updateWatch = vi.fn().mockReturnValue(savePending.promise);
      const deleteWatch = vi.fn().mockReturnValue(deletePending.promise);
      const scanWatch = vi.fn().mockReturnValue(scanPending.promise);
      harness.queryData = persistedFixtures.watch.queries;
      harness.queryReload = { listWatches: vi.fn(), listRecipes: vi.fn() };
      harness.ctx = baseCtx({
        client: clientFixture({ updateWatch, deleteWatch, scanWatch }),
      });
      render(<WatchesScreen />);

      const buttons = {
        save: screen.getByRole("button", { name: "Save" }),
        delete: screen.getByTitle("Delete watch"),
        scan: screen.getByRole("button", { name: "Scan now" }),
      };
      act(() => {
        buttons[primary].click();
        buttons[primary].click();
        Object.entries(buttons).forEach(([name, button]) => {
          if (name !== primary) button.click();
        });
      });
      const callsBeforeSettlement = {
        save: updateWatch.mock.calls.length,
        delete: deleteWatch.mock.calls.length,
        scan: scanWatch.mock.calls.length,
      };

      await act(async () => {
        savePending.resolve(persistedFixtures.watch.queries.listWatches.watches[0]);
        deletePending.resolve();
        scanPending.resolve({
          ingested: [],
          produced: [],
          producing: {},
          pending: {},
          ingesting: {},
        });
        await Promise.all([savePending.promise, deletePending.promise, scanPending.promise]);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(callsBeforeSettlement).toEqual({
        save: primary === "save" ? 1 : 0,
        delete: primary === "delete" ? 1 : 0,
        scan: primary === "scan" ? 1 : 0,
      });
    },
  );
});

describe("visible mutation inventory: Settings", () => {
  const settingsQueries = (model: Record<string, unknown>) => ({
    doctor: { tools: {}, machine: {}, encoders: [] },
    getSettings: { mcp_transport: "stdio", clip_workers: 2, fast_default: true },
    listModels: { models: [model], active: "base" },
  });

  it("single-flights a setting update and surfaces its structured rejection", async () => {
    const delayed = deferred<Record<string, unknown>>();
    const updateSettings = vi.fn().mockReturnValue(delayed.promise);
    const pushToast = vi.fn();
    harness.queryData = settingsQueries({
      name: "base", label: "Base", is_active: true, is_installed: true, size_bytes: 1,
    });
    harness.ctx = baseCtx({ client: clientFixture({ updateSettings }), pushToast });
    render(<SettingsScreen />);
    fireEvent.click(screen.getByRole("button", { name: "MCP server" }));
    const http = screen.getByRole("button", { name: "HTTP" });

    act(() => { http.click(); http.click(); });
    expect(updateSettings).toHaveBeenCalledTimes(1);
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Couldn't save setting",
      body: expect.stringMatching(/^queue_full:/),
    })));
  });

  it("serializes a different setting changed while a settings write is pending", async () => {
    const first = deferred<Record<string, unknown>>();
    const second = deferred<Record<string, unknown>>();
    const updateSettings = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    harness.queryData = settingsQueries({
      name: "base", label: "Base", is_active: true, is_installed: true, size_bytes: 1,
    });
    harness.ctx = baseCtx({ client: clientFixture({ updateSettings }) });
    render(<SettingsScreen />);

    fireEvent.click(screen.getByRole("button", { name: "MCP server" }));
    fireEvent.click(screen.getByRole("button", { name: "HTTP" }));
    fireEvent.click(screen.getByRole("button", { name: "Hardware" }));
    fireEvent.click(screen.getByRole("button", { name: "Quality" }));

    expect(updateSettings).toHaveBeenCalledTimes(1);
    expect(updateSettings).toHaveBeenNthCalledWith(1, { mcp_transport: "streamable-http" });

    await act(async () => {
      first.resolve({
        mcp_transport: "streamable-http", clip_workers: 2, fast_default: true,
      });
      await first.promise;
      await Promise.resolve();
    });

    expect(updateSettings).toHaveBeenCalledTimes(2);
    expect(updateSettings).toHaveBeenNthCalledWith(2, { fast_default: false });

    await act(async () => {
      second.resolve({
        mcp_transport: "streamable-http", clip_workers: 2, fast_default: false,
      });
      await second.promise;
    });
  });

  it("keeps a debounced concurrency write queued behind another settings write", async () => {
    vi.useFakeTimers();
    try {
      const first = deferred<Record<string, unknown>>();
      const second = deferred<Record<string, unknown>>();
      const updateSettings = vi
        .fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);
      harness.queryData = settingsQueries({
        name: "base", label: "Base", is_active: true, is_installed: true, size_bytes: 1,
      });
      harness.ctx = baseCtx({ client: clientFixture({ updateSettings }) });
      render(<SettingsScreen />);
      fireEvent.click(screen.getByRole("button", { name: "Hardware" }));

      fireEvent.click(screen.getByRole("button", { name: "Quality" }));
      fireEvent.change(screen.getByRole("slider", { name: "Render concurrency" }), {
        target: { value: "5" },
      });
      act(() => vi.advanceTimersByTime(400));

      expect(updateSettings).toHaveBeenCalledTimes(1);
      expect(screen.getByText("5 parallel renders · applies on restart")).toBeInTheDocument();

      await act(async () => {
        first.resolve({ mcp_transport: "stdio", clip_workers: 2, fast_default: false });
        await first.promise;
        await Promise.resolve();
      });

      expect(updateSettings).toHaveBeenCalledTimes(2);
      expect(updateSettings).toHaveBeenNthCalledWith(2, { clip_workers: 5 });

      await act(async () => {
        second.resolve({ mcp_transport: "stdio", clip_workers: 5, fast_default: false });
        await second.promise;
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    { installed: true, method: "useModel", failureTitle: "Couldn't switch model" },
    { installed: false, method: "installModel", failureTitle: "Couldn't install model" },
  ])("single-flights $method and surfaces its structured rejection", async ({ installed, method, failureTitle }) => {
    const delayed = deferred<void>();
    const mutation = vi.fn().mockReturnValue(delayed.promise);
    const pushToast = vi.fn();
    harness.queryData = settingsQueries({
      name: "candidate", label: "Candidate model", is_active: false,
      is_installed: installed, size_bytes: 1_000_000,
    });
    harness.ctx = baseCtx({ client: clientFixture({ [method]: mutation }), pushToast });
    render(<SettingsScreen />);
    const model = await screen.findByRole("button", { name: "Candidate model" });

    act(() => { model.click(); model.click(); });
    expect(mutation).toHaveBeenCalledTimes(1);
    expect(model).toBeDisabled();
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: failureTitle,
      body: expect.stringMatching(/^queue_full:/),
    })));
  });
});

describe("visible mutation inventory: Caption Studio", () => {
  it("uses the configured engine render default when the clip has no known platform", async () => {
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const pushToast = vi.fn();
    harness.params = { id: "clip-1" };
    harness.queryData = { getTranscriptDoc: { words: [], segments: [] } };
    harness.ctx = baseCtx({
      sources: [sourceFixture()], clips: [clipFixture({ platform: undefined })],
      client: clientFixture({
        caption: vi.fn().mockResolvedValue({ id: "caption-1" }),
        render: renderClip,
      }),
      pushToast,
    });
    render(<CaptionScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Burn captions" }));

    await waitFor(() => expect(renderClip).toHaveBeenCalledWith("clip-1", {}));
    expect(renderClip).not.toHaveBeenCalledWith("clip-1", { preset: "tiktok" });
  });

  it("single-flights Burn and exposes a structured caption rejection without early success", async () => {
    const delayed = deferred<{ id: string }>();
    const caption = vi.fn().mockReturnValue(delayed.promise);
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.params = { id: "clip-1" };
    harness.queryData = { getTranscriptDoc: { words: [], segments: [] } };
    harness.ctx = baseCtx({
      sources: [sourceFixture()], clips: [clipFixture()],
      client: clientFixture({ caption, render: renderClip }), pushToast, nav,
    });
    render(<StrictMode><CaptionScreen /></StrictMode>);
    const burn = screen.getByRole("button", { name: "Burn captions" });

    act(() => { burn.click(); burn.click(); });
    expect(caption).toHaveBeenCalledTimes(1);
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();

    await act(async () => {
      delayed.reject(structuredFailure());
      await delayed.promise.catch(() => undefined);
    });
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({
      title: "Caption failed",
      body: expect.stringMatching(/^queue_full:/),
    })));
    expect(renderClip).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("stops the caption chain when its initiating route is left during terminal polling", async () => {
    const terminal = deferred<void>();
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.params = { id: "clip-1" };
    harness.queryData = { getTranscriptDoc: { words: [], segments: [] } };
    harness.ctx = baseCtx({
      sources: [sourceFixture()], clips: [clipFixture()], awaitClipJob,
      client: clientFixture({ caption: vi.fn().mockResolvedValue({ id: "caption-1" }), render: renderClip }),
      pushToast, nav,
    });
    window.history.replaceState({}, "", "/clips/clip-1/caption");
    render(<CaptionScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Burn captions" }));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("caption-1"));
    window.history.pushState({}, "", "/library");

    await act(async () => { terminal.resolve(); await terminal.promise; });

    expect(renderClip).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });
});
