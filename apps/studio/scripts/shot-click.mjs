// Navigate a studio route, click an element by exact text, then screenshot.
// usage: node scripts/shot-click.mjs <path> <clickText> <out.png> [waitMs]
import { chromium } from "playwright";

const route = process.argv[2] || "/";
const clickText = process.argv[3] || "";
const out = process.argv[4] || "/tmp/shot.png";
const waitMs = Number(process.argv[5] || 3000);

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
await page.goto("http://localhost:3000" + route, { waitUntil: "domcontentloaded" }).catch((e) => errors.push("goto: " + e.message));
await page.waitForTimeout(waitMs);
if (clickText) {
  await page.getByText(clickText, { exact: true }).first().click().catch((e) => errors.push("click: " + e.message));
  await page.waitForTimeout(2500);
}
await page.screenshot({ path: out });
console.log(errors.length ? "ERRORS:\n" + errors.join("\n") : "no client errors");
await browser.close();
