import { chromium } from "playwright";
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = []; page.on("pageerror", e => errs.push(e.message));
await page.goto("http://localhost:3000/", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);
// type a URL into the Home box, then click Import / Paste URL
await page.getByPlaceholder(/Tell the agent/i).fill("https://www.youtube.com/watch?v=jNQXAC9IVRw");
await page.getByRole("button", { name: /Import \/ Paste URL/i }).click({ force: true });
await page.waitForTimeout(1500);
const onImport = page.url();
const textareaVal = await page.locator("textarea").first().inputValue().catch(() => "(no textarea)");
console.log("landed on:", onImport);
console.log("textarea value:", JSON.stringify(textareaVal));
await page.screenshot({ path: "/tmp/studio_importflow.png" });
console.log(errs.length ? "ERR: " + errs.join(" | ") : "no client errors");
await browser.close();
