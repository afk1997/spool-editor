import { chromium } from "playwright";
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://localhost:3000/sources/c2fa2a9441/discovery", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(3500);
await page.click('button[aria-expanded]', { force: true }).catch(()=>{});
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/studio_discovery_signals.png" });
await browser.close(); console.log("done");
