# Spool Private Clip Foundry: recovery and differentiation roadmap

**Date:** 2026-07-13

**Status:** Phase 0 complete and verified on 2026-07-22; Phases 1-5 have not started

**Owner:** Spool maintainers

**Product direction:** Private Clip Foundry

## 1. Decision and document authority

Spool will stop expanding sideways into publishing, decorative analytics, and broad
automation until its core is safe, durable, and media-correct. The product will become a
**Private Clip Foundry**: local-first video automation with editor-grade inspection,
reproducible decisions, open deliverables, and an agent that cannot silently mutate or
export private work.

This document is the authoritative build order for the recovery program. It supersedes:

- the roadmap and state-model sequencing in `docs/Spool_00_Product-Overview.md`;
- the data-model decisions and Phase 3-4 sequencing in
  `docs/Spool_Engineering-Spec.md`;
- sequencing in `docs/IMPROVEMENTS-PLAN.md` that defers durability and error handling;
- the "proceed to Phase 3" direction in `docs/PROGRESS.md`.

It does not supersede:

- the one-API rule in `README.md`;
- the current visual language unless a phase below explicitly changes a component;
- licensing, attribution, supported media tooling, or completed behavior that passes the
  new gates;
- historical documents as records of why the current implementation exists.

Amendments require a committed edit to this document and explicit user approval. Phase
implementation plans may add file-level detail, but may not weaken the invariants or gates
defined here.

## 2. Why this program exists

Spool has a strong premise and a capable local media engine, but the current alpha is not
safe for irreplaceable media. The primary failure is architectural: disposable queue
records also act as the application's source of product identity and ownership. That
coupling turns queue cleanup, cancellation races, and stale persistence into data-loss and
phantom-state bugs.

The second failure is media semantics. Source time, clip time, and output time are passed as
anonymous floating-point seconds. Ripple cuts, captions, diarization, and pan expressions
therefore disagree after edits.

The third failure is product truth. Several visible controls either do nothing, show
fabricated data, swallow errors, or promise privacy that depends on an undisclosed remote
reasoning path.

Auto-clipping, captions, speaker tracking, virality scores, and agent buttons are now table
stakes. Spool should not win by cloning a cloud clipping suite feature for feature. It
should win by making private footage programmable without giving up editorial control or
open files.

### North-star promise

> Turn private footage into reproducible, editor-grade clips without surrendering the
> footage or editorial control.

### Primary users

1. Solo creators and founders turning long recordings into short clips.
2. Podcasters and interviewers working with two-person talking-head footage.
3. Agencies and ghostwriters handling client footage that should not be uploaded to a
   third-party clipping service.
4. Developers automating the same workflow through the versioned API, CLI, or MCP.

### Success at the end of Phase 4

A new user can import a local file, discover and approve moments, edit a clip, render it,
and deliver an open export bundle without opening a terminal. They can restart the app at
any point without losing state. With a local reasoning provider configured, the workflow
works while outbound networking is blocked. Every agent mutation is previewed, approved,
auditable, and reversible through versioned state.

## 3. Verified current state

The repository audit on 2026-07-13 produced a clean functional-test baseline but exposed
coverage gaps:

- Engine: 860 tests passed.
- Studio: 48 Vitest tests passed across 7 files.
- Five TypeScript package typechecks passed.
- The passing tests do not cover the destructive, concurrent, media-time, mobile, or
  contract failures below.

### Highest-impact failures

| Priority | Current behavior                                                                                                           | Evidence                                                                                                                                | User impact                                                                                |
| -------- | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| P0       | Queue "Clear finished" dismisses terminal downloads, and dismissal unlinks source media.                                   | `apps/studio/src/app/queue/page.tsx:20-24`, `engine/jobs.py:220-240`                                                                    | A cleanup action can destroy the source and break dependent transcripts and clips.         |
| P1       | Cancelled queued work can start later and overwrite `cancelled` with `running` or `done`.                                  | `engine/jobs.py:293`, `engine/transcribe_jobs.py:154`, `engine/clip_jobs.py:159`                                                        | Cancelled downloads and renders resurrect and create orphaned outputs.                     |
| P1       | Whole-file JSON snapshots can replace newer state with an older concurrent snapshot.                                       | `engine/jobs.py:103-115`, `engine/transcribe_jobs.py:111-141`, `engine/clip_jobs.py:113-143`                                            | Restart can downgrade terminal work or lose current progress.                              |
| P1       | Studio invents Sources and Clips from job history.                                                                         | `apps/studio/src/components/spool/context.tsx:115-182`                                                                                  | Failed jobs create phantom clips; dismissing history removes product records.              |
| P1       | Ripple deletion, captions, and fallback pan use incompatible time spaces.                                                  | `engine/clip_runner.py:65-94`, `engine/clip/captioner.py:125-148`, `engine/clip/backhalf/pan_expr.py:13-21`                             | Captions and active-speaker framing drift after edits.                                     |
| P1       | FFmpeg cancellation can leave partial files that later stages accept.                                                      | `engine/clip/_ffmpeg.py:18-94`, `engine/clip_runner.py:195-221`                                                                         | Failed or cancelled renders can appear as valid artifacts.                                 |
| P1       | Failed prerequisites are treated as successful by the Studio pipeline.                                                     | `apps/studio/src/components/spool/context.tsx:412-475`                                                                                  | Captioning or export proceeds against stale and incomplete media.                          |
| P1       | Local file import, Retry, Undo, Publish, and Analytics contain fake, dead, or fabricated behavior.                         | `apps/studio/src/app/import/page.tsx:21-72`, `apps/studio/src/app/analytics/page.tsx:6-11`, `apps/studio/src/app/publish/page.tsx:7-14` | The interface reports capabilities and outcomes it does not provide.                       |
| P1       | Privacy copy says work stays local while the default discovery path can send transcript data to Codex.                     | `apps/studio/src/app/onboarding/page.tsx:56-67`, `engine/clip/llm.py:160-189`, `engine/settings.py:29-36`                               | Users cannot give informed consent for transcript egress.                                  |
| P1       | Mapped IPv6 private addresses bypass SSRF checks, tokenless cross-origin mutations are accepted, and queues are unbounded. | `engine/safety.py:20-41`, `engine/safety.py:94-104`, `engine/app.py:257-276`                                                            | A hostile page or caller can mutate state, reach private services, or exhaust the process. |
| P1       | Token-protected Studio deployments and several API/CLI/MCP contracts are broken.                                           | `apps/studio/src/lib/engine.ts:8-10`, `packages/api-client/src/index.ts:325-328`, `engine/trove_client.py:289-295`                      | Supported clients disagree about authentication and payloads.                              |
| P2       | Fixed-width layout and mouse-only controls fail on narrow screens and keyboards.                                           | `apps/studio/src/app/spool.css:364-397`, `packages/ui/src/ui.tsx:133-135`                                                               | Important actions are clipped or inaccessible.                                             |

### Root causes

1. **Ownership and execution are conflated.** Queue entries own paths and IDs that the
   product treats as Sources, Transcripts, Clips, and Renders.
