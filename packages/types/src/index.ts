/**
 * @spool/types — the shared data model.
 *
 * Mirrors the Python engine's records (spec §3, "Data model"). The JSON API, the typed
 * REST client, the MCP client, and the studio all import from here so there is exactly
 * one source of truth for shapes. Keep in sync with the engine; the API is the contract.
 */

// ───────────────────────── Jobs (trove) ─────────────────────────

export type JobStatus = "queued" | "running" | "paused" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  url: string;
  title: string;
  status: JobStatus;
  thumbnail?: string;
  filePath?: string;
  filename?: string;
  downloadedBytes: number;
  totalBytes: number;
  speed?: number;
  eta?: number;
  fragmentIndex?: number;
  fragmentCount?: number;
  formatChoice?: string;
  formatId?: string;
  autoTranscribe: boolean;
  errorCategory?: string;
}

export type TranscribeStatus = "queued" | "running" | "done" | "error" | "cancelled";
export type DiarizationStatus = "complete" | "empty" | "failed" | null;

export interface TranscribeJob {
  id: string;
  parentJobId: string;
  status: TranscribeStatus;
  progress: number;
  durationSeconds?: number;
  languageDetected?: string;
  diarizationStatus?: DiarizationStatus;
  speakerCount?: number;
  errorCategory?: string;
}

// ───────────────────────── Transcript ─────────────────────────

export interface Word {
  start: number;
  end: number;
  word: string;
  speaker?: string;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker?: string;
  words: Word[];
}

export interface Transcript {
  sourceId: string;
  language?: string;
  segments: TranscriptSegment[];
  editedAt?: string;
}

// ───────────────────────── Source / library ─────────────────────────

export type SourceStatus = "downloading" | "transcribing" | "ready" | "error";

export interface Source {
  id: string;
  title: string;
  durationSeconds?: number;
  origin: "file" | "url";
  url?: string;
  thumbnail?: string;
  createdAt: string;
  status: SourceStatus;
  clipCount: number;
}

// ───────────────────────── Discovery ─────────────────────────

export type ContentMode = "funny" | "insightful" | "hot-take" | "story" | "how-to" | "qa";

/** Named, reweightable factors — the glass-box score, never an opaque 0–99 (spec §5.4). */
export interface CandidateSignals {
  hook?: number;
  selfContained?: number;
  arc?: number;
  audioEnergy?: number;
  lengthFit?: number;
}

export interface Candidate {
  id: string;
  sourceId: string;
  start: number;
  end: number;
  title: string;
  rationale: string;
  mode: ContentMode;
  signals: CandidateSignals;
  score?: number;
}

// ───────────────────────── Clip · reframe · caption · render ─────────────────────────

export interface Clip {
  id: string;
  sourceId: string;
  candidateId?: string;
  start: number;
  end: number;
}

export type Aspect = "9:16" | "16:9" | "1:1" | "4:5";
export type ReframeMode = "pan" | "split" | "center";

