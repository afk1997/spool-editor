"use client";

import { useEffect } from "react";
import Link from "next/link";

/* Route-level error boundary (§6.5: never a blank screen on failure). Renders inside the
 * shell, styled with spool.css; offers retry + home. Self-contained (no context/ui imports)
 * so it works even when the failure is in a screen's data layer. */
export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { console.error("route error:", error); }, [error]);
  return (
    <div className="mainpad fadein">
      <div className="empty">
        <div className="ill" style={{ color: "var(--err)" }}>
          <svg width={32} height={32} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M12 3 2 20h20L12 3ZM12 9v5M12 17h.01" /></svg>
        </div>
        <h3>Something went wrong</h3>
        <p>{error?.message || "An unexpected error occurred while rendering this screen."}{error?.digest ? ` (${error.digest})` : ""}</p>
        <div className="row" style={{ gap: 10, justifyContent: "center" }}>
          <button className="btn primary" onClick={reset}>Try again</button>
          <Link className="btn ghost" href="/">Go home</Link>
        </div>
      </div>
    </div>
  );
}
