/**
 * @spool/api-client — the one typed client for the engine's JSON API (routes/api_v1.py).
 *
 * Mirrors trove_client.py. Components never call fetch() directly (spec §6.3) — they go
 * through this. Cross-cutting concerns (base URL, bearer auth, error shape) live here once;
 * every method returns a wire type from @spool/types so the studio speaks exactly the shape
 * the engine emits. The progress stream is `subscribeEvents` (SSE).
 */
import type {
  Capabilities,
  ClipJobList,
  ClipJobView,
  DoctorReport,
  EventsSnapshot,
  Health,
  JobList,
  JobView,
  TranscribeJobView,
  TranscriptList,
} from "@spool/types";

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

export interface ListParams {
  status?: string;
  limit?: number;
  offset?: number;
  order?: "newest" | "oldest";
}

export interface MomentsParams {
  mode?: string;
  count?: number;
  window?: [number, number];
}
export interface ReframeParams {
  aspect?: string;
  mode?: string;
  rois?: { left: Roi; right: Roi };
}
export interface Roi {
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface PipelineParams {
  start: number;
  end: number;
  aspect?: string;
  mode?: string;
  style?: string;
  preset?: string;
}

function qs(params: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${k}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
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

  private headers(extra?: HeadersInit): Headers {
    const h = new Headers(extra);
    h.set("Accept", "application/json");
    if (this.token) h.set("Authorization", `Bearer ${this.token}`);
    return h;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await this.fetchImpl(`${this.baseUrl}/api/v1${path}`, {
      ...init,
      headers: this.headers(init.headers),
    });
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
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  private get<T>(path: string): Promise<T> {
    return this.request<T>(path);
  }

  private post<T>(path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method: "POST" };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { "Content-Type": "application/json" };
    }
    return this.request<T>(path, init);
  }

  // ── meta ──
  health(): Promise<Health> {
    return this.get("/health");
  }
  capabilities(): Promise<Capabilities> {
    return this.get("/capabilities");
  }
  doctor(): Promise<DoctorReport> {
    return this.get("/doctor");
  }
  storage(): Promise<Record<string, unknown>> {
    return this.get("/storage");
  }

  // ── downloads (sources) ──
  listJobs(p: ListParams = {}): Promise<JobList> {
    return this.get(`/jobs${qs({ ...p })}`);
  }
  getJob(id: string): Promise<JobView> {
    return this.get(`/jobs/${encodeURIComponent(id)}`);
  }
  submitDownload(input: {
    url: string;
    format?: "video" | "audio";
    auto_transcribe?: boolean;
    title?: string;
  }): Promise<JobView> {
    return this.post("/jobs", input);
  }
  pauseJob(id: string): Promise<JobView> {
    return this.post(`/jobs/${encodeURIComponent(id)}/pause`);
  }
  resumeJob(id: string): Promise<JobView> {
    return this.post(`/jobs/${encodeURIComponent(id)}/resume`);
  }
  cancelJob(id: string): Promise<JobView | void> {
    return this.post(`/jobs/${encodeURIComponent(id)}/cancel`);
  }
  dismissJob(id: string): Promise<void> {
    return this.post(`/jobs/${encodeURIComponent(id)}/dismiss`);
  }

  // ── transcripts ──
  listTranscripts(p: ListParams = {}): Promise<TranscriptList> {
    return this.get(`/transcripts${qs({ ...p })}`);
  }
  getTranscript(id: string): Promise<TranscribeJobView> {
    return this.get(`/transcripts/${encodeURIComponent(id)}`);
  }
  startTranscribe(parentJobId: string): Promise<TranscribeJobView> {
    return this.post(`/jobs/${encodeURIComponent(parentJobId)}/transcribe`);
  }

  // ── clips (the render queue) ──
  findMoments(sourceId: string, p: MomentsParams = {}): Promise<ClipJobView> {
    return this.post(`/sources/${encodeURIComponent(sourceId)}/moments`, p);
  }
  cut(sourceId: string, range: { start: number; end: number }): Promise<ClipJobView> {
    return this.post(`/sources/${encodeURIComponent(sourceId)}/cut`, range);
  }
  reframe(clipId: string, p: ReframeParams = {}): Promise<ClipJobView> {
    return this.post(`/clips/${encodeURIComponent(clipId)}/reframe`, p);
  }
  caption(clipId: string, p: { style?: string } = {}): Promise<ClipJobView> {
    return this.post(`/clips/${encodeURIComponent(clipId)}/captions`, p);
  }
  render(clipId: string, p: { preset?: string; fast?: boolean } = {}): Promise<ClipJobView> {
    return this.post(`/clips/${encodeURIComponent(clipId)}/renders`, p);
  }
  renderPipeline(sourceId: string, p: PipelineParams): Promise<ClipJobView> {
    return this.post(`/sources/${encodeURIComponent(sourceId)}/render`, p);
  }
  listClipJobs(p: ListParams & { kind?: string } = {}): Promise<ClipJobList> {
    return this.get(`/clip-jobs${qs({ ...p })}`);
  }
  getClipJob(id: string): Promise<ClipJobView> {
    return this.get(`/clip-jobs/${encodeURIComponent(id)}`);
  }
  cancelClipJob(id: string): Promise<ClipJobView | void> {
    return this.post(`/clip-jobs/${encodeURIComponent(id)}/cancel`);
  }
  dismissClipJob(id: string): Promise<void> {
    return this.post(`/clip-jobs/${encodeURIComponent(id)}/dismiss`);
  }
  /** Direct URL for a produced render's .mp4 — for `<video src>` / download links.
   *  (Token-auth deployments need a signed URL; the local-first default is unauthenticated.) */
  renderFileUrl(clipId: string, renderId: string): string {
    return `${this.baseUrl}/api/v1/clips/${encodeURIComponent(clipId)}/renders/${encodeURIComponent(renderId)}/file`;
  }

  /**
   * Subscribe to the engine's SSE progress stream. Calls `onSnapshot` with each full
   * `{ts, jobs, transcripts, clips}` frame; returns an unsubscribe. Uses fetch streaming
   * (not EventSource) so the bearer token can ride along. `onError` fires on stream drop —
   * callers typically resubscribe with backoff.
   */
  subscribeEvents(
    onSnapshot: (snap: EventsSnapshot) => void,
    opts: { interval?: number; onError?: (e: unknown) => void } = {},
  ): () => void {
    const ctrl = new AbortController();
    const url = `${this.baseUrl}/api/v1/events${qs({ interval: opts.interval })}`;
    (async () => {
      try {
        const res = await this.fetchImpl(url, {
          headers: this.headers({ Accept: "text/event-stream" }),
          signal: ctrl.signal,
        });
        if (!res.ok || !res.body) throw new SpoolApiError(res.status, "events_unavailable");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buf.indexOf("\n\n")) !== -1) {
            const frame = buf.slice(0, sep);
            buf = buf.slice(sep + 2);
            const data = frame
              .split("\n")
              .filter((l) => l.startsWith("data:"))
              .map((l) => l.slice(5).trimStart())
              .join("\n");
            if (!data) continue;
            try {
              onSnapshot(JSON.parse(data) as EventsSnapshot);
            } catch {
              // ignore a malformed frame; the next snapshot supersedes it
            }
          }
        }
      } catch (e) {
        if (!ctrl.signal.aborted) opts.onError?.(e);
      }
    })();
    return () => ctrl.abort();
  }
}
