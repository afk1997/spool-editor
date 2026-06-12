import { describe, it, expect } from "vitest";
import { captionPage, STYLE_CHUNK } from "@/lib/caption-page";

/* Fixture times in seconds. Pages of 3: A=[0,1,2] on [0.0,1.4), B=[3,4,5] on
 * [2.0,3.4), C=[6] on [4.0,4.5) — gaps between pages are silence. */
const W = (idx: number, start: number | null, end: number | null) => ({ idx, w: `w${idx}`, start, end });
const WORDS = [
  W(0, 0.0, 0.4), W(1, 0.5, 0.9), W(2, 1.0, 1.4),
  W(3, 2.0, 2.4), W(4, 2.5, 2.9), W(5, 3.0, 3.4),
  W(6, 4.0, 4.5),
];
const idxs = (r: { page: { idx: number }[] } | null) => (r ? r.page.map((w) => w.idx) : null);

describe("STYLE_CHUNK", () => {
  it("mirrors the engine preset chunk sizes (ass_captions PRESETS)", () => {
    expect(STYLE_CHUNK).toEqual({ opus: 3, karaoke: 4, minimal: 6 });
  });
});

describe("captionPage", () => {
  it("shows nothing before the first word or with no words", () => {
    expect(captionPage(WORDS, -0.01, 3)).toBeNull();
    expect(captionPage([], 1, 3)).toBeNull();
  });

  it("keeps the page fixed while the highlight advances (no sliding)", () => {
    expect(idxs(captionPage(WORDS, 0.0, 3))).toEqual([0, 1, 2]);
    expect(captionPage(WORDS, 0.0, 3)!.activeInPage).toBe(0);
    expect(idxs(captionPage(WORDS, 0.5, 3))).toEqual([0, 1, 2]);
    expect(captionPage(WORDS, 0.5, 3)!.activeInPage).toBe(1);
    expect(idxs(captionPage(WORDS, 1.2, 3))).toEqual([0, 1, 2]);
    expect(captionPage(WORDS, 1.2, 3)!.activeInPage).toBe(2);
  });

  it("keeps the last-spoken word active through mid-page silence", () => {
    // w0 ends 0.4, w1 starts 0.5 — ASS events tile start→next-start, so w0 stays lit
    const r = captionPage(WORDS, 0.45, 3);
    expect(idxs(r)).toEqual([0, 1, 2]);
    expect(r!.activeInPage).toBe(0);
  });

  it("hides in gaps between pages and after the final page", () => {
    expect(captionPage(WORDS, 1.4, 3)).toBeNull(); // page ends at its last word's end
    expect(captionPage(WORDS, 1.7, 3)).toBeNull();
    expect(captionPage(WORDS, 3.7, 3)).toBeNull();
    expect(captionPage(WORDS, 4.5, 3)).toBeNull();
    expect(captionPage(WORDS, 99, 3)).toBeNull();
  });

  it("swaps to the next page exactly when its first word starts", () => {
    const r = captionPage(WORDS, 2.0, 3);
    expect(idxs(r)).toEqual([3, 4, 5]);
    expect(r!.activeInPage).toBe(0);
  });

  it("is seamless across the boundary when speech is continuous", () => {
    const cont = [W(0, 0.0, 0.5), W(1, 0.5, 1.0), W(2, 1.0, 1.5), W(3, 1.5, 2.0)];
    expect(idxs(captionPage(cont, 1.49, 3))).toEqual([0, 1, 2]);
    expect(idxs(captionPage(cont, 1.5, 3))).toEqual([3]); // no blank frame at the swap
  });

  it("handles a tail page shorter than the chunk", () => {
    const r = captionPage(WORDS, 4.1, 3);
    expect(idxs(r)).toEqual([6]);
    expect(r!.activeInPage).toBe(0);
  });

  it("pages word-by-word at chunk=1 and clamps chunk to >= 1", () => {
    expect(idxs(captionPage(WORDS, 0.5, 1))).toEqual([1]);
    expect(captionPage(WORDS, 0.95, 1)).toBeNull(); // w1 ended, w2 not started
    expect(idxs(captionPage(WORDS, 0.0, 0))).toEqual([0]);
  });

  it("gives a missing/inverted end the engine's 50ms degenerate guard", () => {
    const noEnd = [W(0, 1.0, null)];
    expect(idxs(captionPage(noEnd, 1.0, 3))).toEqual([0]);
    expect(captionPage(noEnd, 1.04, 3)).not.toBeNull();
    expect(captionPage(noEnd, 1.06, 3)).toBeNull();
    const inverted = [W(0, 1.0, 0.5)];
    expect(idxs(captionPage(inverted, 1.0, 3))).toEqual([0]);
    expect(captionPage(inverted, 1.06, 3)).toBeNull();
  });

  it("never activates words with a null start", () => {
    const w = [W(0, 0.0, 0.4), W(1, null, null), W(2, 1.0, 1.4)];
    const r = captionPage(w, 1.0, 3);
    expect(idxs(r)).toEqual([0, 1, 2]);
    expect(r!.activeInPage).toBe(2);
  });
});
