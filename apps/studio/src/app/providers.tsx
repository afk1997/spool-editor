"use client";

import { EngineProvider } from "@/lib/engine-context";
import { SpoolProvider } from "@/components/spool/context";
import { Shell } from "@/components/spool/shell";

/** Client root: live engine data → the demo's useSpool context → the ported shell + screens. */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <EngineProvider>
      <SpoolProvider>
        <Shell>{children}</Shell>
      </SpoolProvider>
    </EngineProvider>
  );
}
