import { defineConfig, devices } from "@playwright/test";

function requiredStudioUrl(): string {
  const value = process.env.SPOOL_STUDIO_URL?.trim();
  if (!value) throw new Error("SPOOL_STUDIO_URL is required; run scripts/phase0-e2e.sh");
  const url = new URL(value);
  const hostname = url.hostname.toLowerCase();
  const octets = hostname.split(".");
  const ipv4Loopback =
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((octet) => /^\d{1,3}$/u.test(octet) && Number(octet) <= 255);
  const loopback = hostname === "localhost" || hostname === "[::1]" || ipv4Loopback;
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !loopback ||
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

/* The checked-in phase0-e2e.sh harness owns the isolated token-enabled engine and production
 * Studio server. Playwright only drives that explicit pair: no implicit webServer can race it. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 1_800_000,
  expect: { timeout: 15_000 },
  reporter: "line",
  use: {
    baseURL: requiredStudioUrl(),
    channel: "chrome",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chrome" } }],
});
