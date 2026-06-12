# Paged Karaoke Captions (Editor Live Preview) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The clip editor's live caption overlay pages exactly like the burned render — a fixed group of words on screen, highlight transferring word-by-word, page swapping only when its last word ends — for every caption style, as the only behavior.

**Architecture:** One new pure helper (`captionPage`) in `apps/studio/src/lib/` mirrors the engine's ASS chunking semantics (`engine/clip/backhalf/ass_captions.py`: fixed chunk-size pages, page visible `[first.start, last.end)`, active word = last with `start <= t`). The editor screen swaps its sliding-window math for this helper. Engine and Caption Studio are untouched (they already page).

**Tech Stack:** TypeScript, Next.js (app dir), Vitest (jsdom, `@/` → `apps/studio/src`). Spec: `docs/superpowers/specs/2026-06-12-paged-karaoke-captions-design.md`.

**Conventions:** Run all commands from the repo root. Commit on the current branch (`main`, the project's working branch). Match the codebase's compact comment style.

---

### Task 1: `captionPage` pure helper (TDD)

**Files:**
- Test: `apps/studio/test/caption-page.test.ts` (create)
- Create: `apps/studio/src/lib/caption-page.ts`

- [ ] **Step 1: Write the failing test**

Create `apps/studio/test/caption-page.test.ts` with exactly:

```ts
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pnpm --filter @spool/studio test test/caption-page.test.ts`
Expected: FAIL — cannot resolve `@/lib/caption-page` (module does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `apps/studio/src/lib/caption-page.ts` with exactly:

```ts
/** Chunk size per caption style — mirrors the engine presets
 * (engine/clip/backhalf/ass_captions.py PRESETS[].chunk). */
export const STYLE_CHUNK: Record<string, number> = { opus: 3, karaoke: 4, minimal: 6 };

export interface TimedWord {
  start: number | null;
  end: number | null;
}

/** Paged-karaoke caption state at time `t` (same timebase as the words).
 *
 * Mirrors the burned-ASS behavior (ass_captions build_chunks/build_events): pages are
 * fixed `chunk`-size slices of `words` from index 0; a page is on screen from its first
 * word's start to its last word's end; within it the active word is the last one whose
 * start <= t (ASS events tile start→next-start, so a word stays lit through mid-page
 * silence); nothing shows before the first word or in gaps between pages. A missing or
 * inverted end gets the engine's 50ms degenerate-segment guard.
 */
export function captionPage<W extends TimedWord>(
  words: W[],
  t: number,
  chunk: number,
): { page: W[]; activeInPage: number } | null {
  const size = Math.max(1, Math.floor(chunk));
  let active = -1;
  for (let i = 0; i < words.length; i++) {
    const s = words[i]!.start;
    if (s == null) continue;
    if (s > t) break;
    active = i;
  }
  if (active < 0) return null;
  const pageStart = Math.floor(active / size) * size;
  const page = words.slice(pageStart, pageStart + size);
  const last = page[page.length - 1]!;
  const lastStart = last.start ?? t;
  const pageEnd = last.end != null && last.end > lastStart ? last.end : lastStart + 0.05;
  if (t >= pageEnd && active === pageStart + page.length - 1) return null;
  return { page, activeInPage: active - pageStart };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm --filter @spool/studio test test/caption-page.test.ts`
Expected: PASS — 11 tests (2 describe blocks), 0 failures.

- [ ] **Step 5: Commit**

```bash
git add apps/studio/src/lib/caption-page.ts apps/studio/test/caption-page.test.ts
git commit -m "feat(studio): captionPage helper — paged karaoke math mirroring the engine's ASS chunking"
```

---

### Task 2: Wire the editor overlay to `captionPage`

**Files:**
- Modify: `apps/studio/src/app/clips/[id]/page.tsx` (import block at top; caption math at lines 168–173)

- [ ] **Step 1: Add the import**

In `apps/studio/src/app/clips/[id]/page.tsx`, directly after the line:

```ts
import { useClipSeededState } from "@/lib/use-clip-seeded-state";
```

add:

```ts
import { captionPage, STYLE_CHUNK } from "@/lib/caption-page";
```

- [ ] **Step 2: Replace the sliding-window math**

In the same file, find this block (inside `EditorBody`, after the `previewFit` line):

```ts
  const hl = ({ opus: "var(--caption-hl)", karaoke: "#37E2A0", minimal: "#ffffff" } as Record<string, string>)[style] || "var(--caption-hl)";
  let activeIdx = -1;
  for (let i = 0; i < tlWords.length; i++) { if (((tlWords[i]!.start ?? lo) - lo) <= cur) activeIdx = i; else break; }
  const lineStart = Math.max(0, activeIdx - 2);
  const capLine = tlWords.slice(lineStart, lineStart + 6);
  const activeWordIdx = tlWords[activeIdx]?.idx;
```

and replace it with:

```ts
  const hl = ({ opus: "var(--caption-hl)", karaoke: "#37E2A0", minimal: "#ffffff" } as Record<string, string>)[style] || "var(--caption-hl)";
  // Paged karaoke, mirroring the burn: the page stays fixed while the highlight
  // transfers word-by-word, and swaps only after its last word ends (words are in
  // source time; `cur` is clip-relative, hence lo + cur).
  const capPage = captionPage(tlWords, lo + cur, STYLE_CHUNK[style] ?? 3);
  const capLine = capPage?.page ?? [];
  const activeWordIdx = capPage?.page[capPage.activeInPage]?.idx;
```

The overlay JSX below (`capLine.length > 0 && …`, `w.idx === activeWordIdx`) is untouched — `capLine` is still a slice of `tlWords`, and an empty page hides the overlay exactly as before.

- [ ] **Step 3: Typecheck, run the full suite, lint**

Run: `pnpm --filter @spool/studio typecheck && pnpm --filter @spool/studio test && pnpm --filter @spool/studio lint`
Expected: typecheck clean; all vitest files pass (existing suites + `caption-page.test.ts`); lint clean.

- [ ] **Step 4: Commit**

```bash
git add "apps/studio/src/app/clips/[id]/page.tsx"
git commit -m "fix(studio): editor caption overlay pages like the burn — no more per-word sliding"
```

---

## Verification (orchestrator, after both tasks)

1. `pnpm --filter @spool/studio typecheck && pnpm --filter @spool/studio test` — green.
2. Review the diff against the spec: paging math identical to `ass_captions.build_chunks` /
   `build_events` (fixed slices; `[first.start, last.end)` visibility; last-with-`start<=t`
   active; 50ms degenerate guard), all styles routed through `STYLE_CHUNK`, no engine or
   Caption Studio changes.
3. Process note (user-requested): Opus/Sonnet subagents implement; Fable reviews each task's
   diff before the next task starts.
