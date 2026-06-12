# Paged karaoke captions in the editor live preview — design

**Date:** 2026-06-12
**Status:** approved (scope confirmed in session; user: "yes do it")

## Problem

In the clip editor's live preview, captions are unreadable: each spoken word immediately
pushes a new word into the line, shifting every word's position
(`apps/studio/src/app/clips/[id]/page.tsx` — `lineStart = Math.max(0, activeIdx - 2)` is a
sliding window anchored to the active word).

The **burned render already behaves correctly**: `engine/clip/backhalf/ass_captions.py`
emits one ASS Dialogue event per word where the event text is the *entire chunk* and only
the active word's color changes. A fixed group appears at once, the highlight transfers
word-by-word, and the group swaps only when its last word ends. Verified by running the
generator on sample words.

So the preview lies about the render. The fix makes the preview follow the same paging
rule, for **all caption styles, as the only behavior** (user requirement — no toggle).

## Goals

- Editor live preview pages captions identically to the burned output:
  1. A page of N words appears all at once and never shifts.
  2. The highlight moves to each word as it is spoken.
  3. The next page appears only after the current page's last word ends.
- Behavior applies to every style: opus (3 words/page), karaoke (4), minimal (6) —
  the same chunk sizes as the engine presets.
- Pure, unit-tested paging logic.

## Non-goals

- Engine changes: the ASS generator already pages for all styles. Untouched.
- Caption Studio changes: its preview already pages (fixed `wpl` slices on a demo ticker).
- Pixel fidelity of the overlay (font/size/outline are an approximation today and stay so).
- Window-edge parity for words straddling the clip boundary (pre-existing minor divergence:
  engine includes overlap, preview filters by `start ∈ [lo, hi]`). Out of scope.
- The editor's Render button burns style *defaults*; a custom "Words / line" set in Caption
  Studio applies only to Caption Studio burns. Preview uses the defaults the editor renders with.

## Design

### 1. Pure helper — `apps/studio/src/lib/caption-page.ts` (new)

```ts
export const STYLE_CHUNK: Record<string, number> = { opus: 3, karaoke: 4, minimal: 6 };
// mirrors engine PRESETS chunk sizes (engine/clip/backhalf/ass_captions.py)

export interface CaptionWord { idx: number; w: string; start: number | null; end: number | null }

/** Paged-karaoke caption state at time t (same timebase as the words' start/end).
 *  Mirrors the engine's build_chunks/build_events semantics:
 *  - pages are fixed `chunk`-size slices of `words` from index 0
 *  - a page is visible from its first word's start to its last word's end
 *  - the active word is the last word in the page with start <= t
 *    (it stays active through mid-page silence — ASS events tile start→next start)
 *  - returns null before the first word and in gaps between pages (render shows nothing)
 */
export function captionPage(words: CaptionWord[], t: number, chunk: number):
  { page: CaptionWord[]; activeInPage: number } | null
```

Algorithm: find `activeIdx` = last index with non-null `start <= t` (words are
transcript-ordered). If none → `null`. `pageStart = floor(activeIdx / chunk) * chunk`,
`page = words.slice(pageStart, pageStart + chunk)`. Page end = the last word's `end`,
guarded like the engine's degenerate-segment rule: when `end` is null or `<= start`, use
`start + 0.05`. If `t >= pageEnd` and the active word is the page's last word → `null`
(inter-page gap / after final page). Else return the page and
`activeInPage = activeIdx - pageStart`. `chunk` is clamped to ≥ 1.

### 2. Editor wiring — `apps/studio/src/app/clips/[id]/page.tsx` (edit)

Replace the sliding-window block (currently lines 169–173) with:

```ts
const pageRes = captionPage(tlWords, lo + cur, STYLE_CHUNK[style] ?? 3);
const capLine = pageRes?.page ?? [];
const activeWordIdx = pageRes ? pageRes.page[pageRes.activeInPage]?.idx : undefined;
```

`cur` is clip-relative (video time); words carry source-time stamps, hence `lo + cur`.
The overlay JSX (`capLine.length > 0 && …`) renders unchanged — same fonts, colors,
highlight map per style (minimal keeps no color highlight, matching the burn).

### 3. Tests — `apps/studio/test/caption-page.test.ts` (new)

Vitest, pure-function tests (existing `helpers.test.ts` pattern):
- before the first word → null
- at/after first word start → full first page, active 0; page slice IDENTICAL while the
  highlight advances (no position drift — the regression under test)
- mid-page silence → previous word stays active, page stays visible
- page swap exactly at the next chunk's first word start
- gap between pages (t past page end, before next page) → null; after final page → null
- tail page shorter than chunk; chunk = 1 (single-word pages); null `end` fallback
- STYLE_CHUNK matches engine preset chunk sizes (3/4/6)

## Error handling

- Words with null `start` are never active (skipped by the scan, consistent with today).
- Null/inverted `end` on the page's last word → `start + 0.05` (engine's degenerate guard),
  so the page never flash-hides while its last word is being spoken.
- Empty `tlWords` / no transcript → helper returns null → overlay hidden (as today).

## Implementation & review process (user-requested)

Fable orchestrates and reviews; Opus/Sonnet subagents write the code (TDD: failing tests
first), then an independent review pass checks parity against the engine's event tiling,
then Fable does the final review + full vitest run before committing.
