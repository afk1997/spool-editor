import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SpoolApiError } from "@spool/api-client";
import type { EventsSnapshot } from "@spool/types";
import type { SpoolJob } from "@/components/spool/context";

const harness = vi.hoisted(() => ({
  ctx: null as null | Record<string, unknown>,
}));

vi.mock("@/components/spool/context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/spool/context")>();
  return { ...actual, useSpool: () => harness.ctx };
});

import QueueScreen from "@/app/queue/page";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

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

const queueCtx = (jobs: SpoolJob[], client: Record<string, unknown>, pushToast = vi.fn()) => ({
  jobs,
  client: {
    cancelJob: vi.fn().mockResolvedValue(undefined),
    cancelClipJob: vi.fn().mockResolvedValue(undefined),
    cancelTranscript: vi.fn().mockResolvedValue(undefined),
    dismissJob: vi.fn().mockResolvedValue(undefined),
    dismissClipJob: vi.fn().mockResolvedValue(undefined),
    dismissTranscript: vi.fn().mockResolvedValue(undefined),
    pauseJob: vi.fn().mockResolvedValue(undefined),
    resumeJob: vi.fn().mockResolvedValue(undefined),
    ...client,
  },
  snapshot: { ts: 1, jobs: [], transcripts: [], clips: [] } as unknown as EventsSnapshot,
  nav: vi.fn(),
  pushToast,
});

beforeEach(() => {
  harness.ctx = null;
});

describe("Queue mutation guards", () => {
  it("shows the structured failure code and message in queue logs", () => {
    const failed = {
      ...jobFixture({ status: "failed", err: true, stage: "transcription failed" }),
      errorCode: "transcription_failed",
      errorMessage: "Whisper worker exited.",
    } as SpoolJob;
    harness.ctx = queueCtx([failed], {});
    render(<QueueScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Logs" }));

    expect(
      screen.getByText("error: transcription_failed: Whisper worker exited."),
    ).toBeInTheDocument();
  });

  it("locks a row synchronously across repeated and conflicting actions", async () => {
    const pending = deferred<void>();
    const pauseJob = vi.fn().mockReturnValue(pending.promise);
    const resumeJob = vi.fn().mockResolvedValue(undefined);
    const ctx = queueCtx([jobFixture()], { pauseJob, resumeJob });
    harness.ctx = ctx;
    const { rerender } = render(<QueueScreen />);

    const pause = screen.getByRole("button", { name: "Pause" });
    act(() => {
      pause.click();
      pause.click();
    });

    expect(pauseJob).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Pausing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Pausing…" })).toHaveAttribute("aria-busy", "true");

    ctx.jobs = [jobFixture({ status: "paused" })];
    rerender(<QueueScreen />);
    expect(screen.getByRole("button", { name: "Pausing…" })).toBeDisabled();
    screen.getByRole("button", { name: "Pausing…" }).click();
    expect(resumeJob).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
    expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
  });

  it("locks Pause all and every claimed row until the whole batch settles", async () => {
    const first = deferred<void>();
    const second = deferred<void>();
    const pauseJob = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const pushToast = vi.fn();
    harness.ctx = queueCtx(
      [jobFixture({ id: "download-1" }), jobFixture({ id: "download-2" })],
      { pauseJob },
      pushToast,
    );
    render(<QueueScreen />);

    const pauseAll = screen.getByRole("button", { name: "Pause all" });
    act(() => {
      pauseAll.click();
      pauseAll.click();
    });

    expect(pauseJob).toHaveBeenCalledTimes(2);
    const pausingControls = screen.getAllByRole("button", { name: "Pausing…" });
    expect(pausingControls).toHaveLength(3);
    pausingControls.forEach((control) => {
      expect(control).toBeDisabled();
      expect(control).toHaveAttribute("aria-busy", "true");
    });
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      first.resolve();
      await first.promise;
    });
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      second.resolve();
      await second.promise;
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Pause requests settled",
          body: "2 succeeded · 0 failed",
        }),
      ),
    );
  });

  it("locks Clear finished and its dismiss rows while preserving structured aggregate errors", async () => {
    const delayed = deferred<void>();
    const dismissJob = vi.fn().mockReturnValue(delayed.promise);
    const dismissClipJob = vi
      .fn()
      .mockRejectedValue(new SpoolApiError(503, "unreachable", "offline"));
    const pushToast = vi.fn();
    harness.ctx = queueCtx(
      [
        jobFixture({ id: "download-done", status: "done" }),
        jobFixture({
          id: "clip-failed",
          status: "failed",
          domain: "clip",
          type: "render",
        }),
      ],
      { dismissJob, dismissClipJob },
      pushToast,
    );
    render(<QueueScreen />);

    const clearFinished = screen.getByRole("button", { name: "Clear finished" });
    act(() => {
      clearFinished.click();
      clearFinished.click();
    });

    expect(dismissJob).toHaveBeenCalledTimes(1);
    expect(dismissClipJob).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Clearing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Clearing…" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getAllByRole("button", { name: "Dismissing…" })).toHaveLength(2);
    expect(pushToast).not.toHaveBeenCalled();

    await act(async () => {
      delayed.resolve();
      await delayed.promise;
    });
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Finished-job cleanup settled",
          body: expect.stringMatching(/^1 succeeded · 1 failed · unreachable:/),
        }),
      ),
    );
  });

  it("allows terminal transcript failures to be dismissed individually and by Clear finished", async () => {
    const dismissTranscript = vi.fn().mockResolvedValue(undefined);
    const transcript = jobFixture({
      id: "transcript-failed",
      domain: "transcribe",
      type: "transcribe",
      status: "failed",
      stage: "Whisper worker exited",
      err: true,
    });
    harness.ctx = queueCtx([transcript], { dismissTranscript });
    const view = render(<QueueScreen />);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(dismissTranscript).toHaveBeenCalledWith("transcript-failed"));

    view.unmount();
    dismissTranscript.mockClear();
    harness.ctx = queueCtx([transcript], { dismissTranscript });
    render(<QueueScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Clear finished" }));
    await waitFor(() => expect(dismissTranscript).toHaveBeenCalledWith("transcript-failed"));
  });

  it("allows a queued or running transcript to be cancelled with the row guard", async () => {
    const pending = deferred<void>();
    const cancelTranscript = vi.fn().mockReturnValue(pending.promise);
    harness.ctx = queueCtx(
      [
        jobFixture({
          id: "transcript-running",
          domain: "transcribe",
          type: "transcribe",
          status: "running",
        }),
      ],
      { cancelTranscript },
    );
    render(<QueueScreen />);

    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      cancel.click();
      cancel.click();
    });
    expect(cancelTranscript).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Cancelling…" })).toBeDisabled();

    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
  });
});
