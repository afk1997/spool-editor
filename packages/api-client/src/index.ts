/**
 * @spool/api-client — the one typed client for the engine's JSON API (routes/api_v1.py).
 *
 * Mirrors trove_client.py. Components never call fetch() directly (spec §6.3) — they go
 * through this. Cross-cutting concerns (base URL, bearer auth, error shape) live here once;
 * every method returns a wire type from @spool/types so the studio speaks exactly the shape
 * the engine emits. The progress stream is `subscribeEvents` (SSE).
 */
import type {
  AgentResponse,
  Capabilities,
  ClipJobList,
  ClipJobView,
  DoctorReport,
  EventsSnapshot,
  Health,
  JobList,
  JobView,
  TranscribeJobView,
  TranscriptDoc,
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
  /** Fractional ROIs (0–1, resolution-independent); the engine scales to source pixels. */
  rois?: { left: Roi; right: Roi };
  /** S7 tuning knobs — clamped engine-side (min_dwell 0–10s, smoothing 1–121, crop_margin 0–0.5). */
  min_dwell?: number;
  smoothing?: number;
  crop_margin?: number;
  /** A hand-edited speaker track (drag/flip in S7) — rendered verbatim, skipping diar⊕ROI. */
  segments?: TrackSegment[];
  /** Editor preview: render a fast low-res reframe to preview.mp4 (does not touch reframed.mp4). */
  preview?: boolean;
}
export interface Roi {
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface TrackSegment {
  start: number;
  end: number;
  speaker: "left" | "right";
}
/** One library-wide transcript search hit (deep-linkable into the source/transcript). */
export interface TranscriptMatch {
  transcript_id: string;
  title: string;
  snippet: string;
  start_seconds: number;
  end_seconds: number;
}
export interface TranscriptSearchResult {
  matches: TranscriptMatch[];
  returned: number;
  query: string;
}
/** A persisted brand kit (S9) — a reusable look applied across a project's clips on render. */
export interface BrandKit {
  id: string;
  name: string;
  palette?: string[];
  caption_preset?: string;
  caption_overrides?: CaptionOverrides;
  watermark?: string;
  lower_third?: string;
  fonts?: string[];
}
/** A saved end-to-end pipeline (Phase 3) — the reusable decisions that drive render.pipeline +
 *  watch-folder automation (everything except the per-moment start/end). */
export interface Recipe {
  id: string;
  name: string;
  content_mode?: string;      // funny / insightful / hot-take / story / how-to / q&a (discovery)
  count?: number;             // how many moments to find
  aspect?: string;            // 9:16 / 16:9 / 1:1 / 4:5
  reframe_mode?: string;      // pan / split / center
  caption_preset?: string;    // opus / karaoke / minimal
  brand_kit_id?: string;
  platform?: string;          // tiktok / reels / shorts / youtube / linkedin / x
  fast?: boolean;
  weights?: Record<string, number>;   // optional glass-box ranking weights
}
/** A folder/channel/playlist automation (Phase 3) — new videos auto-produce ranked clips per a
 *  recipe into the review queue. The reconciler advances each source seen → pending (awaiting
 *  transcription) → producing (produce job in flight) → produced (that job completed). */
export interface Watch {
  id: string;
  name: string;
  kind: string;               // folder | channel | playlist
  target: string;             // a local folder path, or a channel/playlist URL
  recipe_id?: string;
  enabled?: boolean;
  seen?: string[];
  pending?: Record<string, string>;
  producing?: Record<string, { job: string; attempts: number }>;
  produced?: string[];
}
/** S8 Caption Studio fine-styling — clamped/validated engine-side, mapped to the real ASS. */
export interface CaptionOverrides {
  size?: number;
  outline?: number;
  words?: number;
  weight?: number;
  fill?: string;
  highlight?: string | null;
  position?: number;
  allcaps?: boolean;
  font?: string;
}
/** Writable engine config (demo 07 Settings) — GET/PATCH /settings. fast/preset apply hot;
 *  concurrency + MCP transport apply on the next engine restart. */
export interface EngineSettings {
  fast_default: boolean;
  default_preset: string;
  clip_workers: number;
  max_workers: number;
  mcp_transport: string;
}
/** One whisper model row from GET /models (the Settings → Models management list). */
export interface ModelInfo {
  name: string;
  label: string;
  size_bytes: number;
  stars: number;
  multilingual: boolean;
  is_active: boolean;
  is_installed: boolean;
}
export interface ModelInstallProgress {
  downloading: boolean;
  name?: string;
  received: number;
  total: number;
  error: string | null;
  done: boolean;
}
export interface ModelsList {
  active: string | null;
  models: ModelInfo[];
  install_progress: ModelInstallProgress;
}
export interface PipelineParams {
  start: number;
  end: number;
  aspect?: string;
  mode?: string;
  style?: string;
  preset?: string;
  /** 'reframe' = cut + auto-reframe then STOP (no caption/export) — the "Make clips" path. */
  stop_after?: "reframe";
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
    // Bind to globalThis: native `fetch` throws "Illegal invocation" in browsers when
    // called as a method of another object (i.e. `this.fetchImpl(...)`). Node tolerates it.
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
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
    return this.bodyMethod<T>("POST", path, body);
  }

