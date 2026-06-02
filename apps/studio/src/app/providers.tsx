"use client";

import { EngineProvider } from "@/lib/engine-context";
import { AppShell } from "@/components/app-shell";

/** Client root: the live-data provider + the persistent shell, wrapping every screen. */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <EngineProvider>
      <AppShell>{children}</AppShell>
    </EngineProvider>
  );
}
