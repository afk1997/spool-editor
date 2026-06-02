import { defineConfig, devices } from "@playwright/test";

/* E2E config. Drives the studio UI against a live engine + dev server (assumed already
 * running on :3000 / :8899 — see the e2e spec header). Uses system Chrome (no browser
 * download), one worker, generous timeout for the real download→transcribe→render pipeline. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 360_000,
  expect: { timeout: 15_000 },
  reporter: "line",
  use: {
    baseURL: process.env.SPOOL_STUDIO_URL ?? "http://localhost:3000",
    channel: "chrome",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chrome" } }],
});