  private bodyMethod<T>(method: "POST" | "PATCH" | "PUT", path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method };
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
    /** yt-dlp download options (engine maps to --write-subs / --embed-chapters / --embed-metadata+thumbnail) */
    subtitles?: boolean;
    chapters?: boolean;
    embed?: boolean;
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
  /** The full v2 transcript document (segments + words) for the viewer. */
  getTranscriptDoc(id: string): Promise<TranscriptDoc> {
    return this.get(`/transcripts/${encodeURIComponent(id)}/export.json`);
  }
  startTranscribe(parentJobId: string): Promise<TranscribeJobView> {
    return this.post(`/jobs/${encodeURIComponent(parentJobId)}/transcribe`);
  }
  /** Full-text search across every completed transcript in the library — returns matches
   *  with a contextual snippet + the timing range, for deep-linking into the source. */
  searchTranscripts(q: string, opts: { limit?: number; context?: number } = {}): Promise<TranscriptSearchResult> {
    return this.get(`/transcripts/search${qs({ q, limit: opts.limit, context: opts.context })}`);
  }
  /** Edit one transcript word in place (set_text / delete / insert_after / merge_next) —
   *  trove's transcript-editor behavior; drives caption re-burns + the transcript ripple cut. */
  editWord(tid: string, idx: number, p: { op: string; w?: string }): Promise<{ tid: string; word: Record<string, unknown> }> {
    return this.post(`/transcripts/${encodeURIComponent(tid)}/words/${idx}`, p);
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
  caption(clipId: string, p: { style?: string; overrides?: CaptionOverrides; watermark?: string; lower_third?: string; color_speakers?: boolean; emphasis?: boolean; balance_lines?: boolean } = {}): Promise<ClipJobView> {
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
  /** One agent turn: a natural-language message (+ optional source context) → an executed
   *  clip-tool action. Blocks while the LLM plans, so show a thinking state. */
  agent(message: string, opts: { sourceId?: string } = {}): Promise<AgentResponse> {
    return this.post("/agent", { message, source_id: opts.sourceId });
  }

  // ── brand kits (S9) ──
  listBrandKits(): Promise<{ brand_kits: BrandKit[] }> {
    return this.get("/brand-kits");
  }
  createBrandKit(kit: Partial<BrandKit>): Promise<BrandKit> {
    return this.post("/brand-kits", kit);
  }
  updateBrandKit(id: string, kit: Partial<BrandKit>): Promise<BrandKit> {
    return this.bodyMethod("PATCH", `/brand-kits/${encodeURIComponent(id)}`, kit);
  }
  deleteBrandKit(id: string): Promise<void> {
    return this.request(`/brand-kits/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  // ── recipes (Phase 3): saved end-to-end pipelines ──
  listRecipes(): Promise<{ recipes: Recipe[] }> {
    return this.get("/recipes");
  }
  createRecipe(recipe: Partial<Recipe>): Promise<Recipe> {
    return this.post("/recipes", recipe);
  }
  updateRecipe(id: string, recipe: Partial<Recipe>): Promise<Recipe> {
    return this.bodyMethod("PATCH", `/recipes/${encodeURIComponent(id)}`, recipe);
  }
  deleteRecipe(id: string): Promise<void> {
    return this.request(`/recipes/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  // ── watches (Phase 3): folder/channel/playlist automations ──
  listWatches(): Promise<{ watches: Watch[] }> {
    return this.get("/watches");
  }
  createWatch(watch: Partial<Watch>): Promise<Watch> {
    return this.post("/watches", watch);
  }
  updateWatch(id: string, watch: Partial<Watch>): Promise<Watch> {
    return this.bodyMethod("PATCH", `/watches/${encodeURIComponent(id)}`, watch);
  }
  deleteWatch(id: string): Promise<void> {
    return this.request(`/watches/${encodeURIComponent(id)}`, { method: "DELETE" });
  }
  scanWatch(id: string): Promise<{ ingested: string[]; produced: string[]; pending: Record<string, string>; producing: Record<string, { job: string; attempts: number }> }> {
    return this.post(`/watches/${encodeURIComponent(id)}/scan`, {});
  }

  // ── settings + models (demo 07) ──
  getSettings(): Promise<EngineSettings> {
    return this.get("/settings");
  }
  updateSettings(patch: Partial<EngineSettings>): Promise<EngineSettings> {
    return this.bodyMethod("PATCH", "/settings", patch);
  }
  listModels(): Promise<ModelsList> {
    return this.get("/models");
  }
  /** Mark an installed model active — the next transcribe uses it (hot). */
  useModel(name: string): Promise<{ active: string }> {
    return this.post(`/models/${encodeURIComponent(name)}/use`);
  }
  /** Begin downloading + installing a known model; poll listModels() for install_progress. */
  installModel(name: string): Promise<{ name: string; downloading: boolean }> {
    return this.post(`/models/${encodeURIComponent(name)}/install`);
  }

  /** Direct URL for a produced render's .mp4 — for `<video src>` / download links.
   *  (Token-auth deployments need a signed URL; the local-first default is unauthenticated.) */
  renderFileUrl(clipId: string, renderId: string): string {
    return `${this.baseUrl}/api/v1/clips/${encodeURIComponent(clipId)}/renders/${encodeURIComponent(renderId)}/file`;
  }

  /** Stream URL for a source's downloaded file (`/jobs/<id>/file`) — feed an inline <video>. */
  jobFileUrl(jobId: string): string {
    return `${this.baseUrl}/api/v1/jobs/${encodeURIComponent(jobId)}/file`;
  }

  /** Stream URL for a clip's intermediate artifact (cut/reframed/captioned mp4) — for the
   *  editor previews (S6/S7/S8). Final platform renders use `renderFileUrl`. */
  clipArtifactUrl(clipId: string, name: "clip" | "reframed" | "captioned" | "preview"): string {
    return `${this.baseUrl}/api/v1/clips/${encodeURIComponent(clipId)}/artifacts/${name}`;
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
