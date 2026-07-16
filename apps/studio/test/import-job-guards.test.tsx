import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SpoolApiError } from "@spool/api-client";
import type { SpoolDownload } from "@/components/spool/context";

const harness = vi.hoisted(() => ({
  ctx: null as null | Record<string, unknown>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/components/spool/context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/spool/context")>();
  return { ...actual, useSpool: () => harness.ctx };
});

import ImportPage from "@/app/import/page";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const downloadFixture = (overrides: Partial<SpoolDownload> = {}): SpoolDownload => ({
  id: "download-1",
  title: "Test download",
  src: "youtube",
  prog: 25,
  status: "downloading",
  size: "10 MB",
  speed: "1 MB/s",
  eta: "10s",
  ...overrides,
});

const importCtx = (downloads: SpoolDownload[], client: Record<string, unknown>) => ({
  downloads,
  client: {
    submitDownload: vi.fn().mockResolvedValue({ id: "new-download" }),
    pauseJob: vi.fn().mockResolvedValue(undefined),
    resumeJob: vi.fn().mockResolvedValue(undefined),
    ...client,
  },
  pushToast: vi.fn(),
  nav: vi.fn(),
});

beforeEach(() => {
  harness.ctx = null;
});

describe("Import download mutation guards", () => {
  it("locks a download synchronously across duplicate Pause clicks and an early paused snapshot", async () => {
    const pending = deferred<void>();
    const pauseJob = vi.fn().mockReturnValue(pending.promise);
    const resumeJob = vi.fn().mockResolvedValue(undefined);
    const ctx = importCtx([downloadFixture()], { pauseJob, resumeJob });
    harness.ctx = ctx;
    const { rerender } = render(<ImportPage />);

    const pause = screen.getByRole("button", { name: "Pause download" });
    act(() => {
      pause.click();
      pause.click();
    });

    expect(pauseJob).toHaveBeenCalledTimes(1);
    const pausing = screen.getByRole("button", { name: "Pausing download…" });
    expect(pausing).toBeDisabled();
    expect(pausing).toHaveAttribute("aria-busy", "true");

    ctx.downloads = [downloadFixture({ status: "paused" })];
    rerender(<ImportPage />);
    screen.getByRole("button", { name: "Pausing download…" }).click();
    expect(resumeJob).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
    expect(screen.getByRole("button", { name: "Resume download" })).toBeEnabled();
  });

  it("locks duplicate Resume clicks and restores the structured failure after settlement", async () => {
    const pending = deferred<void>();
    const pauseJob = vi.fn().mockResolvedValue(undefined);
    const resumeJob = vi.fn().mockReturnValue(pending.promise);
    harness.ctx = importCtx([downloadFixture({ status: "paused" })], { pauseJob, resumeJob });
    render(<ImportPage />);

    const resume = screen.getByRole("button", { name: "Resume download" });
    act(() => {
      resume.click();
      resume.click();
    });

    expect(resumeJob).toHaveBeenCalledTimes(1);
    const resuming = screen.getByRole("button", { name: "Resuming download…" });
    expect(resuming).toBeDisabled();
    expect(resuming).toHaveAttribute("aria-busy", "true");
    expect(pauseJob).not.toHaveBeenCalled();

    await act(async () => {
      pending.reject(new SpoolApiError(409, "not_resumable", "resume token expired"));
      await pending.promise.catch(() => undefined);
    });

    await waitFor(() => expect(screen.getByText("not_resumable")).toBeInTheDocument());
    expect(screen.getByText(/cannot be resumed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume download" })).toBeEnabled();
  });
});
