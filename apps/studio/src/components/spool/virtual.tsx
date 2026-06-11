"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

/* List virtualization (spec §6.4 — "virtualize long lists"). The studio shell scrolls
 * inside `.main` (overflow-y: auto) — NOT the window — so this must be an ELEMENT
 * virtualizer. The previous window virtualizer watched a scroll that never happened and
 * mounted only the first viewport: every long transcript was unreachable past the fold.
 * Rows are measured (variable heights fine) and rendered verbatim in absolutely-
 * positioned full-width wrappers. Falls back to the document scroller when no `.main`
 * ancestor exists (tests, future standalone layouts). */
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
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);
  // The list's offset within the scroller — read in an effect (never during render) so
  // item positions land correctly when content precedes the list.
  const [scrollMargin, setScrollMargin] = useState(0);
  useEffect(() => {
    const el =
      (ref.current?.closest(".main") as HTMLElement | null) ??
      (document.scrollingElement as HTMLElement | null);
    setScrollEl(el);
    if (ref.current && el) {
      setScrollMargin(
        ref.current.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop,
      );
    }
  }, []);

  const v = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollEl,
    estimateSize: () => estimateSize,
    overscan,
    getItemKey: (i) => getKey(items[i]!, i),
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
          {children(items[vi.index]!, vi.index)}
        </div>
      ))}
    </div>
  );
}
