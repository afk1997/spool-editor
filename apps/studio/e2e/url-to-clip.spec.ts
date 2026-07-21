import { execFile } from "node:child_process";
import { stat } from "node:fs/promises";
import { promisify } from "node:util";

import {
  expect,
  test,
  type Page,
  type Response as PlaywrightResponse,
} from "@playwright/test";

const execFileAsync = promisify(execFile);

/* Token-on Phase 0 acceptance. Run only through scripts/phase0-e2e.sh, which supplies isolated
 * engine state and a production Studio build. UI actions create every job; authenticated direct
 * reads only await exact IDs and prove that bypassing the Studio without a token is rejected. */

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase();
  if (normalized === "localhost" || normalized === "[::1]") return true;
  const octets = normalized.split(".");
  return octets.length === 4 && octets[0] === "127"
    && octets.every((octet) => /^\d{1,3}$/u.test(octet) && Number(octet) <= 255);
}

function validateEngineApiUrl(value: string | undefined): string {
  if (!value) throw new Error("E2E_ENGINE_API_URL is required; run scripts/phase0-e2e.sh");
  const url = new URL(value);
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !isLoopbackHostname(url.hostname) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname.replace(/\/+$/u, "") !== "/api/v1"
  ) {
    throw new Error("E2E_ENGINE_API_URL must be an HTTP(S) origin ending in /api/v1");
  }
  return value.replace(/\/+$/u, "");
}

function validateStudioOrigin(value: string | undefined): string {
  if (!value) throw new Error("SPOOL_STUDIO_URL is required; run scripts/phase0-e2e.sh");
  const url = new URL(value);
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !isLoopbackHostname(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("SPOOL_STUDIO_URL must be a loopback HTTP(S) origin root");
  }
  return url.origin;
}

const ENGINE = validateEngineApiUrl(process.env.E2E_ENGINE_API_URL?.trim());
const TOKEN = process.env.TROVE_TOKEN?.trim();
if (!TOKEN) throw new Error("TROVE_TOKEN is required; run scripts/phase0-e2e.sh");
const STUDIO_ORIGIN = validateStudioOrigin(process.env.SPOOL_STUDIO_URL?.trim());
const PROXY_PREFIX = "/api/engine/api/v1";
const VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw";

interface TerminalJob {
  id: string;
  status: string;
  error_category?: string | null;
  error_message?: string | null;
}

interface DownloadJob extends TerminalJob {
  title?: string;
  filename?: string | null;
}

interface Transcript extends TerminalJob {
  parent_job_id: string;
}

interface ClipJob extends TerminalJob {
  kind: string;
  source_id: string | null;
  clip_id: string | null;
  result: {
    candidates?: unknown[];
    clip_id?: string;
    render_id?: string;
    aspect?: string;
  };
}

