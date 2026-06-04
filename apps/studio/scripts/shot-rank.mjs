// Screenshot the Phase-3 glass-box ranking UI on Discovery (real ranked candidates).
// usage: node scripts/shot-rank.mjs [sourceId]
import { chromium } from "playwright";

const sid = process.argv[2] || "032df8e8e5";
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(`http://localhost:3000/sources/${sid}/discovery`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);
await page.screenshot({ path: "/tmp/rank_1_cards.png" });

// 1) Open the reweight panel (Rank by score)
await page.getByText("Rank by score").click({ force: true }).catch((e) => errors.push("rank-btn: " + e.message));
await page.waitForTimeout(800);
await page.screenshot({ path: "/tmp/rank_2_reweight.png" });

// 2) Drag the Energy slider up to reorder, then screenshot
const energy = page.locator('input[aria-label="Energy weight"]');
await energy.fill("5").catch((e) => errors.push("energy-slider: " + e.message));
await page.waitForTimeout(600);
await page.screenshot({ path: "/tmp/rank_3_energy_reweighted.png" });

// 3) Expand a card's score chip → the named factor bars
await page.locator('button[aria-expanded]').first().click({ force: true }).catch((e) => errors.push("expand: " + e.message));
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/rank_4_factors_expanded.png" });

console.log(errors.length ? "ERRORS:\n" + errors.join("\n") : "no client errors");
await browser.close();
