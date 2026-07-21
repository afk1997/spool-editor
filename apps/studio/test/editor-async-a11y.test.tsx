import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EventsSnapshot } from "@spool/types";

const harness = vi.hoisted(() => ({
  ctx: null as null | Record<string, unknown>,
  params: { id: "clip-1" },
  snapshot: { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot,
  queryData: {} as Record<string, unknown>,
  queryReload: {} as Record<string, ReturnType<typeof vi.fn>>,
}));

vi.mock("next/navigation", () => ({
  useParams: () => harness.params,
}));

vi.mock("@/lib/engine-context", () => ({
  useLive: () => ({ snapshot: harness.snapshot, connection: "online" }),
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

import EditorScreen from "@/app/clips/[id]/page";
import { AgentPanel } from "@/components/spool/agent";
import { Timeline } from "@/components/spool/timeline";
import { AdjustModal, TranscriptView } from "@/components/spool/work";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const clipFixture = () => ({
  id: "clip-1",
  title: "A clip",
  src: "source-1",
  dur: 10,
  start: 0,
  end: 10,
  aspect: "9:16",
  style: "opus",
  platform: "tiktok",
  status: "ready",
  prog: 100,
  tags: [],
});

const clientFixture = (overrides: Record<string, unknown> = {}) => ({
  clipArtifactUrl: vi.fn().mockReturnValue("https://files.example.test/clip.mp4"),
  renderFileUrl: vi.fn().mockReturnValue("https://files.example.test/render.mp4"),
  caption: vi.fn().mockResolvedValue({ id: "caption-1" }),
  render: vi.fn().mockResolvedValue({ id: "render-1" }),
  cut: vi.fn().mockResolvedValue({ id: "cut-1" }),
  reframe: vi.fn().mockResolvedValue({ id: "preview-1" }),
  editWord: vi.fn().mockResolvedValue(undefined),
  ...overrides,
});

const baseCtx = (overrides: Record<string, unknown> = {}) => ({
  client: clientFixture(),
  sources: [],
  clips: [clipFixture()],
  snapshot: harness.snapshot,
  nav: vi.fn(),
  pushToast: vi.fn(),
  makeClipsFrom: vi.fn().mockResolvedValue(undefined),
  awaitClipJob: vi.fn().mockResolvedValue(undefined),
  agentOpen: true,
  toggleAgent: vi.fn(),
  agentMessages: [],
  working: false,
  askAgent: vi.fn(),
  ...overrides,
});

const brandKit = {
  id: "kit-1",
  name: "Studio kit",
  palette: ["#ffffff"],
  caption_preset: "opus",
  caption_overrides: {},
  watermark: "",
  lower_third: "",
};

const secondBrandKit = {
  ...brandKit,
  id: "kit-2",
  name: "Launch kit",
};

beforeEach(() => {
  harness.params = { id: "clip-1" };
  harness.snapshot = { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot;
  harness.queryData = {};
  harness.queryReload = {};
  harness.ctx = baseCtx();
  window.history.replaceState({}, "", "/clips/clip-1");
});

describe("editor async lifecycle guards", () => {
  it("Brand apply is single-flight and stops before polling when the initiating route is left", async () => {
    const captionResult = deferred<{ id: string }>();
    const caption = vi.fn().mockReturnValue(captionResult.promise);
    const awaitClipJob = vi.fn().mockResolvedValue(undefined);
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.queryData = { listBrandKits: { brand_kits: [brandKit] } };
    harness.ctx = baseCtx({
      client: clientFixture({ caption, render: renderClip }),
      awaitClipJob,
      pushToast,
      nav,
    });

    render(
      <StrictMode>
        <EditorScreen />
      </StrictMode>,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Brand" }));
    fireEvent.click(screen.getByRole("button", { name: "Studio kit" }));
    const apply = screen.getByRole("button", { name: "Apply kit + render" });
    act(() => {
      apply.click();
      apply.click();
    });

    expect(caption).toHaveBeenCalledTimes(1);
    window.history.pushState({}, "", "/library");
    await act(async () => {
      captionResult.resolve({ id: "caption-1" });
      await captionResult.promise;
    });

    expect(awaitClipJob).not.toHaveBeenCalled();
    expect(renderClip).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("Brand apply stops after polling when its Strict Mode view unmounts", async () => {
    const terminal = deferred<void>();
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.queryData = { listBrandKits: { brand_kits: [brandKit] } };
    harness.ctx = baseCtx({
      client: clientFixture({ render: renderClip }),
      awaitClipJob,
      pushToast,
      nav,
    });

    const view = render(
      <StrictMode>
        <EditorScreen />
      </StrictMode>,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Brand" }));
    fireEvent.click(screen.getByRole("button", { name: "Studio kit" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply kit + render" }));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("caption-1"));

    view.unmount();
    await act(async () => {
      terminal.resolve();
      await terminal.promise;
    });

    expect(renderClip).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("keeps Brand apply alive across inspector tabs and blocks normal Render", async () => {
    const terminal = deferred<void>();
    const awaitClipJob = vi.fn().mockReturnValue(terminal.promise);
    const renderClip = vi.fn().mockResolvedValue({ id: "render-1" });
    const makeClipsFrom = vi.fn().mockResolvedValue(undefined);
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.queryData = { listBrandKits: { brand_kits: [brandKit] } };
    harness.ctx = baseCtx({
      client: clientFixture({ render: renderClip }),
      awaitClipJob,
      makeClipsFrom,
      pushToast,
      nav,
    });

    render(<EditorScreen />);
    fireEvent.click(screen.getByRole("tab", { name: "Brand" }));
    fireEvent.click(screen.getByRole("button", { name: "Studio kit" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply kit + render" }));
    await waitFor(() => expect(awaitClipJob).toHaveBeenCalledWith("caption-1"));

    fireEvent.click(screen.getByRole("tab", { name: "Format" }));
    const renderButton = screen.getByRole("button", { name: "Render" });
    expect(renderButton).toBeDisabled();
    fireEvent.click(renderButton);
    expect(makeClipsFrom).not.toHaveBeenCalled();

    await act(async () => {
      terminal.resolve();
      await terminal.promise;
    });

    await waitFor(() => expect(renderClip).toHaveBeenCalledWith("clip-1", { preset: "tiktok" }));
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Applied “Studio kit”" }),
    );
    expect(nav).toHaveBeenCalledWith("queue");
  });

  it("blocks Brand apply while normal Render is pending", async () => {
    const renderResult = deferred<void>();
    const makeClipsFrom = vi.fn().mockReturnValue(renderResult.promise);
    const caption = vi.fn().mockResolvedValue({ id: "caption-1" });
    harness.queryData = { listBrandKits: { brand_kits: [brandKit] } };
    harness.ctx = baseCtx({
      client: clientFixture({ caption }),
      makeClipsFrom,
    });

    render(<EditorScreen />);
    fireEvent.click(screen.getByRole("tab", { name: "Brand" }));
    fireEvent.click(screen.getByRole("button", { name: "Studio kit" }));
    fireEvent.click(screen.getByRole("button", { name: "Render" }));

    const apply = screen.getByRole("button", { name: "Apply kit + render" });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(caption).not.toHaveBeenCalled();

    await act(async () => {
      renderResult.resolve();
      await renderResult.promise;
    });
  });

  it("blocks timeline transcript mutations while normal Render is pending", async () => {
    const renderResult = deferred<void>();
    const makeClipsFrom = vi.fn().mockReturnValue(renderResult.promise);
    const editWord = vi.fn().mockResolvedValue(undefined);
    const cut = vi.fn().mockResolvedValue({ id: "cut-1" });
    harness.queryData = {
      getTranscriptDoc: {
        words: [
          { idx: 0, w: "keep", start: 0, end: 1, deleted: false },
          { idx: 1, w: "drop", start: 2, end: 3, deleted: true },
        ],
        segments: [],
      },
      sourceEnergy: { bars: [], buckets: 0 },
      sourceScenes: { cuts: [] },
      sourceFilmstrip: { strip: null, frames: 0 },
    };
    harness.ctx = baseCtx({
      sources: [{ id: "source-1", transcriptId: "transcript-1" }],
      client: clientFixture({ editWord, cut }),
      makeClipsFrom,
    });

    render(<EditorScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Render" }));

    const deleteWord = screen.getByTitle("delete word (ripple-cut on Re-cut)");
    const recut = screen.getByRole("button", { name: "Re-cut (drop 1)" });
    expect(deleteWord).toBeDisabled();
    expect(recut).toBeDisabled();
    fireEvent.click(deleteWord);
    fireEvent.click(recut);
    expect(editWord).not.toHaveBeenCalled();
    expect(cut).not.toHaveBeenCalled();

    await act(async () => {
      renderResult.resolve();
      await renderResult.promise;
    });
  });

  it("re-cut does not report, navigate, or update local state after the route changes", async () => {
    const cutResult = deferred<{ id: string }>();
    const cut = vi.fn().mockReturnValue(cutResult.promise);
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.queryData = {
      getTranscriptDoc: {
        words: [
          { idx: 0, w: "keep", start: 0, end: 1, deleted: false },
          { idx: 1, w: "drop", start: 2, end: 3, deleted: true },
        ],
        segments: [],
      },
      sourceEnergy: { bars: [], buckets: 0 },
      sourceScenes: { cuts: [] },
      sourceFilmstrip: { strip: null, frames: 0 },
    };
    harness.ctx = baseCtx({
      sources: [{ id: "source-1", transcriptId: "transcript-1" }],
      client: clientFixture({ cut }),
      pushToast,
      nav,
    });

    render(
      <StrictMode>
        <EditorScreen />
      </StrictMode>,
    );
    const recut = screen.getByRole("button", { name: "Re-cut (drop 1)" });
    act(() => {
      recut.click();
      recut.click();
    });
    expect(cut).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Re-cutting…" })).toBeDisabled();

    window.history.pushState({}, "", "/library");
    await act(async () => {
      cutResult.resolve({ id: "cut-1" });
      await cutResult.promise;
    });

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Re-cutting…" })).toBeDisabled();
  });

  it("trim re-cut stops after its editor view unmounts", async () => {
    const cutResult = deferred<{ id: string }>();
    const cut = vi.fn().mockReturnValue(cutResult.promise);
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.queryData = {
      getTranscriptDoc: {
        words: [{ idx: 0, w: "keep", start: 0, end: 1, deleted: false }],
        segments: [],
      },
      sourceEnergy: { bars: [], buckets: 0 },
      sourceScenes: { cuts: [] },
      sourceFilmstrip: { strip: null, frames: 0 },
    };
    harness.ctx = baseCtx({
      sources: [{ id: "source-1", transcriptId: "transcript-1" }],
      client: clientFixture({ cut }),
      pushToast,
      nav,
    });

    const view = render(<EditorScreen />);
    fireEvent.change(screen.getByRole("slider", { name: "trim in" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Re-cut to trim (9s)" }));
    expect(cut).toHaveBeenCalledTimes(1);

    view.unmount();
    await act(async () => {
      cutResult.resolve({ id: "cut-1" });
      await cutResult.promise;
    });

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("treats a cancelled preview as terminal and lets the user retry it", async () => {
    const reframe = vi
      .fn()
      .mockResolvedValueOnce({ id: "preview-1" })
      .mockResolvedValueOnce({ id: "preview-2" });
    harness.ctx = baseCtx({ client: clientFixture({ reframe }) });

    const view = render(<EditorScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview real reframe" }));
    await waitFor(() => expect(reframe).toHaveBeenCalledTimes(1));

    harness.snapshot = {
      ts: 2,
      jobs: [],
      transcripts: [],
      clips: [{ id: "preview-1", status: "cancelled", result: {} }],
    } as unknown as EventsSnapshot;
    view.rerender(<EditorScreen />);

    const retry = screen.getByRole("button", { name: "Preview real reframe" });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await waitFor(() => expect(reframe).toHaveBeenCalledTimes(2));
  });

  it("keeps an accepted preview locally pending while SSE has no matching job", async () => {
    vi.useFakeTimers();
    try {
      const reframe = vi.fn().mockResolvedValue({ id: "preview-1" });
      harness.ctx = baseCtx({ client: clientFixture({ reframe }) });

      render(<EditorScreen />);
      fireEvent.click(screen.getByRole("button", { name: "Split" }));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Preview real reframe" }));
        await Promise.resolve();
      });

      expect(reframe).toHaveBeenCalledTimes(1);
      const pending = screen.getByRole("button", { name: "Rendering real preview…" });
      expect(pending).toBeDisabled();
      pending.click();
      expect(reframe).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(5_000);
      });
      expect(screen.getByRole("button", { name: "Preview real reframe" })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the selected Brand kit stable while apply is pending", async () => {
    const captionResult = deferred<{ id: string }>();
    const caption = vi.fn().mockReturnValue(captionResult.promise);
    harness.queryData = { listBrandKits: { brand_kits: [brandKit, secondBrandKit] } };
    harness.ctx = baseCtx({ client: clientFixture({ caption }) });

    const view = render(<EditorScreen />);
    fireEvent.click(screen.getByRole("tab", { name: "Brand" }));
    const studioKit = screen.getByRole("button", { name: "Studio kit" });
    const launchKit = screen.getByRole("button", { name: "Launch kit" });
    expect(studioKit).toHaveAttribute("aria-pressed", "false");
    expect(launchKit).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(studioKit);
    expect(studioKit).toHaveAttribute("aria-pressed", "true");
    expect(launchKit).toHaveAttribute("aria-pressed", "false");

    const apply = screen.getByRole("button", { name: "Apply kit + render" });
    act(() => {
      apply.click();
      launchKit.click();
    });

    expect(caption).toHaveBeenCalledTimes(1);
    expect(studioKit).toHaveAttribute("aria-pressed", "true");
    expect(launchKit).toHaveAttribute("aria-pressed", "false");
    expect(studioKit).toBeDisabled();
    expect(launchKit).toBeDisabled();

    view.unmount();
    await act(async () => {
      captionResult.resolve({ id: "caption-1" });
      await captionResult.promise;
    });
  });

  it("preview submission does not toast or update editor state after the route is left", async () => {
    const previewResult = deferred<{ id: string }>();
    const reframe = vi.fn().mockReturnValue(previewResult.promise);
    const pushToast = vi.fn();
    harness.ctx = baseCtx({ client: clientFixture({ reframe }), pushToast });

    render(
      <StrictMode>
        <EditorScreen />
      </StrictMode>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview real reframe" }));
    expect(screen.getByRole("button", { name: "Rendering real preview…" })).toBeDisabled();

    window.history.pushState({}, "", "/library");
    await act(async () => {
      previewResult.reject(new Error("preview worker stopped"));
      await previewResult.promise.catch(() => undefined);
    });

    expect(pushToast).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Rendering real preview…" })).toBeDisabled();
  });

  it("render wrapper does not toast or update editor state after the route is left", async () => {
    const renderResult = deferred<void>();
    const makeClipsFrom = vi.fn().mockReturnValue(renderResult.promise);
    const pushToast = vi.fn();
    harness.ctx = baseCtx({ makeClipsFrom, pushToast });

    render(
      <StrictMode>
        <EditorScreen />
      </StrictMode>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Render" }));
    expect(screen.getByRole("button", { name: "Rendering…" })).toBeDisabled();

    window.history.pushState({}, "", "/library");
    await act(async () => {
      renderResult.reject(new Error("render worker stopped"));
      await renderResult.promise.catch(() => undefined);
    });

    expect(pushToast).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Rendering…" })).toBeDisabled();
  });

  it("transcript cut is single-flight and leaves selection/navigation untouched after a route change", async () => {
    const cutResult = deferred<{ id: string }>();
    const cut = vi.fn().mockReturnValue(cutResult.promise);
    const pushToast = vi.fn();
    const nav = vi.fn();
    harness.ctx = baseCtx({ client: clientFixture({ cut }), pushToast, nav });

    render(
      <TranscriptView
        tid="transcript-1"
        sourceId="source-1"
        speakers={{ A: { name: "Speaker A", color: "#fff" } }}
        lines={[
          {
            id: 1,
            sp: "A",
            t: 0,
            words: "Hello world",
            tokens: [
              { idx: 0, w: "Hello", ti: 0, te: 0.5 },
              { idx: 1, w: "world", ti: 0.6, te: 1 },
            ],
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByText("Hello"));
    fireEvent.click(screen.getByText("world"));
    const cutButton = screen.getByRole("button", { name: "Cut clip from selection" });
    act(() => {
      cutButton.click();
      cutButton.click();
    });

    expect(cut).toHaveBeenCalledTimes(1);
    window.history.pushState({}, "", "/queue");
    await act(async () => {
      cutResult.resolve({ id: "cut-1" });
      await cutResult.promise;
    });

    expect(pushToast).not.toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cut clip from selection" })).toBeInTheDocument();
  });
});

describe("editor controls expose their real accessible actions", () => {
  it("does not fabricate Speaker A when timeline words have no diarization", () => {
    render(
      <Timeline
        words={[{ idx: 0, w: "Hello", start: 0, end: 1 }]}
        segments={[]}
        lo={0}
        hi={2}
        cur={0}
        onSeek={vi.fn()}
        onDeleteWord={vi.fn()}
        energyBars={[]}
        sceneCuts={[]}
        filmstrip={null}
        onTrim={vi.fn()}
      />,
    );

    expect(screen.queryByTitle("Speaker A")).not.toBeInTheDocument();
  });

  it("labels the Agent close control", () => {
    harness.ctx = baseCtx({ agentOpen: true });
    render(<AgentPanel />);
    expect(screen.getByRole("button", { name: "Close agent panel" })).toBeInTheDocument();
  });

  it("keeps text-only Agent clarifications answerable and blocks mutation confirmations", () => {
    const answerElicit = vi.fn();
    const clarification = {
      role: "elicit" as const,
      id: "clarify-1",
      kind: "enum" as const,
      q: "Which supplied passage do you mean?",
      options: ["Interview", "Keynote"],
    };
    harness.ctx = baseCtx({
      agentOpen: true,
      answerElicit,
      agentMessages: [
        clarification,
        {
          role: "elicit",
          id: "confirm-1",
          kind: "confirm",
          q: "Allow delete_recipe?",
          options: ["Confirm", "Cancel"],
          confirmFor: { text: "Delete the recipe", tool: "delete_recipe" },
        },
      ],
    });

    render(<AgentPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Keynote" }));
    expect(answerElicit).toHaveBeenCalledWith(clarification, "Keynote");
    expect(screen.getByText("Allow delete_recipe?")).toBeInTheDocument();
    expect(screen.getAllByText(/Agent changes are unavailable in Phase 0/)).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("disables clarification choices while another Agent request is running", () => {
    harness.ctx = baseCtx({
      agentOpen: true,
      working: true,
      agentMessages: [
        {
          role: "elicit",
          id: "clarify-busy",
          kind: "enum",
          q: "Which supplied passage do you mean?",
          options: ["Interview", "Keynote"],
        },
      ],
    });

    render(<AgentPanel />);

    expect(screen.getByRole("button", { name: "Interview" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keynote" })).toBeDisabled();
  });

  it("labels the range-adjustment close control", () => {
    render(
      <AdjustModal
        c={{
          id: "candidate-1",
          source_id: "source-1",
          title: "Candidate",
          start: 0,
          end: 10,
          mode: "Funny",
          why: "A reason",
          excerpt: "",
          signals: [],
          sel: false,
        }}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Close range adjustment" })).toBeInTheDocument();
  });

  it("labels editor playback with the action that the button will perform", () => {
    harness.ctx = baseCtx();
    const view = render(<EditorScreen />);
    expect(screen.getByRole("button", { name: "Play preview" })).toBeInTheDocument();

    fireEvent.play(view.container.querySelector("video")!);
    expect(screen.getByRole("button", { name: "Pause preview" })).toBeInTheDocument();
  });

  it("exposes the selected reframe mode with pressed-state semantics", () => {
    harness.ctx = baseCtx();
    render(<EditorScreen />);

    const pan = screen.getByRole("button", { name: "Pan" });
    const split = screen.getByRole("button", { name: "Split" });
    expect(pan).toHaveAttribute("aria-pressed", "true");
    expect(split).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(split);
    expect(pan).toHaveAttribute("aria-pressed", "false");
    expect(split).toHaveAttribute("aria-pressed", "true");
  });

  it("renders transcript words as pressed-state buttons with an F2 edit path", () => {
    harness.ctx = baseCtx();
    render(
      <TranscriptView
        tid="transcript-1"
        sourceId="source-1"
        speakers={{ A: { name: "Speaker A", color: "#fff" } }}
        lines={[
          {
            id: 1,
            sp: "A",
            t: 0,
            words: "Hello",
            tokens: [{ idx: 0, w: "Hello", ti: 0, te: 0.5 }],
          },
        ]}
      />,
    );

    const token = screen.getByRole("button", { name: /Hello.*F2 to edit/i });
    expect(token).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(token);
    expect(token).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(token, { key: "F2" });
    expect(screen.getByRole("textbox", { name: "Edit transcript word Hello" })).toHaveValue(
      "Hello",
    );
  });
});
