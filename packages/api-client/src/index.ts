/**
 * @spool/api-client — the one typed client for the engine's JSON API (routes/api_v1.py).
 *
 * Mirrors trove_client.py. Components never call fetch() directly (spec §6.3) — they go
 * through this. The skeleton below nails the cross-cutting concerns (base URL, bearer
 * auth, error shape); Phase 1 fills out the full surface (ingest, transcripts, discovery,
 * reframe, captions, render, library) and the SSE/WS progress stream.
 */
import type { Job, ServerCapabilities, StorageInfo } from "@spool/types";

export interface SpoolApiOptions {
  /** Engine base URL. Defaults to the localhost dev bind. */
  baseUrl?: string;
  /** TROVE_TOKEN bearer, when the engine is started with auth. */
  token?: string;
  /** Inject a fetch (tests / non-browser runtimes). Defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
}

/** Typed error carrying the engine's machine-readable `error` code and HTTP status. */
export class SpoolApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message?: string,
  ) {
    super(message ?? code);
    this.name = "SpoolApiError";
  }
}

export class SpoolApiClient {
  private readonly baseUrl: string;
  private readonly token?: string;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(opts: SpoolApiOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? "http://127.0.0.1:8899").replace(/\/+$/, "");
    this.token = opts.token;
    this.fetchImpl = opts.fetch ?? globalThis.fetch;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    const res = await this.fetchImpl(`${this.baseUrl}/api/v1${path}`, { ...init, headers });
    if (!res.ok) {
      let code = `http_${res.status}`;
      try {
        const body = (await res.json()) as { error?: string };
        if (body.error) code = body.error;
      } catch {
        // non-JSON error body — keep the http_<status> code
      }
      throw new SpoolApiError(res.status, code);
    }
    return (await res.json()) as T;
  }

  // ── Core read surface (Phase 0 endpoints) ──
  health(): Promise<{ status: string }> {
    return this.request("/health");
  }
  listJobs(): Promise<Job[]> {
    return this.request("/jobs");
  }
  getJob(id: string): Promise<Job> {
    return this.request(`/jobs/${encodeURIComponent(id)}`);
  }
  capabilities(): Promise<ServerCapabilities> {
    return this.request("/capabilities");
  }
  storage(): Promise<StorageInfo> {
    return this.request("/storage");
  }

  // Phase 1 adds: ingest.download/import_file, media.list_sources/get_transcript,
  // discover.find_moments/rank, reframe.*, caption.*, render.export, library.query,
  // plus subscribe() over the SSE/WS progress stream.
}