2. **Time has no type.** Anonymous seconds cross source, clip, and output boundaries.
3. **Artifacts have no commit protocol.** A filename can look complete before media and
   metadata are validated and committed.
4. **The API contract is handwritten in several places.** Flask responses, TypeScript
   types, the TypeScript client, Python client, CLI, and MCP drift independently.
5. **The UI was broadened before the core loop became trustworthy.** Placeholder surfaces
   and swallowed failures disguise missing behavior.

## 4. What stays and must not regress

- The UI, CLI, and MCP remain clients of one versioned JSON API. There is no second agent
  engine or UI-only implementation path.
- FFmpeg, yt-dlp, local transcription, diarization, and the existing audio-plus-ROI reframe
  approach remain the media foundation.
- Media and deliverables remain ordinary user-visible files on disk. SQLite owns metadata,
  not opaque media blobs.
- The Python engine and Next.js monorepo stay in place. This program is not a platform or
  language rewrite.
- Existing successful engine, Studio, CLI, and MCP behaviors remain regression coverage.
- Recipes, Watches, and existing user data are preserved even while unfinished surfaces are
  hidden.

## 5. Product and engineering invariants

These are target invariants. Existing violations may remain only until the owning phase gate
below, must not worsen, and may not be copied into new code. High-risk violating behavior is
disabled until compliant.

1. Queue cleanup never deletes a Source, Transcript, Clip, Render, Delivery, or published
   artifact.
2. Jobs describe attempts. They never define product identity or ownership.
3. A domain entity exists independently of whether its latest job succeeded, failed, was
   cancelled, or was dismissed.
4. Source time, clip time, and output time are named at every persisted or public boundary.
5. A final artifact becomes visible only after successful validation and atomic promotion.
6. A failed or cancelled attempt cannot become current after a newer generation exists.
7. No visible control may fabricate data, perform a no-op while reporting success, or hide a
   failure.
8. Remote transcript egress is explicit, attributable to a provider, and consented to before
   the request.
9. Anything the agent can mutate can also be inspected and performed manually.
10. Agent mutations require a frozen plan, visible diff, approval, immutable execution ID,
    and revision-based undo.
11. API, CLI, Studio, and MCP semantics are contract-tested against the same versioned
    schema.
12. No new feature work may bypass an unmet phase gate.

| Invariants | Enforcement schedule                                                                                                        |
| ---------- | --------------------------------------------------------------------------------------------------------------------------- |
| 1 and 7    | Phase 0 removes destructive cleanup and false controls.                                                                     |
| 2 and 3    | Phase 1 establishes domain ownership in the engine; Phase 3 removes the remaining job-derived Studio adapter.               |
| 4          | Phase 2 owns the typed timeline boundary. Anonymous durable time fields are not added before then.                          |
| 5          | Phase 1 establishes Artifact publication; Phase 2 applies it to every media stage.                                          |
| 6          | Phase 0 adds immediate state guards; Phase 1 makes generation checks durable.                                               |
| 8          | Phase 0 disables remote reasoning; Phase 4 adds explicit consent and full EgressReceipt behavior before re-enablement.      |
| 9 and 10   | Phase 0 disables mutating agent actions. Phase 4 re-enables them only after plan, diff, approval, execution, and undo pass. |
| 11         | Phase 0 repairs known drift; Phase 1 makes OpenAPI 3.1 authoritative.                                                       |
| 12         | Effective immediately for all work in this program.                                                                         |

## 6. Target architecture

```text
Studio / CLI / MCP
        |
        v
Versioned JSON API and event contract
        |
        v
Domain operations
(Source, Transcript, Candidate, Clip Revision, Render, Delivery)
        |
        +------> SQLite metadata and relationships
        +------> open media and artifact files
        |
        v
Persisted job-attempt state machine
        |
        +------> download worker pool
        +------> transcription worker pool
        +------> media worker pool
```

The three execution pools remain separate during migration. They share one persisted job
schema and transition service, but Phase 1 does not build a giant generic executor.

### 6.1 Durable domain model

| Entity               | Ownership and lifecycle                                                                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Source`             | Owns origin, provenance, one Spool-managed source artifact, duration, availability, and current transcript pointer. Existing source IDs remain stable during migration.   |
| `Artifact`           | Records a relative path, kind, bytes, checksum, validation metadata, and `staging`, `published`, `missing`, `quarantined`, `trashed`, `purge_pending`, or `purged` state. |
| `Transcript`         | Stable identity for a Source's transcript history and current revision pointer.                                                                                           |
| `TranscriptRevision` | Immutable words, segments, speakers, model, language, and edit provenance. Transcript edits create revisions.                                                             |
| `DiscoveryRun`       | Records the transcript revision, mode, provider, settings, and reasoning receipt used to produce Candidates.                                                              |
| `Candidate`          | Persists source bounds, title, rationale, named score factors, and `proposed`, `approved`, or `rejected` state.                                                           |
| `Clip`               | Stable editorial identity with a current revision pointer.                                                                                                                |
| `ClipRevision`       | Immutable source window, timeline map, caption/reframe settings, and parent revision. Manual edits and undo create or select revisions.                                   |
| `Render`             | Immutable desired output for one exact ClipRevision. It receives published artifact pointers only after validation; execution status belongs only to JobAttempt.          |
| `Delivery`           | One validated Render plus relationally owned export-bundle Artifacts and its reproducible root manifest.                                                                  |
| `JobAttempt`         | One immutable execution attempt within a logical job: subject, generation, params, result, error, cancellation, progress, and timestamps. Retry creates a new row.        |
| `LegacyAlias`        | Resolves existing download-job-derived IDs and compatibility routes during migration.                                                                                     |
| `EgressReceipt`      | Provider, purpose, approved data categories, payload digest, timestamp, and result for remote reasoning.                                                                  |

SQLite is opened with `journal_mode=WAL`, `foreign_keys=ON`,
`busy_timeout=5000`, and `synchronous=FULL`. All timestamps use RFC 3339 UTC with fixed
millisecond precision (`YYYY-MM-DDTHH:MM:SS.sssZ`). Durable timeline values are integer
milliseconds with the coordinate space in the field name, such as `source_start_ms`,
`clip_start_ms`, and `output_start_ms`. Anonymous durable `start` and `end` floats are
forbidden.

The first migration must implement the following semantic schema. File-level plans may add
indexes and audit columns, but may not remove these relationships:

```sql
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN
    ('staging', 'published', 'missing', 'quarantined', 'trashed', 'purge_pending', 'purged')),
  byte_size INTEGER,
  sha256 TEXT,
  validation_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (byte_size IS NULL OR byte_size >= 0),
  CHECK (state != 'published' OR
    (byte_size IS NOT NULL AND sha256 IS NOT NULL AND validation_json IS NOT NULL))
);

CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  origin_kind TEXT NOT NULL CHECK (origin_kind IN
    ('url', 'local_upload')),
  original_uri TEXT,
  title TEXT NOT NULL,
  media_artifact_id TEXT REFERENCES artifacts(id),
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  state TEXT NOT NULL CHECK (state IN
    ('importing', 'ready', 'failed', 'missing', 'trashed')),
  current_transcript_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  trashed_at TEXT,
  FOREIGN KEY (current_transcript_id, id)
    REFERENCES transcripts(id, source_id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE transcripts (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  current_revision_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (id, source_id),
  FOREIGN KEY (current_revision_id, id)
    REFERENCES transcript_revisions(id, transcript_id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE transcript_revisions (
  id TEXT PRIMARY KEY,
  transcript_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(id),
  revision INTEGER NOT NULL,
  words_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  model TEXT NOT NULL,
  language TEXT,
  parent_revision_id TEXT,
  created_by TEXT NOT NULL CHECK (created_by IN
    ('engine', 'manual', 'agent', 'migration')),
  created_at TEXT NOT NULL,
  UNIQUE (transcript_id, revision),
  UNIQUE (id, transcript_id),
  UNIQUE (id, source_id),
  FOREIGN KEY (transcript_id, source_id)
    REFERENCES transcripts(id, source_id),
  FOREIGN KEY (parent_revision_id, transcript_id)
    REFERENCES transcript_revisions(id, transcript_id)
);

CREATE TABLE egress_receipts (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  data_categories_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  request_artifact_id TEXT REFERENCES artifacts(id),
  approved_at TEXT NOT NULL,
  response_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  redacted_at TEXT
);

CREATE TABLE egress_receipt_links (
  egress_receipt_id TEXT NOT NULL REFERENCES egress_receipts(id),
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  PRIMARY KEY (egress_receipt_id, entity_kind, entity_id)
);

CREATE TABLE discovery_runs (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  transcript_revision_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  provider TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  egress_receipt_id TEXT REFERENCES egress_receipts(id),
  created_at TEXT NOT NULL,
  UNIQUE (id, source_id),
  FOREIGN KEY (transcript_revision_id, source_id)
    REFERENCES transcript_revisions(id, source_id)
);

CREATE TABLE candidates (
  id TEXT PRIMARY KEY,
  discovery_run_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_start_ms INTEGER NOT NULL,
  source_end_ms INTEGER NOT NULL,
  title TEXT NOT NULL,
  rationale TEXT NOT NULL,
  score_factors_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('proposed', 'approved', 'rejected')),
  created_at TEXT NOT NULL,
  CHECK (source_start_ms >= 0),
  CHECK (source_end_ms > source_start_ms),
  UNIQUE (id, source_id),
  FOREIGN KEY (discovery_run_id, source_id)
    REFERENCES discovery_runs(id, source_id)
);

CREATE TABLE clips (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  candidate_id TEXT,
  current_revision_id TEXT,
  current_render_id TEXT,
  state TEXT NOT NULL CHECK (state IN ('draft', 'ready', 'trashed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (id, source_id),
  FOREIGN KEY (candidate_id, source_id)
    REFERENCES candidates(id, source_id),
  FOREIGN KEY (current_revision_id, id)
    REFERENCES clip_revisions(id, clip_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (current_render_id, id)
    REFERENCES renders(id, clip_id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE clip_revisions (
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id),
  revision INTEGER NOT NULL,
  parent_revision_id TEXT,
  source_window_json TEXT NOT NULL,
  timeline_map_json TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by IN
    ('engine', 'manual', 'agent', 'migration')),
  created_at TEXT NOT NULL,
  UNIQUE (clip_id, revision),
  UNIQUE (id, clip_id),
  FOREIGN KEY (parent_revision_id, clip_id)
    REFERENCES clip_revisions(id, clip_id)
);

CREATE TABLE renders (
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id),
  clip_revision_id TEXT NOT NULL,
  video_artifact_id TEXT REFERENCES artifacts(id),
  manifest_artifact_id TEXT REFERENCES artifacts(id),
  preset TEXT NOT NULL,
  created_at TEXT NOT NULL,
  published_at TEXT,
  UNIQUE (id, clip_id),
  FOREIGN KEY (clip_revision_id, clip_id)
    REFERENCES clip_revisions(id, clip_id),
  CHECK (published_at IS NULL OR
    (video_artifact_id IS NOT NULL AND manifest_artifact_id IS NOT NULL))
);

CREATE TABLE deliveries (
  id TEXT PRIMARY KEY,
  render_id TEXT NOT NULL REFERENCES renders(id),
  manifest_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (render_id)
);

CREATE TABLE delivery_artifacts (
  delivery_id TEXT NOT NULL REFERENCES deliveries(id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  role TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
  PRIMARY KEY (delivery_id, role, ordinal),
  UNIQUE (delivery_id, artifact_id)
);

CREATE TABLE job_attempts (
  id TEXT PRIMARY KEY,
  logical_job_id TEXT NOT NULL,
  retry_of_id TEXT REFERENCES job_attempts(id),
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled', 'interrupted')),
  pause_requested_at TEXT,
  cancel_requested_at TEXT,
  progress_json TEXT NOT NULL,
  params_json TEXT NOT NULL,
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  dismissed_at TEXT,
  CHECK (generation >= 1),
  UNIQUE (logical_job_id, generation)
);

CREATE TABLE legacy_aliases (
  legacy_kind TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  PRIMARY KEY (legacy_kind, legacy_id)
);

CREATE INDEX idx_sources_state_created
  ON sources(state, created_at, id);
CREATE INDEX idx_transcripts_source
  ON transcripts(source_id, created_at, id);
CREATE INDEX idx_candidates_source_state
  ON candidates(source_id, state, created_at, id);
CREATE INDEX idx_clips_source_state
  ON clips(source_id, state, created_at, id);
CREATE INDEX idx_renders_clip_created
  ON renders(clip_id, created_at, id);
CREATE INDEX idx_jobs_state_created
  ON job_attempts(state, created_at, id);
CREATE INDEX idx_jobs_subject
  ON job_attempts(subject_type, subject_id, created_at, id);
CREATE UNIQUE INDEX idx_jobs_one_active_generation
  ON job_attempts(logical_job_id)
  WHERE state IN ('queued', 'running', 'paused');
CREATE INDEX idx_artifacts_state_created
  ON artifacts(state, created_at, id);
CREATE INDEX idx_egress_links_entity
  ON egress_receipt_links(entity_kind, entity_id);
CREATE INDEX idx_delivery_artifacts_artifact
  ON delivery_artifacts(artifact_id);
```

Current-pointer foreign keys are deferred so a transaction can create a new immutable
revision or Render and update its owner atomically. The repository layer also verifies that
each current Transcript, ClipRevision, and Render belongs to the same owning Source or Clip.
Composite foreign keys prevent TranscriptRevision, DiscoveryRun, Candidate, Clip,
ClipRevision, and Render rows from naming inconsistent parents or crossing Source boundaries.

JobAttempt is the only owner of execution status. A Render is created with immutable desired
settings for one ClipRevision; its publication pointers are write-once. Failed, cancelled,
and interrupted attempts leave those pointers empty. After validation, one publication
transaction sets both published Artifact pointers and `published_at`; only then may
`clips.current_render_id` select it. Delivery creation verifies that the Render is published
and that every row in `delivery_artifacts` is `published`. Required roles are `video`,
`caption_srt`, `caption_vtt`, `caption_ass`, `transcript_json`, `thumbnail`, `copy`, and
`project_manifest`; `ordinal` supports future multi-language or multi-file roles. The
garbage collector treats every `delivery_artifacts` row as a reachability root.

The existing FTS5 transcript index remains a rebuildable search index. It is not a second
source of product identity or lifecycle state.

### 6.2 Job transition contract

Legal transitions are:

```text
queued -------> running -------> succeeded
  |  \             |-----------> failed
  |   \            |-----------> cancelled
  |    v           |-----------> interrupted
  |  paused <------+
  |    |
  |    +----------> queued (resume, same generation)
  +---------------> cancelled

failed/cancelled/interrupted -- retry --> new queued attempt with generation + 1
```

- Every transition is a compare-and-set transaction over `(id, logical_job_id, generation,
state)`.
- Only the active generation may start, report progress, publish output, or finish.
- Retry inserts a new JobAttempt with the same `logical_job_id`, `retry_of_id` pointing to the
  terminal attempt, and `generation + 1`. Terminal attempts are never reset to queued.
- The repository verifies that `retry_of_id` belongs to the same logical job, subject, and
  kind. Terminal attempt fields are immutable except `dismissed_at`.
- The partial unique index permits at most one queued, running, or paused attempt per logical
  job.
- Cancellation is committed before terminating the worker process.
- A pause request is committed before terminating a resumable worker. Resume keeps the same
  generation and requeues that attempt against its verified partial input.
- Dismissal sets `dismissed_at`; it never deletes domain rows or artifacts.
- On restart, resumable downloads become queued or paused according to their persisted
  capability. Non-resumable running work becomes interrupted and requires an explicit retry.
- History retention is bounded and configurable, but domain history and referenced artifacts
  are not garbage-collected through job retention.

### 6.3 Artifact commit and deletion contract

1. A short transaction reserves an Artifact in `staging` state and commits its immutable
   final relative path.
2. The stage writes to an attempt-scoped same-directory temporary path that preserves the
   media suffix.
3. The stage validates the output with `ffprobe`, stream checks, expected dimensions,
   non-zero duration, and a decode probe.
4. `os.replace()` promotes the temporary path to its immutable final path.
5. A second short transaction sets bytes, checksum, validation metadata, and `published`
   state and promotes the owning current pointer. Media validation includes `ffprobe`; text,
   JSON, and manifest Artifacts use their schema or parser validator.
6. A startup reconciler repairs the crash window after step 1 or step 4, or quarantines the
   unreferenced file. It never deletes unknown user media.

An existing published artifact remains current until its replacement is fully published.
Downstream stages accept only published artifact IDs, never "the latest filename that
exists."

Queue cleanup is non-destructive. Source deletion is a separate Library action:

- preview the dependent Transcript, Candidate, Clip, Render, and Delivery counts;
- refuse deletion while related jobs are active;
- mark the Source and exclusively owned Artifacts as logically trashed without changing
  immutable relative paths;
- allow restore for seven days;
- require a second explicit action to purge Trash;
- garbage-collect only artifacts unreachable from non-trashed domain records and older than
  the grace period.

All new local imports are copied into the Spool-managed library. Referencing arbitrary
external paths is outside this program, so Source deletion never targets a file outside that
library. A failed URL or local import retains a retryable `failed` Source record and no
published media Artifact.

EgressReceipt links participate in Source and Clip deletion preview. Purge first marks the
request Artifact `purge_pending` in a transaction while retaining the receipt link; normal
reads hide that state. It then unlinks the managed request bytes. A final transaction sets
`request_artifact_id` to `NULL`, sets `redacted_at`, and marks the Artifact `purged`. Startup
reconciliation completes any interrupted `purge_pending` operation. The receipt retains only
provider, purpose, data categories, payload digest, approval time, and response status as the
minimal audit record.

### 6.4 Canonical media timeline

`engine/clip/timeline.py` becomes the pure timeline boundary. It defines named
`SourceSpan`, `ClipSpan`, `OutputSpan`, and an immutable `TimelineMap` made of ordered
source slices mapped to contiguous clip slices.

Rules:

- The persisted map uses integer milliseconds and a schema version.
- Deletions are normalized, clamped to the source window, merged, and subtracted once.
- An all-deleted edit produces an explicit `empty_after_edits` error. It never falls back to
  the original range.
- Caption words and diarization turns are intersected in SourceTime, then mapped once into
  ClipTime.
- OutputTime remains a distinct type even when initially identical to ClipTime.
- Inserted transcript text is anchored caption metadata. It does not create zero-duration
  spoken media or affect cut duration.
- Clip revisions persist the exact timeline map and stage settings used for rendering.

### 6.5 API and event contract

`/api/v1` remains supported through the recovery program. Existing job, transcript, clip-job,
and event endpoints remain compatibility surfaces until Studio, CLI, and MCP have migrated to
domain endpoints.

Required domain resources are:

| Resource        | Required operations                                                     |
| --------------- | ----------------------------------------------------------------------- |
| Sources         | list/get, URL import, local-file import, delete preview, trash, restore |
| Transcripts     | list/get, create revision, set current revision, export                 |
| Discovery       | start run, list runs, list/approve/reject Candidates                    |
| Clips           | list/get, create revision, set current revision, trash/restore          |
| Renders         | submit/get/cancel/retry, list validated artifacts                       |
| Deliveries      | create/get/list, verify bundle manifest                                 |
| Jobs            | paginated attempt history, cancel/retry/dismiss history only            |
| Agent changes   | propose/get diff/approve/execute/revert                                 |
| Egress receipts | list/get for the local user                                             |

Compatibility policy:

- Existing response fields and `/api/v1/events` full snapshots remain valid through the first
  tagged Phase 3 release and for at least 90 days afterward. Removal also requires a
  separately approved cleanup spec.
- A tagged release is a repository tag matching `vMAJOR.MINOR.PATCH`; engine diagnostics and
  Studio build metadata report the same version.
- Additive fields are allowed; removals follow that compatibility window.
- Deprecated fields return warning headers and are covered by adapter tests.
- Existing IDs resolve through `LegacyAlias`.
- `/jobs` becomes job history only. Studio stops constructing product entities from it.
- The contract exposes `api_version`, `contract_version`, `storage_schema_version`, and
  `migration_status` separately.
- A checked-in OpenAPI 3.1 document at `docs/api/openapi.yaml` is authoritative. Flask
  responses, generated or validated TypeScript types, the TypeScript client, Python client,
  CLI, and MCP are contract-tested against it.
- A separate versioned delta or invalidation event endpoint replaces full snapshots for the
  new Studio. The compatibility `/api/v1/events` endpoint remains unchanged during the
  window above. Delta heartbeats do not serialize the entire library.
- Every collection is cursor-paginated and indexed. List queries do not probe the filesystem.

### 6.6 Security and privacy contract

- The engine binds to loopback by default.
- A non-loopback bind refuses startup without authentication.
- Browser mutations require an allowed local Origin and configured authentication where
  applicable. Originless CLI and MCP calls remain supported.
- `X-Forwarded-For` is ignored unless a trusted-proxy mode is explicitly configured.
- IPv4-mapped IPv6 is normalized before private, loopback, link-local, and metadata-range
  checks.
- Download, transcription, and media pending capacity defaults to four times the configured
  worker count, with a minimum of 4 and maximum of 32. Capacity overflow returns a structured
  `429 queue_full` response with `Retry-After` and creates no executor work.
- DOM media URLs work in authenticated mode through short-lived signed URLs or an equivalent
  same-origin authenticated proxy.
- Phase 0 exposes no remote reasoning provider. Persisted and environment-supplied legacy
  provider/consent values are canonicalized to `none`/`false`, and every reasoning-dependent
  route fails before provider, lease, job, or subprocess work. A later phase may re-enable
  remote reasoning only through a supported zero-tool transport plus explicit consent.
- The Phase 0 UI reports `Fully local` or `Offline`. `Remote reasoning enabled` remains the
  required label for any future remote-provider implementation; it is unreachable while the
  provider capability is disabled.

## 7. Delivery roadmap

```text
Phase 0: Safety and product-truth fuse
        |
        v
Phase 1: Durable domain core and recovery
        |\
        | +----> Phase 2A pure timeline work may begin in parallel
        v
Phase 2: Media correctness and reproducibility
        |
        v
Phase 3: One golden workflow
        |
        v
Phase 4: Private intelligence and safe agent
        |
        v
Phase 5: Automation, publishing connectors, and real analytics
```

No Phase 5 feature work starts before the Phase 3 gate. Phase 4 agent mutation work also
depends on durable revisions from Phases 1 and 2.

### Phase 0: safety and product-truth fuse

**Goal:** remove data-loss paths, stop lying in the UI, repair exposed contracts, and put a
minimum security boundary around the current alpha.

**Scope:**

1. Make download, transcription, and clip-job dismissal history-only. Terminal cancel,
   queue clearing, and TTL sweep must not unlink published media.
2. Guard queued workers and completion callbacks with current-state and attempt-identity
   checks so cancelled work cannot start or publish. Phase 1 persists the generation rule.
3. Hide Publish, Analytics, Recipes, and Watches from primary navigation until their release
   gates pass. Preserve their data and backend code.
4. Remove fake local-file drop, fake Undo, dead Retry, fake quality choices, and fabricated
   numbers. A control is either wired to a real operation with errors or absent.
5. Reject invalid URL imports before submission and display structured errors.
6. Correct onboarding and settings copy. Keep remote reasoning unavailable and canonicalize
   provider state to `none`/`false` until a supported zero-tool transport can enforce the
   promised transcript-only boundary; any future enablement still requires explicit consent.
7. Fix mapped-IPv6 SSRF validation, hostile browser Origin rejection, trusted-proxy handling,
   bounded queues, and rate-limiter key retention.
8. Make token-protected Studio API, SSE, playback, and downloads work.
9. Repair known API/CLI/MCP contract drift, including transcript word-edit payloads and bulk
   response shapes. Replace or clearly remove the TypeScript MCP stub.
10. Upgrade the vulnerable PostCSS dependency and any high-severity advisory present at the
    final gate in isolated dependency changes.
11. Disable mutating agent tools and their UI actions. Read-only inspection may remain;
    mutation returns a structured `agent_mutation_disabled` response until Phase 4 passes.

**Acceptance gate:**

1. Dismissing, cancelling, clearing, and TTL-sweeping every terminal job type leaves every
   existing managed source, transcript, clip, caption, render, and export file byte-identical
   before and after restart.
2. A cancelled queued job never invokes its target. A cancelled running job remains
   cancelled after its worker unwinds.
3. `::ffff:127.0.0.1`, mapped private ranges, and mapped metadata addresses fail URL
   validation.
4. A hostile or `null` browser Origin cannot perform a state-changing request. A valid local
   Studio Origin and originless authenticated CLI/MCP calls still work.
5. Submitting pending capacity plus one returns `429 queue_full`, includes `Retry-After`, and
   creates no hidden executor work.
6. Studio completes its existing URL-to-clip E2E with token authentication enabled.
7. Every production navigation item and visible control performs a real operation or is
   absent. Error responses are visible and actionable.
8. Remote transcript reasoning cannot begin in Phase 0. Provider state remains `none`/`false`,
   hostile legacy state is repaired, and every route/direct boundary rejects before egress.
9. Contract fixtures pass against Flask, the TypeScript client, Python client, CLI, and MCP.
10. Mutating agent actions are absent or rejected with `agent_mutation_disabled`; no current
    mutation path bypasses the Phase 4 approval contract.

**Rollback:** each change is independently revertible. Destructive cleanup remains disabled
if any dependent behavior is uncertain. Security changes keep loopback CLI/MCP access as a
recovery path.

**Estimated effort:** 3-5 focused engineering days, split into destruction/product-truth,
security/resource, authentication, and contract PRs.

**Completion record:** Phase 0 passed its final gates at behavior checkpoint `c8af69c` on
2026-07-22. The committed evidence, review checkpoints, known frozen formatting baseline,
and later-phase deferrals are recorded in Section 16 of the
[Phase 0 implementation plan](../plans/2026-07-13-private-clip-foundry-phase-0-safety-fuse.md#16-phase-0-completion-record).

### Phase 1: durable domain core and recovery

**Goal:** make product identity, relationships, and current versions survive job cleanup,
concurrency, crashes, and restart.

**Scope:**

1. Add the versioned SQLite repository and schema from Section 6.1.
2. Add domain services for Sources, Transcript revisions, Discovery runs, Candidates, Clip
   revisions, Renders, Deliveries, Artifacts, and JobAttempts.
3. Replace whole-file snapshot persistence with row-level transactions and compare-and-set
   job transitions.
4. Implement the artifact commit protocol, startup reconciliation, Trash, restore, and purge.
5. Add cursor pagination and required indexes for status, creation time, foreign keys, and
   current-version queries.
6. Add idempotency keys for side-effecting imports and renders.
7. Preserve separate worker pools while routing all status changes through one transition
   service.
8. Add a strangler migration from:
   - `jobs.json`;
   - `transcribe_jobs.json`;
   - `clip_jobs.json`;
   - `clips/*/meta.json`;
   - transcript, caption, candidate, and render artifacts;

   The migration inventory also checksums Brand Kits, Recipes, Watches, and settings and
   preserves their existing stores until a child spec defines their durable schema. It does
   not silently drop or opportunistically reshape them during the core migration.

**Migration sequence:**

1. Install schema and repository adapters without changing runtime ownership.
2. Run an idempotent dry-run importer that emits counts, stable IDs, paths, missing files,
   dangling relationships, and collisions.
3. Enter maintenance mode under a process-wide migration lock. New submissions return a
   structured `503 migration_in_progress` response.
4. Drain safe work, mark remaining non-resumable work interrupted, and stop all worker pools.
5. Record checksums and backups of the final quiescent legacy metadata files.
6. Create `state.sqlite3.migrating` and import metadata in one transaction without moving
   media files.
7. Reconcile artifacts against the temporary database, run `PRAGMA integrity_check` and
   `PRAGMA foreign_key_check`, checkpoint and close it, and fsync it. Any failure deletes the
   temporary database and leaves the legacy backend active.
8. Atomically promote the validated temporary database to `state.sqlite3`, switch both reads
   and writes behind `SPOOL_STATE_BACKEND=sqlite`, then restart worker pools and leave
   maintenance mode.
9. On subsequent startups, an existing database at the target schema makes legacy import a
   verification-only no-op. The app may report checksum drift but must never rebuild or
   replace live SQLite state from stale legacy files.
10. Keep legacy metadata read-only and untouched through the Phase 3 golden-workflow release.
11. Remove fallback support only through a later explicit migration. Do not dual-write.

Stable existing Source, Transcript, and Clip IDs remain unchanged. New JobAttempts receive
independent IDs. Legacy routes resolve through `LegacyAlias`.

**Acceptance gate:**

1. Running the importer twice against the same legacy snapshot and isolated empty targets
   produces identical domain rows and no duplicate artifacts. This idempotency test occurs
   before either target accepts SQLite writes.
2. Migration failure rolls back the transaction, leaves legacy files unchanged, and prevents
   workers from starting while diagnostics remain available.
3. The dry-run parity report has zero unexplained missing records. Known missing files are
   represented as `missing`, not silently dropped.
4. Restart reconstructs the same Library without reading queue history.
5. Clearing all job history changes no domain-entity or published-artifact count.
6. One hundred barrier-driven concurrent submit, cancel, retry, and restart iterations
   preserve legal transitions and current generations.
7. Killing the process during queued, running, cancelling, and finalizing states produces no
   resurrected work or accepted partial artifact.
8. An interrupted artifact commit is repaired or quarantined on restart.
9. On a 10,000-Source fixture, a 100-item list page is at most 1 MiB, executes at most five
   SQL statements, and its filtered/sorted `EXPLAIN QUERY PLAN` uses a declared index rather
   than a full table scan.
10. Existing `/api/v1` clients continue through compatibility adapters.
11. Maintenance mode accepts no new side-effecting request, and no worker can write legacy
    metadata after the final snapshot begins.
12. Failed, cancelled, and interrupted Render attempts leave publication pointers empty and
    `clips.current_render_id` unchanged. Delivery creation rejects an unpublished Render or
    non-published Artifact.
13. Once the live database exists, restart never invokes legacy import or replaces its bytes;
    legacy checksum differences are diagnostic only.

**Rollback:** before cutover, restore the untouched legacy metadata backup. After SQLite has
accepted new writes, roll application code forward against SQLite; never silently fall back
to stale JSON. The SQLite database and media files are never deleted by rollback tooling.

**Estimated effort:** 7-12 focused engineering days across schema/repository, migration,
state-machine, artifact/reconciliation, and compatibility PRs.

### Phase 2: media correctness and reproducibility

**Goal:** make every editorial decision use one canonical mapping and publish only validated,
reproducible output.

**Required PR sequence:**

1. **Canonical timeline domain.** Add the pure timeline types and persisted TimelineMap.
2. **Atomic media protocol.** Apply temporary-output validation and promotion to cut,
   reframe, caption burn, and export.
3. **Ripple cut.** Replace `_kept_spans` behavior with normalized edit-map construction.
4. **Transcript order.** Centralize logical word order and anchored inserted-text behavior.
5. **Caption mapping.** Intersect in SourceTime and map cues into ClipTime.
6. **Diarization and reframe mapping.** Normalize overlapping turns, map them once, and make
   speaker-side ties deterministic.
7. **Golden media suite.** Require real generated media in CI instead of silently skipping
   FFmpeg scenarios.

The generated fixture contains colored regions, seeded tones, sparse keyframes, and two
speaker turns. It covers mid-GOP starts, head/middle/tail/all deletion, inserted text,
fallback pan, and cancellation during every FFmpeg stage.

**Acceptance gate:**

1. Every edit produces a monotonic SourceTime to ClipTime to OutputTime map whose slice
   durations sum to clip duration.
2. Head, tail, and middle deletions produce media within 200 ms of mapped duration. An
   all-deleted edit returns `empty_after_edits` and publishes nothing.
3. No caption cue includes a deleted word or extends beyond clip duration. Caption alignment
   p95 is within 100 ms of the canonical mapped word timeline.
4. A source speaker change at 186 seconds in a 180-second window with a two-second earlier
   deletion maps to clip second 4, in both face-track and fallback-pan paths.
5. Inserted text reads in deterministic command order across TXT, JSON, SRT, VTT, and ASS and
   never creates a zero-duration spoken event.
6. Canonical reframe-track JSON, TimelineMap JSON, captions, and stage-parameter manifests are
   byte-stable across repeated runs with identical inputs.
7. Published renders pass `ffprobe`, duration, stream, dimensions, and decode checks.
8. Cancellation publishes no final artifact, never replaces the prior current Render, and
   leaves no accepted partial file.
9. Portrait and landscape outputs preserve geometry and match the requested aspect exactly.
10. The full engine suite and golden media suite pass twice consecutively.

Bit-identical MP4 bytes across hardware encoders are not required. Reproducibility means an
identical editorial plan, timeline, captions, stage parameters, and verifiable media
properties.

Canonical semantic manifests sort JSON object keys, preserve array order where editorially
meaningful, normalize paths relative to the Delivery root, and exclude entity IDs, execution
IDs, timestamps, absolute paths, encoder-generated container metadata, and other declared
nondeterministic fields. Manual, MCP, and repeated-run equality gates use this canonical
representation.

**Rollback:** immutable ClipRevisions and Renders allow the current pointer to return to the
last validated revision. Old artifacts remain reachable until the new revision passes the
grace period.

**Estimated effort:** 7-12 focused engineering days. Pure timeline work may begin while Phase
1 is underway; persisted ClipRevision integration waits for the durable domain core.

### Phase 3: one golden workflow

**Goal:** collapse the broad interface into one honest, excellent journey:

```text
Import -> inspect source/transcript -> review storyboard -> approve
       -> edit -> preflight -> render -> deliver
```

**Scope:**

1. Add real local file import through an engine multipart/copy endpoint. Imported browser
   files become Spool-managed Source artifacts. URL imports retain provenance and display
   validation errors before queueing.
2. Make the Source page the project workspace. It owns transcript inspection, Candidate
   storyboard, approvals, Clip revisions, and Render history.
3. Candidate cards show title, rationale, named score factors, boundaries, speakers, crop
   preview, caption preview, and approval state.
4. Replace job-derived Studio state with domain-resource queries and event invalidation.
5. Restore aspect, reframe mode, crop boxes, caption style, timeline, and current revisions
   whenever an editor reopens.
6. Make prerequisite waiting fail closed. Failed, cancelled, interrupted, or timed-out work
   stops the chain and shows recovery options.
7. Add a render preflight for boundary quality, caption overflow/timing, crop confidence,
   silence, black frames, disk space, aspect, duration, and estimated output size.
8. Deliver a versioned open bundle containing:
   - MP4;
   - SRT, VTT, and ASS captions;
   - transcript JSON;
   - thumbnail;
   - suggested title and copy;
   - `spool-project.json` with IDs, versions, source provenance, TimelineMap, settings,
     provider-receipt IDs and digests, artifact checksums, and manifest version. Exact remote
     request content remains local and is not copied into a Delivery bundle by default.
9. Make the shell responsive and accessible:
   - desktop: rail, workspace, optional agent;
   - tablet: collapsible navigation and agent drawer;
   - phone: import, Candidate review, approvals, queue monitoring, and delivery;
   - semantic buttons, switches, tabs, dialogs, labels, focus management, and live progress.

OTIO and FCPXML export follow only after the `spool-project.json` manifest is stable.

**Acceptance gate:**

1. A clean user imports one local file and one valid URL without a terminal. Invalid inputs
   never appear successful.
2. From one Source, the user approves and renders three Clips, closes the app at every major
   step, and resumes the same entity, revision, and settings after restart.
3. Failed discovery, reframe, caption, and render attempts create no phantom Source, Clip, or
   current Render.
4. The first-clip workflow completes without agent assistance and without hidden controls.
5. Every Delivery verifies its checksums and can be understood outside Spool from the bundle
   contents alone.
6. The golden workflow has no horizontal clipping at 390, 768, or 1440 CSS pixels.
7. The golden workflow is keyboard-completable and meets WCAG 2.2 AA accessible-name, focus,
   contrast, target-size, and status-announcement requirements.
8. Playwright plus axe-core reports zero `serious` or `critical` violations on the golden
   workflow. A checked-in manual keyboard/focus checklist covers behavior automation cannot
   prove.
9. Manual and MCP operations against the same domain plan produce canonically equal
   ClipRevision and Render semantic manifests.

**Rollback:** hide the new workspace behind a local feature flag until migration parity and
restart tests pass. Domain records and artifacts remain valid if the Studio route is rolled
back.

**Estimated effort:** 7-10 focused engineering days across import/workspace, editor-state,
preflight/delivery, and responsive-accessibility PRs.

### Phase 4: private intelligence and safe agent

**Goal:** turn privacy, inspectability, and reversible automation into the product moat.

**Scope:**

1. Add a provider interface with Ollama as the first supported local reasoning provider.
   MLX and llama.cpp-compatible adapters are follow-on providers, not Phase 4 blockers.
2. Default to no transcript egress. Remote providers are explicit opt-ins.
3. Persist an EgressReceipt for each remote action: provider, purpose, data categories,
   payload digest, exact locally viewable request, approval time, response status, and related
   entity IDs.
4. Replace fire-and-forget agent mutation with:

   ```text
   request -> frozen proposal -> visible diff -> approve -> execute -> verify -> undo
   ```

5. Read-only agent questions may run immediately. Transcript changes, boundary changes,
   reframe settings, caption settings, remote egress, expensive renders, and destructive
   actions require approval.
6. Approval executes the exact frozen proposal under an immutable execution ID. It does not
   re-plan or change arguments after confirmation.
7. Undo selects or creates a compensating TranscriptRevision or ClipRevision. It never
   pretends to reverse an external side effect.
8. Add local TasteProfiles per creator or client. Initial learning is deterministic and based
   on explicit approvals, rejections, boundary changes, crop changes, and caption choices.
   Profiles are inspectable, editable, exportable, resettable, disableable, and deletable.

**Acceptance gate:**

1. With Ollama configured and non-loopback sockets blocked, the local-file Phase 3 workflow
   succeeds and the integration test observes zero non-loopback connections.
2. Every remote reasoning request has an EgressReceipt visible before and after execution.
3. No agent mutation begins without a persisted approved proposal and immutable execution ID.
4. Agent and manual execution of the same plan produce canonically equal revision and Render
   semantic manifests.
5. Undo restores a prior revision and UI state in a restart-safe test.
6. Cancelling an agent execution prevents all not-yet-started mutations and records the final
   partial result honestly.
7. Recorded approval and rejection feedback produces a deterministic ranking or style change
   that the user can inspect and reverse.
8. Disabling or deleting a TasteProfile immediately returns ranking and style selection to
   explicit defaults.
9. Purging a linked Source or Clip durably records `purge_pending` before removing exact
   managed request bytes, then nulls `request_artifact_id`, marks the Artifact `purged`, and
   preserves only the redacted minimal audit record and payload digest.

**Rollback:** provider adapters and taste learning are feature-flagged independently.
Revisioned domain state remains valid if the agent UI is disabled. Egress receipts are never
deleted by a provider rollback.

**Estimated effort:** 10-15 focused engineering days across provider/privacy, transactional
agent, and TasteProfile PRs.

### Phase 5: automation, connectors, and real analytics

**Entry condition:** Phases 0-3 pass in a release build and the manual workflow has been
dogfooded on real projects. Phase 4 may continue in parallel, but automation cannot bypass its
approval and egress rules when agent behavior is involved.

**Scope order:**

1. Reintroduce Recipes as saved domain-operation plans.
2. Reintroduce Watches as triggers that call those same plans and create reviewable domain
   entities.
3. Add publishing through selected platform connectors or established integrations. Do not
   build an in-house scheduler before connector reliability and token-storage requirements are
   specified.
4. Add analytics only from genuine publishing data with provenance. Never restore decorative
   dashboards.
5. Feed outcome data into TasteProfiles only with visible factors and an off switch.

Recipes, Watches, manual actions, and agent actions must call the same domain services. No
automation-only path may write files or mutate current revisions directly.

**Acceptance gate for any Phase 5 child spec:**

1. Triggering the same Recipe manually, through a Watch, and through MCP produces the same
   frozen plan and domain manifest.
2. Duplicate triggers are idempotent.
3. Connector credentials use the OS keychain or an equivalently reviewed secret store.
4. Connector failure does not change Delivery integrity or report a successful publish.
5. Every analytics value identifies its platform, account, content ID, collection time, and
   freshness.

Phase 5 effort is intentionally not estimated until connector choices and platform access are
known.

## 8. Cross-phase verification

### Required test layers

| Layer        | Required coverage                                                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| Pure unit    | Timeline normalization/mapping, state transitions, migration transforms, domain invariants, manifest validation, taste calculations. |
| Concurrency  | Barrier-controlled cancel/start/finish races, stale generations, persistence ordering, restart interruption, capacity overflow.      |
| Integration  | SQLite repositories, Flask contracts, artifact promotion/reconciliation, signed media, provider consent, egress receipts.            |
| Contract     | Authoritative OpenAPI 3.1 contract against Flask, TypeScript client, Python client, CLI, MCP, and event payloads.                    |
| Component    | Loading, empty, error, progress, keyboard, editor restoration, and approval-diff states.                                             |
| Golden media | Generated audiovisual fixture through cut, reframe, caption, export, cancellation, and decode.                                       |
| E2E          | URL-to-Delivery, local-file-to-Delivery, restart at each stage, token mode, offline local-provider mode, responsive/a11y workflow.   |
| Migration    | Dry run, idempotency, collision, missing artifact, injected failure, backup checksums, rollback, and legacy alias compatibility.     |

### Verification commands

Each implementation PR runs its focused tests plus the relevant repository floor:

```bash
cd engine && python3 -m pytest -q
pnpm typecheck
pnpm lint
pnpm test
pnpm build
pnpm format:check
```

FFmpeg and `ffprobe` are required in CI for the golden media suite. A missing binary fails the
suite instead of silently skipping it. Existing formatting debt must be repaired in an
isolated mechanical change before `format:check` becomes a merge gate; functional PRs do not
mix broad formatting rewrites with behavior changes.

### Release gates

- **No data loss:** destructive operations are explicit, dependency-aware, recoverable, and
  unrelated to queue history.
- **Restart safe:** every major state can survive process termination and reconstruct the
  same current entity and revision.
- **Deterministic plan:** identical inputs produce identical TimelineMaps, captions, settings,
  and manifests.
- **Validated artifacts:** every published media Artifact probes and decodes; text, JSON,
  captions, and manifests pass their declared parser or schema validator.
- **Offline honest:** offline means no non-loopback sockets; URL ingestion is never described
  as offline.
- **Manual/agent parity:** both surfaces reach the same domain operations and contracts.
- **Accessible:** the golden workflow is keyboard-completable and responsive.
- **Truthful:** no placeholder data, fake controls, swallowed errors, or undisclosed egress.

## 9. Performance and capacity budgets

Hardware varies, so media speed is measured as a regression ratio against checked-in
reference fixtures rather than a universal wall-clock promise.

1. A library list request returns at most the requested page and performs no filesystem scan.
2. Event heartbeats contain no full Library snapshot.
3. Pending work is bounded according to Section 6.6.
4. Timeline mapping and manifest generation are deterministic pure operations and complete in
   linear time relative to slices or cues.
5. Phase 2 records baseline and post-change wall time for each FFmpeg stage on the same
   fixture; a regression above 20% requires explanation and approval.
6. Phase 3 records initial route bundle size, workspace interaction latency, and 390/768/1440
   layout screenshots. A regression above 20% requires explanation and approval.
7. Import and render preflight checks available disk space before starting work that can
   exceed the configured reserve.

## 10. Rollout policy

- Phase 0 ships as isolated fixes with focused regression tests.
- Phase 1 uses a feature flag and strangler migration. It does not dual-write.
- Phase 2 publishes immutable new revisions and keeps the last validated Render current until
  replacement succeeds.
- Phase 3 enables the new workspace only after migration, restart, and golden-media gates pass.
- Phase 4 enables local and remote providers independently and defaults remote egress off.
- Phase 5 features are individually hidden until their own child specs and gates pass.

Each release records `api_version`, `contract_version`, `storage_schema_version`, application
commit, provider configuration, and Delivery manifest version in diagnostics.

## 11. Risks and mitigations

| Risk                                                                                           | Mitigation                                                                                                                                                                               |
| ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQLite migration loses or mis-links user work.                                                 | Dry-run parity report, checksummed legacy backups, validated temporary-database promotion, stable IDs, integrity checks, no dual writes, and read-only legacy retention through Phase 3. |
| New job abstraction becomes a risky executor rewrite.                                          | Keep separate worker pools and introduce only a shared persisted transition service.                                                                                                     |
| Timeline refactor mixes too many conversions in one PR.                                        | Split pure timeline, ripple, captions, and reframe into reviewable conversion boundaries.                                                                                                |
| Atomic file promotion and metadata transaction cannot be literally one filesystem transaction. | Use immutable paths plus startup reconciliation for the documented rename/commit crash window.                                                                                           |
| Hardware encoders produce different bytes.                                                     | Compare semantic manifests and media properties, not cross-hardware MP4 hashes.                                                                                                          |
| Hiding future surfaces deletes or strands user data.                                           | Hide navigation only; preserve Recipes, Watches, settings, and artifacts.                                                                                                                |
| Local-model setup is too heavy for first use.                                                  | The manual golden workflow remains fully usable; Ollama is optional and remote reasoning is explicit opt-in.                                                                             |
| The master spec is too large for one safe implementation pass.                                 | Require child specs and PR-sized plans before code changes.                                                                                                                              |

## 12. Explicitly out of scope

- Cloud hosting, organizations, multi-user authorization, SSO, and billing.
- Native mobile apps or Tauri/Electron packaging.
- A distributed queue, rendering farm, or rewrite of Flask/Next.js.
- Full professional NLE replacement or multi-track audio production.
- Automatic B-roll and emoji generation during the recovery program.
- Opaque or black-box virality scores.
- Direct social publishing or analytics before a Phase 5 connector spec is approved.
- Remote model training.
- Moving or content-addressing every existing media file during the SQLite migration.
- Bit-for-bit MP4 equality across hardware encoders.
- Silent third-party uploads or a blanket "all local" claim while a remote provider is active.
- Deleting existing Recipes, Watches, brand kits, or historical metadata merely because their
  UI is hidden.

## 13. Required implementation decomposition

This master spec defines product direction, architecture, order, and release gates. Before
implementation, create and approve these file-level child specs and plans:

1. **Safety fuse:** destruction, cancellation, security, privacy truth, authentication, and
   contract repairs.
2. **Durable core:** SQLite schema, repositories, strangler migration, job transitions,
   domain APIs, artifact reconciliation, and rollback.
3. **Media correctness:** timeline types, ripple edits, captions, diarization/reframe,
   immutable revisions, atomic media, and golden fixtures.
4. **Golden workflow:** real import, domain-backed Studio workspace, editor restoration,
   preflight, delivery bundles, responsive layout, and accessibility.
5. **Private moat:** provider abstraction, egress receipts, transactional agent, undo, and
   TasteProfiles.

After Phase 0, work may proceed in three lanes:

```text
Lane A: SQLite/domain model -> migration -> domain API
Lane B: pure timeline -> media corrections -> validated render
Lane C: responsive/a11y foundations -> domain-backed workspace

Lane A + Lane B + Lane C -> golden workflow
golden workflow + immutable revisions -> private agent and taste learning
```

Lane B's pure timeline work and Lane C's semantic component work may begin while Lane A is in
progress. Final media persistence depends on ClipRevision. The golden workflow waits for all
three lanes.

## 14. Master definition of done

The recovery and differentiation program is complete when:

1. Every phase gate through Phase 4 passes in CI and on a clean local install.
2. Queue history can be erased without changing Library entities or published artifact bytes.
3. Migration preserves stable IDs and all explainable existing work.
4. Process termination at every major stage produces a legal restart state and no phantom
   entities.
5. The golden media suite proves mapped cuts, captions, framing, cancellation, validation,
   and deterministic manifests.
6. A first-time user completes the golden workflow from local file and URL at desktop and
   narrow viewport widths without a terminal.
7. The workflow works with non-loopback sockets blocked when Ollama is configured.
8. Every remote operation has informed consent and an inspectable EgressReceipt.
9. Every agent mutation uses a frozen approved proposal, durable execution record, and real
   revision-based undo.
10. Deliveries remain useful and reproducible outside Spool through their open export bundle.
