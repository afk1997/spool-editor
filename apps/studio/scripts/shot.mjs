// Screenshot a studio route + capture client errors.
// usage: node scripts/shot.mjs <path> <out.png> [waitMs]
import { chromium } from "playwright";

const route = process.argv[2] || "/";
const out = process.argv[3] || "/tmp/shot.png";
const waitMs = Number(process.argv[4] || 5000);

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("requestfailed", (r) => { const u = r.url(); if (!u.includes("/_next/") && !u.endsWith(".map")) errors.push("requestfailed: " + u + " — " + (r.failure()?.errorText || "")); });

await page.goto("http://localhost:3000" + route, { waitUntil: "domcontentloaded" }).catch((e) => errors.push("goto: " + e.message));
await page.waitForTimeout(waitMs);
await page.screenshot({ path: out, fullPage: false });
console.log(errors.length ? "ERRORS:\n" + errors.join("\n") : "no client errors");
await browser.close();
