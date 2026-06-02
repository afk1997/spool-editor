import type { Metadata } from "next";
import {
  Schibsted_Grotesk,
  Instrument_Serif,
  JetBrains_Mono,
  Archivo_Black,
} from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";

// The approved demo's type system (spec §6.1). Display fonts are preloaded so there's no
// FOUT / "Unpacking…" flash (spec §6.4). Each binds to the CSS var the theme layer reads.
const ui = Schibsted_Grotesk({ subsets: ["latin"], variable: "--font-ui", display: "swap" });
const serif = Instrument_Serif({ subsets: ["latin"], weight: "400", variable: "--font-serif", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });
const caption = Archivo_Black({ subsets: ["latin"], weight: "400", variable: "--font-caption", display: "swap" });

export const metadata: Metadata = {
  title: "Spool",
  description:
    "Local-first clip studio — turn long videos into platform-ready vertical clips, entirely on your machine.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      data-accent="slate"
      data-density="comfortable"
      className={`${ui.variable} ${serif.variable} ${mono.variable} ${caption.variable} h-full antialiased`}
    >
      <body className="bg-bg text-text font-sans min-h-full">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
