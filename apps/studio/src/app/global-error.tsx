"use client";

/* Last-resort boundary for failures in the root layout itself — must render its own
 * <html>/<body> and can't rely on spool.css (the layout may be what failed). */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "system-ui, sans-serif", background: "#f3f1eb", color: "#211e17", display: "grid", placeItems: "center", height: "100vh" }}>
        <div style={{ textAlign: "center", maxWidth: 420, padding: 24 }}>
          <h2 style={{ fontSize: 20, marginBottom: 8 }}>Spool failed to load</h2>
          <p style={{ color: "#5b5649", fontSize: 14, marginBottom: 20 }}>{error?.message || "A fatal error occurred."}</p>
          <button onClick={reset} style={{ border: 0, borderRadius: 8, padding: "9px 16px", background: "#45556e", color: "#fbfaf6", fontWeight: 600, cursor: "pointer" }}>Reload</button>
        </div>
      </body>
    </html>
  );
}
