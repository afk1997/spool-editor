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
