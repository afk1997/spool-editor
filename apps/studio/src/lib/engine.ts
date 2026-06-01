import { SpoolApiClient } from "@spool/api-client";

/**
 * The studio's singleton engine client. Base URL comes from the environment so the same
 * build can target a local or remote engine; defaults to the localhost dev bind. Components
 * use this — they never call `fetch` directly (spec §6.3).
 */
export const engine = new SpoolApiClient({
  baseUrl: process.env.NEXT_PUBLIC_SPOOL_ENGINE_URL ?? "http://127.0.0.1:8899",
});
