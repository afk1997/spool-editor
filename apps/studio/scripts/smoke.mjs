// Smoke-test every studio route for client errors. Auto-discovers a real source + clip id
// from the live engine so the dynamic routes render against real data.
// usage: node scripts/smoke.mjs   (engine on :8899, studio on :3000)
import { chromium } from "playwright";

const ENGINE = process.env.E2E_ENGINE_API_URL ?? "http://127.0.0.1:8899/api/v1";
const STUDIO = process.env.SPOOL_STUDIO_URL ?? "http://127.0.0.1:3000";
const TOKEN = process.env.TROVE_TOKEN?.trim();
const ENGINE_HEADERS = TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};
const j = async (p) => {
  const response = await fetch(ENGINE + p, {
    headers: ENGINE_HEADERS,
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`${p} returned ${response.status}`);
  return response.json();
};

const { jobs = [] } = await j("/jobs");
const { clip_jobs = [] } = await j("/clip-jobs");
const SRC = (jobs.find((x) => x.status === "done") || jobs[0] || {}).id || "none";
const CLIP = (clip_jobs.find((c) => c.clip_id) || {}).clip_id || "none";

const routes = ["/", "/import", "/library", "/clips", "/queue", "/settings", "/brand", "/publish", "/analytics", "/onboarding",
  `/sources/${SRC}`, `/sources/${SRC}/discovery`, `/clips/${CLIP}`, `/clips/${CLIP}/reframe`, `/clips/${CLIP}/caption`];

const browser = await chromium.launch({ channel: "chrome" });
let bad = 0;
for (const r of routes) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/favicon/.test(m.text())) errs.push("console: " + m.text().slice(0, 120)); });
  await page.goto(STUDIO + r, { waitUntil: "domcontentloaded" }).catch((e) => errs.push("goto: " + e.message));
  await page.waitForTimeout(2500);
  console.log((errs.length ? "✗" : "✓") + " " + r + (errs.length ? "  → " + errs.join(" | ") : ""));
  if (errs.length) bad++;
  await page.close();
}
await browser.close();
console.log("\n" + (bad ? bad + " route(s) with errors" : "ALL ROUTES CLEAN"));
process.exit(bad ? 1 : 0);