/** A face rectangle on a sample frame — the basis for motion-based speaker detection. */
export interface Roi {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface SpeakerSegment {
  start: number;
  end: number;
  speaker: "left" | "right";
}

/** The fused diar⊕ROI timeline that drives the pan (spec §1.3). */
export interface SpeakerTrack {
  clipId: string;
  segments: SpeakerSegment[];
  roiL?: Roi;
  roiR?: Roi;
  source: "diar" | "roi" | "fused";
}

export type CaptionStyle = "opus" | "karaoke" | "minimal";

export interface Caption {
  id: string;
  clipId: string;
  style: CaptionStyle;
  assPath?: string;
  overrides?: Record<string, unknown>;
}

export interface Render {
  id: string;
  clipId: string;
  aspect: Aspect;
  mode: ReframeMode;
  captionId?: string;
  brandKitId?: string;
  preset: string;
  outputPath?: string;
  version: number;
  status: JobStatus;
}

// ───────────────────────── Brand · recipe · publish ─────────────────────────

export interface BrandKit {
  id: string;
  name: string;
  fonts?: string[];
  colors?: string[];
  captionStyle?: CaptionStyle;
  logoPath?: string;
}

/** A saved end-to-end pipeline (aspect + mode + caption style + export preset). */
export interface Recipe {
  id: string;
  name: string;
  aspect: Aspect;
  mode: ReframeMode;
  captionStyle: CaptionStyle;
  preset: string;
}

export type Platform = "tiktok" | "reels" | "shorts" | "linkedin" | "x" | "youtube";

export interface PublishPost {
  id: string;
  renderId: string;
  platform: Platform;
  scheduledAt?: string;
  status: "draft" | "scheduled" | "posted" | "failed";
}

// ───────────────────────── Server ─────────────────────────

export interface ServerCapabilities {
  version: string;
  encoders: string[];
  diarizationAvailable: boolean;
  offlineMode: boolean;
}

export interface StorageInfo {
  totalBytes: number;
  usedBytes: number;
  downloadDir: string;
}

// ───────────────────────── Progress stream (SSE/WS) ─────────────────────────

/** One frame on the progress stream — every loading/progress UI reads these, never fakes. */
export interface ProgressEvent {
  jobId: string;
  kind: "download" | "transcribe" | "render";
  status: JobStatus | TranscribeStatus;
  percent: number;
  /** Pre-formatted, human-readable line (e.g. "Downloading… 42% · 3.1 MB/s · ETA 0:18"). */
  humanSummary?: string;
}

// ═════════════════════════ Wire types — the api_v1 contract ═════════════════════════
//
// These mirror routes/api_v1.py's JSON **exactly** (snake_case), so the typed client and
// the screens speak the same shape the engine emits — no mapping layer to drift. The
// camelCase interfaces above are the aspirational domain model; the `*View` types below
// are what actually crosses the wire today (PROGRESS: "Phase-1 wiring reconciles the TS
// types with the real api_v1 shape"). When they diverge, the wire types win.

/** The pre-formatted, agent/CLI-friendly strings every job view carries (`_job_view.human`). */
export interface HumanSummary {
  progress?: string;
  downloaded?: string;
  size?: string;
  speed?: string;
  eta?: string;
  elapsed?: string;
  audio_duration?: string;
  summary: string;
}

export interface Health {
  ok: boolean;
  version: string;
}

/** A download job (`_job_view`). */
export interface JobView {
  id: string;
  url: string;
  title: string;
  status: JobStatus | "downloading";
  filename: string | null;
  thumbnail: string | null;
  format_choice: string | null;
  downloaded_bytes: number;
  total_bytes: number;
  speed_bps: number | null;
  eta_seconds: number | null;
  fragment_index: number | null;
  fragment_count: number | null;
  progress_pct: number;
  elapsed_seconds: number;
  auto_transcribe: boolean;
  error_category: string | null;
  error_message: string | null;
  human: HumanSummary;
}

/** A transcribe job (`_tj_view`). */
export interface TranscribeJobView {
  id: string;
  parent_job_id: string;
  status: TranscribeStatus;
  model_used: string;
  progress_pct: number;
  duration_seconds: number;
  language_detected: string;
  elapsed_seconds: number;
  error_category: string | null;
  error_message: string | null;
  diarization_status: string | null;
  diarization_error: string | null;
  speaker_count: number | null;
  human: HumanSummary;
}

export type ClipKind = "moments" | "cut" | "reframe" | "caption" | "export" | "pipeline";

/** One moment from a `find_moments` job's `result.candidates`. */
export interface MomentCandidate {
  start: number;
  end: number;
  title: string;
  rationale: string;
  mode: string;
  signals: string[];
  source_id?: string;
}

/** A clip/render job's `result` — fields present depend on `kind`. */
export interface ClipJobResult {
  candidates?: MomentCandidate[];
  count?: number;
  mode?: string;
  clip_id?: string;
  clip_path?: string;
  reframed_path?: string;
  captioned_path?: string;
  ass_path?: string;
  render_id?: string;
  output_path?: string;
  preset?: string;
  style?: string;
  aspect?: string;
  reframe_mode?: string;
  source?: string;
  segments?: SpeakerSegment[];
  start?: number;
  end?: number;
}

/** A clip/render job (`_clip_job_view`) — the render-queue entry. */
export interface ClipJobView {
  id: string;
  kind: ClipKind;
  source_id: string | null;
  clip_id: string | null;
  status: TranscribeStatus; // queued|running|done|error|cancelled
  progress_pct: number;
  stage: string | null;
  elapsed_seconds: number;
  params: Record<string, unknown>;
  result: ClipJobResult;
  error_category: string | null;
  error_message: string | null;
  human: HumanSummary;
}

/** Paginated list envelope (`{<key>, total, returned, limit, offset}`). */
export interface Paginated {
  total: number;
  returned: number;
  limit: number;
  offset: number;
}
export interface JobList extends Paginated {
  jobs: JobView[];
}
export interface TranscriptList extends Paginated {
  transcripts: TranscribeJobView[];
}
export interface ClipJobList extends Paginated {
  clip_jobs: ClipJobView[];
}

/** The feature / limit / format registry (`/capabilities`). */
export interface Capabilities {
  api_version: string;
  schema_version: number;
  auth_required: boolean;
  features: Record<string, boolean>;
  formats: {
    transcript_export: string[];
    clip_aspects?: string[];
    reframe_modes?: string[];
    caption_styles?: string[];
    render_presets?: string[];
  };
  scopes: Record<string, string>;
  limits: Record<string, unknown>;
  openapi_url: string;
}

/** Dependency-doctor report (`/doctor`) — drives S0 Onboarding. */
export interface DoctorTool {
  present: boolean;
  version: string | null;
}
export interface DoctorReport {
  machine: Record<string, unknown>;
  tools: Record<string, DoctorTool>;
  encoders: string[];
  ok: boolean;
}

/** One SSE frame from `/events` — the whole live state (jobs + transcripts + clips). */
export interface EventsSnapshot {
  ts: number;
  jobs: JobView[];
  transcripts: TranscribeJobView[];
  clips: ClipJobView[];
}

/** A transcript's v2 `words.json` as returned by `/transcripts/{tid}/export.json`. */
export interface TranscriptWord {
  idx: number;
  w: string;
  start: number | null;
  end: number | null;
  speaker?: string | null;
  deleted?: boolean;
}
export interface TranscriptDocSegment {
  start: number;
  end: number;
  text: string;
  word_idxs: number[];
  speaker: string | null;
}
export interface TranscriptDoc {
  schema_version: number;
  language?: string;
  duration?: number;
  words: TranscriptWord[];
  segments: TranscriptDocSegment[];
}

/** One turn of the NL agent (`POST /agent`). `clarify` is the spec's elicitation —
 *  the studio renders `question` + `options` as an inline card. */
export interface AgentResponse {
  reply: string;
  action: "find_moments" | "make_clip" | "clarify" | "reply";
  jobs: ClipJobView[];
  question?: string;
  options?: string[];
}
