"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useWindowVirtualizer } from "@tanstack/react-virtual";

/* Window virtualization (spec §6.4 — "virtualize long lists"). Mounts only the visible window
 * of a long list while it scrolls with the *page* (not an inner scroll container), so the
 * demo's page-flow layout is unchanged. Row heights are measured (variable-height rows like
 * wrapped transcript lines are fine). Used past a threshold only — short lists render plainly,
 * so the common case (and every screen the demo shows) is byte-identical to before.
 *
 * Each item's own markup is rendered verbatim inside an absolutely-positioned, full-width
 * wrapper, so the rows look identical — only the off-screen ones aren't in the DOM. */
export function WindowList<T>({
  items,
  getKey,
  estimateSize = 52,
  overscan = 12,
  children,
}: {
  items: T[];
  getKey: (item: T, index: number) => string | number;
  estimateSize?: number;
  overscan?: number;
  children: (item: T, index: number) => ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // The list's offset from the document top — read in an effect (never during render) and fed
  // to the window virtualizer so item positions land correctly within the page scroll.
  const [scrollMargin, setScrollMargin] = useState(0);
  useEffect(() => {
    if (ref.current) setScrollMargin(ref.current.offsetTop);
  }, []);

  const v = useWindowVirtualizer({
    count: items.length,
    estimateSize: () => estimateSize,
    overscan,
    getItemKey: (i) => getKey(items[i], i),
    scrollMargin,
  });

  return (
    <div ref={ref} style={{ position: "relative", height: v.getTotalSize(), width: "100%" }}>
      {v.getVirtualItems().map((vi) => (
        <div
          key={vi.key}
          data-index={vi.index}
          ref={v.measureElement}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            transform: `translateY(${vi.start - scrollMargin}px)`,
          }}
        >
          {children(items[vi.index], vi.index)}
        </div>
      ))}
    </div>
  );
}