const sleep = (milliseconds: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

async function directResponse(
  path: string,
  authenticated: boolean,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (authenticated) headers.set("Authorization", `Bearer ${TOKEN}`);
  return fetch(`${ENGINE}${path}`, {
    ...init,
    headers,
    signal: init.signal ?? AbortSignal.timeout(15_000),
  });
}

async function engine<T>(path: string): Promise<T> {
  const response = await directResponse(path, true);
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 300);
    throw new Error(`${path} returned ${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

function assertNotFailed(label: string, job: TerminalJob): void {
  if (job.status !== "error" && job.status !== "cancelled") return;
  throw new Error(
    `${label} ${job.id} ${job.status}: ${job.error_category ?? "unknown"}: ${job.error_message ?? "no detail"}`,
  );
}

async function waitForTerminal<T extends TerminalJob>(
  label: string,
  load: () => Promise<T>,
  timeout: number,
): Promise<T> {
  const deadline = Date.now() + timeout;
  for (;;) {
    const job = await load();
    assertNotFailed(label, job);
    if (job.status === "done") return job;
    if (Date.now() >= deadline) {
      throw new Error(`${label} ${job.id} timed out in status ${job.status}`);
    }
    await sleep(2_000);
  }
}

async function waitForTranscript(sourceId: string, timeout: number): Promise<Transcript> {
  const deadline = Date.now() + timeout;
  for (;;) {
    const { transcripts } = await engine<{ transcripts: Transcript[] }>("/transcripts");
    const transcript = transcripts.filter((item) => item.parent_job_id === sourceId).at(-1);
    if (transcript) {
      assertNotFailed("transcript", transcript);
      if (transcript.status === "done") return transcript;
    }
    if (Date.now() >= deadline) {
      throw new Error(`transcript for source ${sourceId} did not complete`);
    }
    await sleep(2_000);
  }
}

function proxyPath(response: PlaywrightResponse): string {
  return new URL(response.url()).pathname;
}

function isProxyResponse(
  response: PlaywrightResponse,
  method: string,
  pathname: string,
): boolean {
  return response.request().method() === method && proxyPath(response) === pathname;
}

function isSettingsPatch(
  response: PlaywrightResponse,
  key: string,
  value: unknown,
): boolean {
  if (!isProxyResponse(response, "PATCH", `${PROXY_PREFIX}/settings`)) return false;
  try {
    const body = response.request().postDataJSON() as Record<string, unknown>;
    return body[key] === value;
  } catch {
    return false;
  }
}

async function admitted<T>(response: PlaywrightResponse): Promise<T> {
  expect(response.ok(), `${response.request().method()} ${response.url()} returned ${response.status()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function rangedMedia(page: Page, path: string, disposition: "attachment" | "inline") {
  const probe = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const rangeRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === path && url.searchParams.get("phase0_range_probe") === probe;
  });
  const result = await page.evaluate(async ({ mediaPath, probeId }) => {
    const url = new URL(mediaPath, window.location.origin);
    url.searchParams.set("phase0_range_probe", probeId);
    const response = await fetch(url, {
      cache: "no-store",
      headers: { Range: "bytes=0-1023" },
    });
    const bytes = await response.arrayBuffer();
    return {
      status: response.status,
      byteLength: bytes.byteLength,
      acceptRanges: response.headers.get("accept-ranges"),
      contentDisposition: response.headers.get("content-disposition"),
      contentLength: response.headers.get("content-length"),
      contentRange: response.headers.get("content-range"),
      contentType: response.headers.get("content-type"),
    };
  }, { mediaPath: path, probeId: probe });

  const browserRequest = await rangeRequest;
  expect(browserRequest.headers().range).toBe("bytes=0-1023");
  expect(browserRequest.headers()["if-range"]).toBeUndefined();

  expect(result.status).toBe(206);
  expect(result.byteLength).toBe(1_024);
  expect(result.acceptRanges).toBe("bytes");
  expect(result.contentLength).toBe("1024");
  expect(result.contentRange).toMatch(/^bytes 0-1023\/\d+$/u);
  expect(result.contentType).toMatch(/^video\//u);
  expect(result.contentDisposition).toMatch(new RegExp(`^${disposition}`, "u"));
  return result;
}

test("token-on URL import reaches a downloadable render only through the Studio proxy", async ({ page }) => {
  const browserEnginePaths: string[] = [];
  const boundaryViolations: string[] = [];
  page.on("request", (browserRequest) => {
    const url = new URL(browserRequest.url());
    if (url.port === "8899") boundaryViolations.push(`browser contacted Flask: ${url.href}`);
    if (!url.pathname.startsWith("/api/engine")) return;
    browserEnginePaths.push(url.pathname);
    if (url.origin !== STUDIO_ORIGIN) boundaryViolations.push(`proxy request changed origin: ${url.href}`);
    if (!url.pathname.startsWith(PROXY_PREFIX)) boundaryViolations.push(`proxy path escaped v1: ${url.pathname}`);
    if (browserRequest.headers().authorization) boundaryViolations.push(`browser exposed Authorization: ${url.pathname}`);
  });

  const capabilities = await engine<{ auth_required: boolean }>("/capabilities");
  expect(capabilities.auth_required).toBe(true);

  const firstSse = page.waitForRequest(
    (browserRequest) => new URL(browserRequest.url()).pathname === `${PROXY_PREFIX}/events`,
    { timeout: 30_000 },
  );

  // Establish explicit privacy state through the UI. Isolated state must start Online + None.
  await page.goto("/settings");
  const sseRequest = await firstSse;
  expect(sseRequest.headers().accept).toBe("text/event-stream");
  await expect(page.getByText("engine connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Privacy", exact: true }).click();

  const offline = page.getByRole("switch", { name: "Offline mode" });
  await expect(offline).toHaveAttribute("aria-checked", "false");
  const codex = page.getByRole("button", { name: "Codex", exact: true });
  await expect(codex).toHaveAttribute("aria-pressed", "false");

  const providerPatch = page.waitForResponse(
    (response) => isSettingsPatch(response, "reasoning_provider", "codex"),
  );
  await codex.click();
  expect((await providerPatch).ok()).toBe(true);
  await expect(codex).toHaveAttribute("aria-pressed", "true");

  const consent = page.getByRole("switch", {
    name: "Allow transcript text to leave this machine for Codex",
  });
  await expect(consent).toHaveAttribute("aria-checked", "false");
  const consentPatch = page.waitForResponse(
    (response) => isSettingsPatch(response, "reasoning_egress_consent", true),
  );
  await consent.click();
  expect((await consentPatch).ok()).toBe(true);
  await expect(consent).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("button", { name: "Privacy: Remote reasoning enabled" })).toBeVisible();

  // Submit the import through Studio and retain the exact UI-created source id.
  await page.goto("/import");
  await expect(page.getByRole("button", { name: "Privacy: Remote reasoning enabled" })).toBeVisible();
  await page.getByPlaceholder(/youtube\.com/i).fill(VIDEO_URL);
  const importResponse = page.waitForResponse(
    (response) => isProxyResponse(response, "POST", `${PROXY_PREFIX}/jobs`),
  );
  const downloadButton = page.getByRole("button", { name: "Download", exact: true });
  await expect(downloadButton).toBeEnabled();
  await downloadButton.click();
  const imported = await admitted<DownloadJob>(await importResponse);
  const sourceId = imported.id;
  expect(sourceId).toBeTruthy();

  const completedDownload = await waitForTerminal(
    "download",
    () => engine<DownloadJob>(`/jobs/${encodeURIComponent(sourceId)}`),
    120_000,
  );
  expect(completedDownload.filename).toBeTruthy();
  await waitForTranscript(sourceId, 120_000);

  // Run one explicit discovery mode, preserve its exact job, then select one candidate.
  await page.goto(`/sources/${encodeURIComponent(sourceId)}/discovery`);
  await page.locator(".seg").first().getByRole("button", { name: /^Funny/u }).click();
  const momentResponse = page.waitForResponse(
    (response) => isProxyResponse(
      response,
      "POST",
      `${PROXY_PREFIX}/sources/${encodeURIComponent(sourceId)}/moments`,
    ),
  );
  await page.getByRole("button", { name: /^Find(?: more)? funny moments$/iu }).first().click();
  const momentAdmission = await admitted<ClipJob>(await momentResponse);
  const momentJob = await waitForTerminal(
    "moment scan",
    () => engine<ClipJob>(`/clip-jobs/${encodeURIComponent(momentAdmission.id)}`),
    120_000,
  );
  expect(momentJob.result.candidates?.length ?? 0).toBeGreaterThan(0);

  const selected = page.getByRole("button", { name: "Selected", exact: true });
  await expect(selected.first()).toBeVisible({ timeout: 30_000 });
  for (let guard = 0; guard < 10 && await selected.count() > 1; guard += 1) {
    await selected.last().click();
  }
  expect(await selected.count()).toBe(1);

  // Start exactly one cut+reframe pipeline through the UI and await that exact admission.
  const pipelineResponse = page.waitForResponse(
    (response) => isProxyResponse(
      response,
      "POST",
      `${PROXY_PREFIX}/sources/${encodeURIComponent(sourceId)}/render`,
    ),
  );
  await page.getByRole("button", { name: /^Make 1 clip/u }).click();
  const pipelineAdmission = await admitted<ClipJob>(await pipelineResponse);
  await expect(page).toHaveURL(/\/sources\/.*[?&]tab=Clips/u);
  const pipeline = await waitForTerminal(
    "cut and reframe pipeline",
    () => engine<ClipJob>(`/clip-jobs/${encodeURIComponent(pipelineAdmission.id)}`),
    180_000,
  );
  expect(pipeline.result.aspect).toBe("9:16");
  expect(pipeline.result.render_id).toBeUndefined();
  const clipId = pipeline.clip_id ?? pipeline.result.clip_id;
  if (!clipId) throw new Error(`pipeline ${pipeline.id} completed without a clip id`);

  // Editor Render performs reframe + caption before admitting the final render; capture that final id.
  await page.goto(`/clips/${encodeURIComponent(clipId)}`);
  const editorVideo = page.locator("video");
  await expect(editorVideo).toBeVisible({ timeout: 30_000 });
  await expect(editorVideo).toHaveAttribute(
    "src",
    new RegExp(`^${PROXY_PREFIX}/clips/${clipId}/artifacts/`, "u"),
  );
  const renderResponse = page.waitForResponse(
    (response) => isProxyResponse(
      response,
      "POST",
      `${PROXY_PREFIX}/clips/${encodeURIComponent(clipId)}/renders`,
    ),
    { timeout: 300_000 },
  );
  await page.getByRole("button", { name: "Render", exact: true }).click();
  const renderAdmission = await admitted<ClipJob>(await renderResponse);
  const renderJob = await waitForTerminal(
    "final render",
    () => engine<ClipJob>(`/clip-jobs/${encodeURIComponent(renderAdmission.id)}`),
    240_000,
  );
  const renderId = renderJob.result.render_id;
  if (!renderId) throw new Error(`render ${renderJob.id} completed without a render id`);

  // A fresh editor load must receive a completed SSE snapshot and surface the render.
  await page.goto(`/clips/${encodeURIComponent(clipId)}`);
  await expect(page.getByText("engine connected", { exact: true })).toBeVisible();
  await expect(page.locator("video")).toHaveAttribute(
    "src",
    `${PROXY_PREFIX}/clips/${encodeURIComponent(clipId)}/renders/${encodeURIComponent(renderId)}/file`,
  );
  const completedVideo = page.locator("video");
  await expect.poll(
    () => completedVideo.evaluate((video: HTMLVideoElement) => video.readyState),
    { message: "final render never loaded playable metadata", timeout: 30_000 },
  ).toBeGreaterThanOrEqual(1);
  const browserMetadata = await completedVideo.evaluate((video: HTMLVideoElement) => ({
    duration: video.duration,
    videoHeight: video.videoHeight,
    videoWidth: video.videoWidth,
  }));
  expect(Number.isFinite(browserMetadata.duration)).toBe(true);
  expect(browserMetadata.duration).toBeGreaterThan(0);
  expect(browserMetadata.videoWidth).toBeGreaterThan(0);
  expect(browserMetadata.videoHeight).toBeGreaterThan(0);
  expect(browserMetadata.videoWidth / browserMetadata.videoHeight).toBeCloseTo(9 / 16, 2);

  const sourceMediaPath = `${PROXY_PREFIX}/jobs/${encodeURIComponent(sourceId)}/file`;
  const artifactPath = `${PROXY_PREFIX}/clips/${encodeURIComponent(clipId)}/artifacts/reframed`;
  const renderMediaPath = `${PROXY_PREFIX}/clips/${encodeURIComponent(clipId)}/renders/${encodeURIComponent(renderId)}/file`;
  await rangedMedia(page, sourceMediaPath, "attachment");
  await rangedMedia(page, artifactPath, "inline");
  const renderRange = await rangedMedia(page, renderMediaPath, "attachment");

  // Exercise the real browser download path, not a Node-side artifact probe.
  await page.getByRole("tab", { name: "Export", exact: true }).click();
  const downloadLink = page.getByRole("link", { name: "Download", exact: true }).last();
  await expect(downloadLink).toHaveAttribute("href", renderMediaPath);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    downloadLink.click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.mp4$/u);
  expect(await download.failure()).toBeNull();
  const downloadedPath = await download.path();
  if (!downloadedPath) throw new Error("Playwright download completed without a local path");
  const totalMatch = /\/(\d+)$/u.exec(renderRange.contentRange ?? "");
  if (!totalMatch) throw new Error(`render range omitted total size: ${renderRange.contentRange}`);
  const downloadedSize = (await stat(downloadedPath)).size;
  expect(downloadedSize).toBe(Number(totalMatch[1]));

  const { stdout: probeOutput } = await execFileAsync("ffprobe", [
    "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "stream=codec_type,width,height:format=duration",
    "-of", "json",
    downloadedPath,
  ], { encoding: "utf8", timeout: 30_000 });
  const probe = JSON.parse(probeOutput) as {
    format?: { duration?: string };
    streams?: Array<{ codec_type?: string; height?: number; width?: number }>;
  };
  const videoStream = probe.streams?.[0];
  expect(videoStream?.codec_type).toBe("video");
  expect(videoStream?.width).toBeGreaterThan(0);
  expect(videoStream?.height).toBeGreaterThan(0);
  expect((videoStream?.width ?? 0) / (videoStream?.height ?? 1)).toBeCloseTo(9 / 16, 2);
  expect(Number(probe.format?.duration)).toBeGreaterThan(0);
  await execFileAsync("ffmpeg", [
    "-nostdin", "-v", "error", "-xerror",
    "-i", downloadedPath,
    "-map", "0:v:0",
    "-f", "null", "-",
  ], { encoding: "utf8", timeout: 120_000 });

  // The same Flask resources are inaccessible when a caller bypasses Studio without the token.
  const unauthenticatedPaths = [
    "/jobs",
    "/events?max_events=1",
    `/jobs/${encodeURIComponent(sourceId)}/file`,
    `/clips/${encodeURIComponent(clipId)}/renders/${encodeURIComponent(renderId)}/file`,
  ];
  for (const path of unauthenticatedPaths) {
    const response = await directResponse(path, false);
    expect(response.status, `unauthenticated ${path}`).toBe(401);
    await response.body?.cancel();
  }

  expect(boundaryViolations).toEqual([]);
  const requiredBrowserPaths = [
    `${PROXY_PREFIX}/events`,
    `${PROXY_PREFIX}/settings`,
    `${PROXY_PREFIX}/jobs`,
    `${PROXY_PREFIX}/sources/${encodeURIComponent(sourceId)}/moments`,
    `${PROXY_PREFIX}/sources/${encodeURIComponent(sourceId)}/render`,
    `${PROXY_PREFIX}/clips/${encodeURIComponent(clipId)}/renders`,
    sourceMediaPath,
    artifactPath,
    renderMediaPath,
  ];
  for (const path of requiredBrowserPaths) {
    expect(browserEnginePaths, `browser never exercised ${path}`).toContain(path);
  }
});
