"use client";

import { useState } from "react";

/** Seed local inspector state from the live clip, and RE-seed whenever the live value
 * changes — a background job (reframe/recaption while the editor stays mounted) must not
 * leave the inspector stale, or Render submits an outdated aspect/style. The trade-off is
 * deliberate: a finished job overrides an unsubmitted local selection.
 *
 * Uses the "derived state" pattern (two useState + synchronous update during render) so the
 * React Compiler is satisfied: no effects, no refs read/written during render.
 * See: https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes */
export function useClipSeededState(live: string | undefined, fallback: string) {
  const [v, setV] = useState(live ?? fallback);
  const [prevLive, setPrevLive] = useState(live);
  if (live !== undefined && live !== prevLive) {
    setPrevLive(live);
    setV(live);
  }
  return [v, setV] as const;
}
