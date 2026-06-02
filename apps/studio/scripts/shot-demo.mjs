// Navigate the demo SPA to a screen and screenshot it (visual source of truth).
// usage: node scripts/shot-demo.mjs <screen> <out.png> [paramId] [waitMs]
import { chromium } from "playwright";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const here = dirname(fileURLToPath(import.meta.url));
const demo = resolve(here, "../../../docs/Spool (standalone) (1).html");

const screen = process.argv[2] || "home";
const out = process.argv[3] || "/tmp/demo.png";
const paramId = process.argv[4] || "ep42";
const waitMs = Number(process.argv[5] || 1500);

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("file://" + demo, { waitUntil: "load" });
await page.waitForFunction(() => window.__spool, null, { timeout: 5000 }).catch(() => {});
await page.evaluate(([s, id]) => window.__spool && window.__spool.nav(s, { id }), [screen, paramId]);
await page.waitForTimeout(waitMs);
await page.screenshot({ path: out, fullPage: false });
await browser.close();
console.log("demo " + screen + " → " + out);
