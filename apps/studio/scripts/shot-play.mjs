import { chromium } from "playwright";
const CID="2ba809d276", SRC="c20558e40d";
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = []; page.on("pageerror", e => errs.push(e.message));
async function probe(url, label) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(5000);
  const vCount = await page.locator("video").count();
  if (!vCount) { const t=(await page.locator("body").innerText()).replace(/\s+/g," ").slice(-90); console.log(label, "→ NO <video> · ...", JSON.stringify(t)); return; }
  const v = page.locator("video").first();
  const played = await v.evaluate(async (el) => { try { el.muted = true; await el.play(); await new Promise(r => setTimeout(r, 1800)); return { currentTime:+el.currentTime.toFixed(2), readyState:el.readyState, dims:el.videoWidth+"x"+el.videoHeight, err:el.error?.message ?? null }; } catch (e) { return { error:String(e) }; } });
  console.log(label, "→", JSON.stringify(played), played.currentTime>0 ? "✓ PLAYING" : "✗ not advancing");
}
await probe("http://localhost:3000/clips/"+CID, "EDITOR render ("+CID+")");
await probe("http://localhost:3000/sources/"+SRC, "PROJECT source ("+SRC+")");
console.log(errs.length ? "ERR: "+errs.join(" | ") : "no client errors");
await browser.close();
