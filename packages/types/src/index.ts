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
