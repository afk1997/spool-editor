import { test, expect } from "@playwright/test";

/* The Phase-1 acceptance loop, driven through the real UI against the live engine:
 *   paste URL → download + transcribe → find moments → make a 9:16 clip → render.
 *
 * Prereqs (the agent launches these detached): the engine on :8899 (Codex logged in) and
 * `pnpm dev` on :3000. Run with: pnpm --filter @spool/studio e2e
 *
 * The UI performs every action; the engine API is polled only to await async completion and
 * to prove a real 9:16 render artifact exists (the ground truth a screenshot can't assert). */

const ENGINE = process.env.SPOOL_ENGINE_URL ?? "http://127.0.0.1:8899/api/v1";
const VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"; // "Me at the zoo" — short, public

interface DownloadJob { id: string; status: string; progress_pct: number; title?: string }
interface Transcript { id: string; parent_job_id: string; status: string }
interface ClipJob { id: string; kind: string; status: string; source_id: string; clip_id: string; result?: { candidates?: unknown[]; render_id?: string; aspect?: string }; params?: { aspect?: string } }

async function engine<T>(path: string): Promise<T> {
  const res = await fetch(ENGINE + path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

test("paste URL → 9:16 captioned clip, end to end through the UI", async ({ page }) => {
  // 1) Import the URL via the Import screen.
  await page.goto("/import");
  await page.getByPlaceholder(/youtube\.com/i).fill(VIDEO_URL);
  await page.getByRole("button", { name: /^Download$/ }).click();

  // 2) Wait for a source that finished downloading AND transcribing (auto_transcribe).
  let sourceId = "";
  await expect
    .poll(async () => {
      const { jobs } = await engine<{ jobs: DownloadJob[] }>("/jobs");
      const done = jobs.find((j) => j.status === "done" && /zoo/i.test(j.title || ""));
      if (!done) return false;
      const { transcripts } = await engine<{ transcripts: Transcript[] }>("/transcripts");
      if (transcripts.some((t) => t.parent_job_id === done.id && t.status === "done")) { sourceId = done.id; return true; }
      return false;
    }, { timeout: 150_000, intervals: [2000] })
    .toBe(true);

  // 3) Discovery: scan one mode (Funny) through the UI, then wait for real candidates. (The "All"
  //    tab's button is "Scan all modes" = every mode; pick one tab to keep the e2e to one scan.)
  await page.goto(`/sources/${sourceId}/discovery`);
  await page.locator(".seg button", { hasText: "Funny" }).first().click().catch(() => {});
  await page.getByRole("button", { name: /^Find/i }).first().click().catch(() => {});
  await expect
    .poll(async () => {
      const { clip_jobs } = await engine<{ clip_jobs: ClipJob[] }>("/clip-jobs?kind=moments");
      return clip_jobs.some((j) => j.source_id === sourceId && j.status === "done" && (j.result?.candidates || []).length > 0);
    }, { timeout: 120_000, intervals: [2000] })
    .toBe(true);

  // 4) "Make N clips" CUTS + auto-reframes to 9:16 (no burn) and lands on the source's Clips tab
  //    — the review-first flow, not an immediate render.
  await page.reload();
  const makeBtn = page.getByRole("button", { name: /^Make \d+ clips?/i });
  await expect(makeBtn).toBeVisible({ timeout: 30_000 });
  await makeBtn.click();
  await expect(page).toHaveURL(/\/sources\/.*[?&]tab=Clips/, { timeout: 15_000 });

  // 5) Wait for the cut+reframe pipeline job to finish — reframed.mp4 + the 9:16 aspect, NO render
  //    yet (it stopped after reframe). Grab the clip.
  let clipId = "";
  await expect
    .poll(async () => {
      const { clip_jobs } = await engine<{ clip_jobs: ClipJob[] }>("/clip-jobs?kind=pipeline");
      const r = clip_jobs.find((j) => j.status === "done" && j.source_id === sourceId && j.clip_id && !j.result?.render_id);
      if (r) { clipId = r.clip_id; expect(r.result?.aspect).toBe("9:16"); return true; }
      return false;
    }, { timeout: 180_000, intervals: [3000] })
    .toBe(true);

  // 6) Open the clip in the Editor and hit Render — burns captions + exports the final 9:16.
  await page.goto(`/clips/${clipId}`);
  const renderBtn = page.getByRole("button", { name: /^Render$/ });
  await expect(renderBtn).toBeVisible({ timeout: 30_000 });
  await renderBtn.click();

  // 7) Wait for the finished export carrying a render_id for this clip.
  let renderId = "";
  await expect
    .poll(async () => {
      const { clip_jobs } = await engine<{ clip_jobs: ClipJob[] }>("/clip-jobs");
      const r = clip_jobs.find((j) => (j.kind === "export" || j.kind === "pipeline") && j.status === "done" && j.result?.render_id && j.clip_id === clipId);
      if (r && r.result?.render_id) { renderId = r.result.render_id; return true; }
      return false;
    }, { timeout: 240_000, intervals: [3000] })
    .toBe(true);

  // 8) The render artifact is real and fetchable.
  const file = await fetch(`${ENGINE}/clips/${clipId}/renders/${renderId}/file`);
  expect(file.status).toBe(200);
  expect(Number(file.headers.get("content-length") || "0")).toBeGreaterThan(1000);

  // 9) And it surfaces in the Clips library UI.
  await page.goto("/clips");
  await expect(page.locator(".mcard").first()).toBeVisible({ timeout: 30_000 });
});
