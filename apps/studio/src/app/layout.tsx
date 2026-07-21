import type { Metadata } from "next";
import "./spool.css"; // the demo's verbatim design — the single source of truth (tokens + components + resets + bundled @font-face)
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Spool",
  description:
    "Local-first clip studio for platform-ready vertical clips, with optional consented Codex reasoning.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-accent="slate" data-density="compact" className="antialiased">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
