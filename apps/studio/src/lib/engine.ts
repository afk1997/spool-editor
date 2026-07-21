import { SpoolApiClient } from "@spool/api-client";

/**
 * The browser knows only the same-origin Studio route. The server-side proxy owns the engine
 * origin and optional bearer, so JSON, SSE, media, and downloads share one credential boundary.
 */
export const engine = new SpoolApiClient({ baseUrl: "/api/engine" });
