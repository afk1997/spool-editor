# Private Clip Foundry Phases 1-5 Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Ready for execution planning; Phase 0 is complete, Phases 1-5 are unstarted

**Phase 0 completion commit:** `8060b88` (`docs(phase0): record completed safety fuse`)

**Goal:** Finish Spool's transition from a fragile job-backed alpha into a restart-safe Private Clip Foundry with correct media semantics, one excellent manual workflow, private/reversible intelligence, and automation that reuses the same audited domain operations.

**Architecture:** Keep the Flask engine, Next.js Studio, Python CLI/MCP server, separate worker pools, FFmpeg media stack, and ordinary user-visible files. Move identity and relationships into SQLite domain records, make files immutable validated Artifacts, make edits immutable revisions over a typed timeline, expose one OpenAPI-governed API to every client, then rebuild the Studio and agent on those operations.

**Tech Stack:** Python 3.11+ · Flask · SQLite WAL · pytest · FFmpeg/ffprobe · TypeScript 5 · Next.js 16 / React 19 · Vitest / Testing Library · Playwright · axe-core · pnpm 10 / Turborepo · MCP · Ollama.

---

## 1. What this document is

This is the single program map for everything after Phase 0. It translates the approved
master design in
`docs/superpowers/specs/2026-07-13-private-clip-foundry-roadmap-design.md` into ordered,
reviewable delivery slices.

The master design remains authoritative for product invariants, schema semantics, API
compatibility, security, and release gates. This plan adds:

- the build order within each phase;
- the concrete files and ownership boundaries involved;
- tests and evidence required before moving to the next slice;
- rollback behavior and commit boundaries;
- the decisions that must be approved before a Phase 5 connector is selected.

This plan intentionally does not place five phases into one implementation branch. Each
phase gets a short child spec and file-level execution plan immediately before its code work,
because the repository will change substantially after every gate. The child documents may
refine exact functions and line numbers, but they may not weaken this plan or the master
design.

## 2. Current checkpoint

| Phase                        | State       | Outcome                                                                                                                                                                                                            |
| ---------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0 — Safety and product truth | Complete    | Destructive cleanup removed; legacy attempts fenced; queues bounded; network/auth/origin protections added; authenticated same-origin Studio proxy shipped; mutating Agent/MCP paths fused off; contracts aligned. |
| 1 — Durable domain core      | Not started | SQLite identity, durable attempts, artifact lifecycle, migration, domain API.                                                                                                                                      |
| 2 — Media correctness        | Not started | Typed timeline, correct ripple/caption/reframe mapping, reproducible validated renders.                                                                                                                            |
| 3 — Golden workflow          | Not started | Real import-to-delivery Studio journey, restored state, preflight, open bundles, responsive/a11y release.                                                                                                          |
| 4 — Private intelligence     | Not started | Ollama, egress receipts, frozen agent proposals, approval/execution/undo, TasteProfiles.                                                                                                                           |
| 5 — Automation/connectors    | Not started | Domain-backed Recipes/Watches, selected publishing connectors, provenance-backed analytics.                                                                                                                        |

Phase 0's in-memory attempt guards, JSON-store compatibility, fail-closed reasoning, and
same-origin transport are foundations to preserve during migration. Phase 1 replaces only
the temporary persistence mechanics; it must retain or strengthen every Phase 0 behavior.

## 3. Build order and dependency graph

```text
Phase 0 complete (8060b88)
        |
        +------------------------------+
        |                              |
        v                              v
Lane A: Phase 1 durable core     Lane B: Phase 2A pure TimelineMap
        |                              |
        +---------------+--------------+
                        v
             Phase 2 persisted media correctness
                        |
        +---------------+----------------+
        |                                |
        v                                v
Lane C: responsive/a11y base      domain-backed Studio data layer
        |                                |
        +---------------+----------------+
                        v
              Phase 3 golden workflow
                        |
                        v
        Phase 4 private intelligence and safe agent
                        |
                        v
    Phase 5 Recipes -> Watches -> connector -> analytics
```

Allowed overlap:

- Phase 2's pure timeline types/tests may begin while Phase 1 repositories are underway.
- Phase 3's semantic component and responsive-shell foundations may begin during Phase 2.
- Phase 4 provider exploration may begin during Phase 3, but no agent mutation can ship
  before immutable revisions and the Phase 3 manual workflow pass.
- Phase 5 child-spec research may begin after Phase 3. No connector mutation may bypass
  Phase 4 egress or approval rules.

Everything else is sequential. In particular, persisted media integration waits for the
Phase 1 `ClipRevision` and `Artifact` contracts, and the Studio cutover waits for all three
lanes.

## 4. Program-wide execution rules

1. Start every phase branch from the prior phase's verified completion commit. Use the
   `codex/` prefix and an isolated worktree.
2. Write and approve the phase child spec and execution plan before production code. Record
   the exact baseline test counts and known pre-existing failures in that plan.
3. Use red-green-refactor for each behavioral slice. Observe the focused test fail for the
   intended reason before implementing the behavior.
4. Keep migrations, runtime cutovers, media semantics, UI rewrites, and dependency-only
   changes in separate commits.
5. Never dual-write SQLite and legacy JSON. Migration is a locked, validated cutover with
   read-only legacy retention through the first Phase 3 release.
6. Never move or rewrite existing media just to adopt SQLite. Domain rows point to existing
   paths through validated `Artifact` records.
7. `/api/v1` remains compatible through the first tagged Phase 3 release and at least 90
   days afterward. Removals require a separately approved cleanup spec.
8. Jobs describe execution attempts only. They cannot own Source, Transcript, Clip, Render,
   or Delivery identity.
9. A visible final file is never evidence of success by itself. Publication requires
   validation, atomic promotion, and a committed `published` Artifact record.
10. Preserve manual REST/CLI operations while agent mutations are disabled. Re-enable agent
    mutation only through Phase 4's frozen-proposal contract.
11. Keep Recipes, Watches, Brand Kits, settings, and historical metadata even while their
    entry points are hidden.
12. Do not mix unrelated formatting cleanup with behavior. Every commit runs
    `git diff --check` and lists its staged paths before committing.
13. Long acceptance runs must terminate their captured process trees and prove the test
    ports are free afterward.
14. A phase is complete only when its entire acceptance matrix is recorded in its plan and
    a documentation-only completion commit marks the exact behavior checkpoint.

## 5. Cross-phase target file map

The child plans should preserve these ownership boundaries unless a committed spec amendment
approves a better decomposition.

### Durable state and domain operations

- Create `engine/state/database.py` — SQLite connection policy, transactions, WAL/foreign-key
  configuration, integrity checks, and atomic database promotion helpers.
- Create `engine/state/migrations.py` and `engine/state/schema/0001_domain.sql` — checksummed,
  versioned schema installation.
- Create `engine/state/models.py` — typed immutable records and enums for domain boundaries;
  no SQL or Flask behavior.
- Create `engine/state/artifact_repository.py` — Artifact reservation, publication, state
  transitions, reachability, and reconciliation queries.
- Create `engine/state/library_repository.py` — Sources, Transcripts, TranscriptRevisions,
  LegacyAliases, trash, restore, and current-pointer transactions.
- Create `engine/state/clip_repository.py` — DiscoveryRuns, Candidates, Clips,
  ClipRevisions, Renders, Deliveries, and current-pointer transactions.
- Create `engine/state/job_repository.py` — JobAttempt generation and compare-and-set
  transitions.
- Create `engine/state/idempotency_repository.py` — request-key reservation, canonical
  request digest, completed response replay, conflict detection, and bounded retention for
  side-effecting imports and renders.
- Create `engine/state/egress_repository.py` — EgressReceipt persistence and deletion links;
  populated in Phase 4 but installed with the Phase 1 schema.
- Create `engine/domain/library.py`, `engine/domain/clips.py`, `engine/domain/jobs.py`, and
  `engine/domain/deliveries.py` — the only orchestration layer allowed to mutate related
  repositories and current pointers.
- Create `engine/state/legacy_migration.py` — dry-run inventory, stable-ID import,
  maintenance-mode cutover, diagnostics, and verification-only restart behavior.
- Create `engine/artifact_reconciler.py` — startup repair/quarantine and trash purge.

### API and shared contracts

- Create `docs/api/openapi.yaml` — authoritative OpenAPI 3.1 contract.
- Create `engine/routes/domain_v1.py` — domain resources while
  `engine/routes/api_v1.py` retains compatibility adapters.
- Split shared TypeScript contracts into `packages/types/src/domain.ts`,
  `packages/types/src/events.ts`, and `packages/types/src/errors.ts`; keep
  `packages/types/src/index.ts` as the public export surface.
- Split domain methods into `packages/api-client/src/domain.ts`; keep transport/auth/proxy
  behavior in `packages/api-client/src/index.ts` until a child plan proves a safe extraction.
- Extend `engine/trove_client.py`, `engine/cli.py`, and `engine/mcp_server.py` only as clients
  of the same domain endpoints.
- Create `contracts/v1/semantic-manifest.schema.json` and
  `contracts/v1/spool-project.schema.json` for deterministic comparison and Delivery
  validation.

### Media correctness

- Create `engine/clip/timeline.py` — pure named time types and immutable `TimelineMap`.
- Create `engine/clip/semantic_manifest.py` — canonical editorial-plan serialization and
  nondeterministic-field exclusion.
- Create `engine/clip/preflight.py` — Phase 3 render checks with structured findings.
- Modify `engine/clip/cutter.py`, `engine/clip/captioner.py`,
  `engine/clip/backhalf/ass_captions.py`, `engine/clip/reframe.py`,
  `engine/clip/face_track.py`, `engine/clip/backhalf/pan_expr.py`,
  `engine/clip/_ffmpeg.py`, `engine/clip/exporter.py`, and `engine/clip_runner.py` to consume
  the typed map and Phase 1 Artifact service.
- Create `engine/tests/fixtures/golden_media.py` and
  `engine/tests/test_golden_media_pipeline.py` for generated audiovisual fixtures and real
  FFmpeg cancellation/publication coverage.

### Studio golden workflow

- Create `apps/studio/src/lib/domain-client.ts` and
  `apps/studio/src/lib/domain-events.ts` — typed resource queries and delta invalidation.
- Create `apps/studio/src/components/workspace/` with focused Source summary, transcript,
  storyboard, candidate, revision, render-history, preflight, and delivery components.
- Create `apps/studio/src/components/import/local-file-import.tsx` and
  `apps/studio/src/components/import/url-import.tsx` — honest import paths with structured
  errors.
- Refactor `apps/studio/src/components/spool/context.tsx` so it no longer derives product
  entities from jobs; retain it only for shell-local state or replace it through a focused
  adapter.
- Modify the existing Source/editor/library/import/queue routes instead of introducing a
  second competing navigation model.
- Create `apps/studio/e2e/golden-workflow.spec.ts` and
  `apps/studio/e2e/golden-workflow-a11y.spec.ts` for restart, responsive, keyboard, proxy,
  and Delivery evidence.

### Private intelligence and automation

- Create `engine/reasoning/base.py`, `engine/reasoning/ollama.py`, and
  `engine/reasoning/registry.py` — provider-neutral request/response contract and loopback
  Ollama adapter.
- Create `engine/agent_changes.py` — proposal, diff, approval, execution, cancellation,
  verification, and revision-based revert.
- Create `engine/taste_profiles.py` — deterministic explicit-feedback model and lifecycle.
- Modify `engine/clip/agent.py` and `engine/clip/agent_tools.py` only to call domain services
  through `engine/agent_changes.py`.
- Create `engine/publishing/base.py` and one provider-specific adapter only after its Phase 5
  child spec approves scope, credentials, idempotency, and failure semantics.
- Refactor `engine/recipes.py`, `engine/watches.py`, and `engine/watcher.py` to store/trigger
  frozen domain-operation plans rather than directly writing legacy state or files.

## 6. Phase 1 — durable domain core and recovery

**Outcome:** Product identity and current versions survive queue cleanup, concurrency,
crashes, and restart. SQLite becomes the one live metadata backend without moving media or
silently losing legacy records.

**Estimated effort:** 7-12 focused engineering days across seven reviewable slices.

### Slice 1A — schema, connection policy, and migration runner

**Files:**

- Create: `engine/state/__init__.py`
- Create: `engine/state/database.py`
- Create: `engine/state/migrations.py`
- Create: `engine/state/schema/0001_domain.sql`
- Create: `engine/state/models.py`
- Test: `engine/tests/test_state_database.py`
- Test: `engine/tests/test_state_migrations.py`

- [ ] Write failing tests for WAL, `foreign_keys=ON`, `busy_timeout=5000`,
      `synchronous=FULL`, fixed-millisecond UTC timestamps, checksum mismatch, idempotent schema
      install, failed-migration rollback, `integrity_check`, and `foreign_key_check`.
- [ ] Install the complete Section 6.1 semantic schema, including deferred current-pointer
      relationships and every required index.
- [ ] Prove published Artifacts require size/checksum/validation, cross-Source foreign keys
      fail, and only one active JobAttempt generation exists per logical job.
- [ ] Run `cd engine && .venv/bin/python -m pytest -q tests/test_state_database.py tests/test_state_migrations.py` twice; expect both runs to pass against fresh temporary roots.
- [ ] Commit only this slice as `feat(state): add versioned SQLite domain schema`.

### Slice 1B — repositories and transaction-scoped domain services

**Files:**

- Create: `engine/state/artifact_repository.py`
- Create: `engine/state/library_repository.py`
- Create: `engine/state/clip_repository.py`
- Create: `engine/state/job_repository.py`
- Create: `engine/state/idempotency_repository.py`
- Create: `engine/state/egress_repository.py`
- Create: `engine/domain/__init__.py`
- Create: `engine/domain/library.py`
- Create: `engine/domain/clips.py`
- Create: `engine/domain/jobs.py`
- Create: `engine/domain/deliveries.py`
- Test: `engine/tests/test_state_repositories.py`
- Test: `engine/tests/test_domain_services.py`

- [ ] Test immutable revision creation, same-owner current pointers, stable Source/Clip IDs,
      failed import retention, unpublished Render rejection, Delivery role completeness, and
      cursor ordering under equal timestamps.
- [ ] Implement narrow repositories that accept a caller-owned SQLite transaction; keep
      multi-entity invariants in domain services.
- [ ] Make Render publication pointers write-once and allow `clips.current_render_id` to
      move only to a published Render whose Artifacts validate.
- [ ] Reserve idempotency keys transactionally for imports and renders. Replay the stored
      response for an identical canonical request, reject reuse with different parameters, and
      recover an in-progress reservation honestly after restart.
- [ ] Treat every `delivery_artifacts` row as a reachability root and require the roles
      `video`, `caption_srt`, `caption_vtt`, `caption_ass`, `transcript_json`, `thumbnail`,
      `copy`, and `project_manifest`.
- [ ] Run both focused files plus `cd engine && .venv/bin/python -m pytest -q`; expect no
      Phase 0 regression.
- [ ] Commit as `feat(domain): add durable repositories and services`.

### Slice 1C — persisted JobAttempt state machine

**Files:**

- Modify: `engine/domain/jobs.py`
- Modify: `engine/jobs.py`
- Modify: `engine/transcribe_jobs.py`
- Modify: `engine/clip_jobs.py`
- Modify: `engine/job_capacity.py`
- Modify: `engine/process_ownership.py`
- Test: `engine/tests/test_job_attempt_repository.py`
- Test: `engine/tests/test_job_attempt_concurrency.py`
- Modify tests: `engine/tests/test_jobs.py`, `engine/tests/test_transcribe_jobs.py`,
  `engine/tests/test_clip_jobs.py`

- [ ] Encode the legal transition table from the master design as data and test every legal
      edge plus every rejected edge.
- [ ] Replace in-memory attempt identity as the authority with transactional compare-and-set
      over `(id, logical_job_id, generation, state)`; retain Phase 0 staging and process guards
      as defense in depth.
- [ ] Keep the download, transcription, and media worker pools separate. Route transitions
      through the shared service without introducing a generic executor rewrite.
- [ ] Make retry insert generation `n + 1` with `retry_of_id`; never reset a terminal row.
- [ ] Persist cancel/pause intent before terminating owned processes; allow resume only for
      verified resumable partial input.
- [ ] Reconcile running work on startup to queued/paused only when resumable, otherwise to
      `interrupted`.
- [ ] Run 100 barrier-controlled submit/cancel/retry/restart iterations and assert one active
      generation, legal terminal state, no resurrected callback, and balanced capacity leases.
- [ ] Commit as `feat(jobs): persist attempts and guarded transitions`.

### Slice 1D — Artifact publication, reconciliation, Trash, and purge

**Files:**

- Modify: `engine/state/artifact_repository.py`
- Create: `engine/artifact_reconciler.py`
- Modify: `engine/attempt_staging.py`
- Modify: `engine/domain/library.py`
- Modify: `engine/domain/clips.py`
- Test: `engine/tests/test_artifact_repository.py`
- Test: `engine/tests/test_artifact_reconciler.py`
- Test: `engine/tests/test_trash_lifecycle.py`

- [ ] Test the reserve → write attempt-local same-directory path → validate → `os.replace`
      → publish transaction, injecting a crash before and after each boundary.
- [ ] Validate media with ffprobe, stream/dimension/nonzero-duration checks, and a decode
      probe; validate JSON/text/captions/manifests with their parser or schema.
- [ ] On restart, repair a promoted-but-uncommitted immutable file when ownership can be
      proven; otherwise quarantine it. Never delete unknown user media.
- [ ] Implement dependency-count preview, active-job refusal, logical trash, seven-day
      restore, explicit purge, and reachability-based garbage collection within the managed
      library only.
- [ ] Prove queue-history deletion changes no domain row, reachable Artifact, or file byte.
- [ ] Commit as `feat(artifacts): publish atomically and add recoverable trash`.

### Slice 1E — legacy inventory and strangler cutover

**Files:**

- Create: `engine/state/legacy_migration.py`
- Create: `engine/migration_lock.py`
- Modify: `engine/app.py`
- Modify: `engine/config.py`
- Modify: `engine/jobs_store.py`
- Modify: `engine/routes/api_v1.py`
- Test: `engine/tests/test_legacy_migration.py`
- Test: `engine/tests/test_migration_cutover.py`
- Test: `engine/tests/test_migration_restart.py`

- [ ] Inventory and checksum `jobs.json`, `transcribe_jobs.json`, `clip_jobs.json`, clip
      metadata, transcript/caption/candidate/render artifacts, Brand Kits, Recipes, Watches, and
      settings without mutating any file.
- [ ] Emit a deterministic dry-run report with counts, stable IDs, paths, missing files,
      dangling relationships, collisions, and explainable exclusions.
- [ ] Under a process-wide lock, return `503 migration_in_progress`, drain safe work,
      interrupt the rest, stop pools, snapshot/checksum legacy metadata, import into
      `state.sqlite3.migrating`, reconcile, integrity-check, checkpoint, close, and fsync.
- [ ] Atomically promote the validated database and switch reads/writes only after success;
      do not dual-write.
- [ ] Prove an injected failure deletes only the temporary database, leaves legacy bytes
      unchanged, and prevents workers from starting.
- [ ] Prove a live database makes subsequent startup verification-only even if legacy bytes
      drift.
- [ ] Commit as `feat(migration): cut legacy state over to SQLite`.

### Slice 1F — domain API, authoritative OpenAPI, and client compatibility

**Files:**

- Create: `docs/api/openapi.yaml`
- Create: `engine/routes/domain_v1.py`
- Modify: `engine/routes/__init__.py`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/app.py`
- Create: `packages/types/src/domain.ts`
- Create: `packages/types/src/events.ts`
- Create: `packages/types/src/errors.ts`
- Modify: `packages/types/src/index.ts`
- Create: `packages/api-client/src/domain.ts`
- Modify: `packages/api-client/src/index.ts`
- Modify: `engine/trove_client.py`
- Modify: `engine/cli.py`
- Modify: `engine/mcp_server.py`
- Test: `engine/tests/test_openapi_contract.py`
- Test: `engine/tests/test_domain_api.py`
- Create: `apps/studio/test/domain-api-client.test.ts`

- [ ] Define Sources, Transcripts, Discovery, Clips, Renders, Deliveries, Jobs, Agent
      changes, and EgressReceipt resources with explicit version fields and structured errors.
- [ ] Add cursor pagination and a versioned delta/invalidation event stream whose heartbeat
      does not serialize the Library.
- [ ] Keep existing routes and full-snapshot events working through compatibility adapters;
      add warning headers to deprecated fields without removing them.
- [ ] Generate or validate Python/TypeScript shapes from `docs/api/openapi.yaml` and run the
      same fixtures through Flask, TS client, Python client, CLI, and MCP.
- [ ] Prove token auth, same-origin proxy media/SSE, originless authenticated CLI/MCP, and
      `LegacyAlias` resolution still work.
- [ ] Commit as `feat(api): expose versioned domain resources`.

### Slice 1G — scale, restart, rollback, and Phase 1 certification

**Files:**

- Create: `engine/tests/test_domain_scale.py`
- Create: `engine/tests/test_phase1_restart_matrix.py`
- Create: `engine/tests/test_phase1_acceptance.py`
- Update: this plan's Phase 1 checklist and evidence section only after tests pass.

- [ ] On a 10,000-Source fixture, prove a 100-item Library page is at most 1 MiB, uses at
      most five SQL statements, and its filtered/sorted query plan uses the declared index.
- [ ] Kill the engine during queued, running, cancelling, finalizing, database promotion,
      artifact rename, and Trash purge windows; prove the documented recovery state.
- [ ] Clear all job history and compare domain counts, Artifact reachability, checksums, and
      Library responses before and after restart.
- [ ] Run the repository floor in Section 11, the authenticated URL-to-render Phase 0 E2E,
      migration twice against isolated copies, and process/port leak checks.
- [ ] Record the exact passing counts, database schema version, migration fixture checksum,
      and behavior commit in the Phase 1 child plan.
- [ ] Commit evidence as `docs(phase1): record durable core completion`.

**Phase 1 exit gate:** All thirteen acceptance items in the master design pass. The
application starts on SQLite, existing IDs resolve, legacy metadata is read-only, and no
worker or client treats queue history as product ownership.

## 7. Phase 2 — media correctness and reproducibility

**Outcome:** Every edit uses a typed SourceTime → ClipTime → OutputTime mapping, and every
published output is a validated immutable artifact tied to one exact ClipRevision.

**Estimated effort:** 7-12 focused engineering days across seven ordered slices.

### Slice 2A — pure canonical timeline

**Files:**

- Create: `engine/clip/timeline.py`
- Test: `engine/tests/test_timeline.py`
- Create: `contracts/v1/timeline-map.schema.json`

- [ ] Define immutable `SourceSpan`, `ClipSpan`, `OutputSpan`, `TimelineSlice`, and
      versioned `TimelineMap` types using integer milliseconds and named JSON fields.
- [ ] Test normalization, clamp, sort, overlap/adjacency merge, head/middle/tail deletion,
      all-deleted error, monotonic mapping, inverse lookup at boundaries, and deterministic
      serialization.
- [ ] Make slice durations sum exactly to mapped clip duration; prohibit anonymous durable
      `start`/`end` floats.
- [ ] Prove normalization, mapping, and semantic serialization are linear in the number of
      slices/cues on checked-in size fixtures.
- [ ] Run `cd engine && .venv/bin/python -m pytest -q tests/test_timeline.py` twice and
      compare serialized fixture bytes.
- [ ] Commit as `feat(timeline): add canonical media time mapping`.

### Slice 2B — immutable ClipRevision and semantic manifest integration

**Files:**

- Create: `engine/clip/semantic_manifest.py`
- Create: `contracts/v1/semantic-manifest.schema.json`
- Modify: `engine/domain/clips.py`
- Modify: `engine/state/clip_repository.py`
- Modify: `engine/clip_runner.py`
- Test: `engine/tests/test_semantic_manifest.py`
- Test: `engine/tests/test_clip_revision_render.py`

- [ ] Persist the exact TimelineMap, source window, caption/reframe settings, parent
      revision, and creation actor in every ClipRevision.
- [ ] Canonicalize object keys, preserve editorial array order, normalize paths relative to
      Delivery root, and exclude IDs/timestamps/absolute paths/encoder metadata declared
      nondeterministic.
- [ ] Prove repeated manual and MCP plans produce canonically equal semantic manifests.
- [ ] Keep the old current Render selected until the replacement passes validation and is
      published.
- [ ] Commit as `feat(clips): persist immutable editorial revisions`.

### Slice 2C — ripple cut and atomic FFmpeg outputs

**Files:**

- Modify: `engine/clip/cutter.py`
- Modify: `engine/clip/_ffmpeg.py`
- Modify: `engine/clip_runner.py`
- Modify: `engine/attempt_staging.py`
- Test: `engine/tests/test_cutter.py`
- Test: `engine/tests/test_clip_runner.py`
- Create: `engine/tests/test_atomic_media_pipeline.py`

- [ ] Replace legacy `_kept_spans` calculations with one normalized TimelineMap.
- [ ] Apply attempt-local temporary output, ffprobe/decode validation, immutable path
      promotion, and Artifact publication to cut/reframe/caption/export stages.
- [ ] Test mid-GOP starts, head/middle/tail/all deletion, process cancellation during each
      stage, stale generation completion, invalid output, and full-disk/write failure.
- [ ] Assert cancellation leaves the old current Render unchanged and no final path or
      published Artifact for the failed attempt.
- [ ] Commit as `fix(media): cut and publish from the canonical timeline`.

### Slice 2D — transcript logical order and caption mapping

**Files:**

- Modify: `engine/transcript_io.py`
- Modify: `engine/clip/captioner.py`
- Modify: `engine/clip/backhalf/ass_captions.py`
- Modify: `engine/clip/exporter.py`
- Modify: `apps/studio/src/lib/caption-page.ts`
- Test: `engine/tests/test_transcript_io.py`
- Test: `engine/tests/test_captioner.py`
- Test: `engine/tests/test_exporter.py`
- Modify test: `apps/studio/test/caption-page.test.ts`

- [ ] Centralize deterministic word ordering, including inserted text anchored to source
      words without synthesizing spoken duration.
- [ ] Intersect words/cues with SourceTime once, map to ClipTime once, and reject cues beyond
      mapped duration or containing deleted words.
- [ ] Emit TXT, JSON, SRT, VTT, and ASS in the same logical command order.
- [ ] Prove caption alignment p95 is within 100 ms of the canonical mapped word timeline.
- [ ] Commit as `fix(captions): map transcript cues through TimelineMap`.

### Slice 2E — diarization and reframe mapping

**Files:**

- Modify: `engine/diarizer.py`
- Modify: `engine/clip/reframe.py`
- Modify: `engine/clip/face_track.py`
- Modify: `engine/clip/backhalf/pan_expr.py`
- Test: `engine/tests/test_diarizer_audio_alignment.py`
- Test: `engine/tests/test_reframe.py`
- Test: `engine/tests/test_face_track.py`

- [ ] Normalize overlapping/adjacent speaker turns in SourceTime and define deterministic
      tie-breaking for equal confidence or equal overlap.
- [ ] Map diarization and face-track decisions into ClipTime exactly once; make fallback pan
      consume the same mapped speaker timeline.
- [ ] Lock the regression where source second 186 in a 180-second window with a two-second
      earlier deletion becomes clip second 4 in both paths.
- [ ] Verify portrait and landscape geometry, requested aspect, crop bounds, and byte-stable
      canonical reframe-track JSON.
- [ ] Commit as `fix(reframe): share mapped speaker timeline`.

### Slice 2F — generated golden-media suite

**Files:**

- Create: `engine/tests/fixtures/golden_media.py`
- Create: `engine/tests/test_golden_media_pipeline.py`
- Modify: CI workflow selected by the Phase 2 child plan

- [ ] Generate a deterministic audiovisual fixture with colored regions, seeded tones,
      sparse keyframes, transcript words, and two speaker turns.
- [ ] Exercise cut, ripple, caption, diarization/reframe, export, mid-stage cancellation,
      decode, and semantic-manifest comparison using real FFmpeg/ffprobe.
- [ ] Fail—not skip—when FFmpeg or ffprobe is absent in CI.
- [ ] Record per-stage wall time on the same fixture; any regression above 20% requires an
      approved explanation in the child plan.
- [ ] Run the complete engine and golden suite twice consecutively.
- [ ] Commit as `test(media): gate releases on golden audiovisual fixtures`.

### Slice 2G — Phase 2 certification

- [ ] Prove all ten Phase 2 acceptance items, including duration tolerance, caption p95,
      mapped speaker regression, decode validation, cancellation, aspect, and two clean runs.
- [ ] Verify rollback selects the last published immutable ClipRevision/Render and leaves
      failed replacement artifacts unreachable.
- [ ] Run the repository floor, Phase 1 migration/restart matrix, and Phase 0 authenticated
      E2E to catch boundary regressions.
- [ ] Record exact media fixture digest, FFmpeg/ffprobe versions, test counts, performance
      ratios, and behavior commit.
- [ ] Commit evidence as `docs(phase2): record media correctness completion`.

**Phase 2 exit gate:** The same editorial plan produces the same typed TimelineMap, captions,
reframe track, stage parameters, and canonical semantic manifest. Published media probes and
decodes; cancelled or failed work cannot replace the current Render.

## 8. Phase 3 — one golden workflow

**Outcome:** A new user can import, inspect, approve, edit, preflight, render, restart, and
deliver an open bundle without a terminal or agent, on desktop and narrow screens.

**Estimated effort:** 7-10 focused engineering days across five slices.

### Slice 3A — real local/URL import and domain-backed Studio data layer

**Files:**

- Modify: `engine/routes/domain_v1.py`
- Modify: `engine/domain/library.py`
- Modify: `packages/api-client/src/domain.ts`
- Create: `apps/studio/src/lib/domain-client.ts`
- Create: `apps/studio/src/lib/domain-events.ts`
- Create: `apps/studio/src/components/import/local-file-import.tsx`
- Create: `apps/studio/src/components/import/url-import.tsx`
- Modify: `apps/studio/src/app/import/page.tsx`
- Test: `engine/tests/test_domain_imports.py`
- Create: `apps/studio/test/import-domain.test.tsx`

- [ ] Add multipart local-file import that copies bytes into an attempt-scoped Spool-managed
      path, validates them, and publishes the Source artifact; never retain arbitrary external
      browser paths.
- [ ] Keep URL provenance, validate before admission, and persist failed Sources as retryable
      domain records without published media.
- [ ] Add typed resource pagination and delta-event invalidation; do not poll full Library
      snapshots.
- [ ] Show real queued/progress/error/retry state and idempotency behavior for both imports.
- [ ] Commit as `feat(studio): import real sources through domain API`.

### Slice 3B — Source workspace and storyboard

**Files:**

- Create: `apps/studio/src/components/workspace/source-summary.tsx`
- Create: `apps/studio/src/components/workspace/transcript-panel.tsx`
- Create: `apps/studio/src/components/workspace/storyboard.tsx`
- Create: `apps/studio/src/components/workspace/candidate-card.tsx`
- Create: `apps/studio/src/components/workspace/revision-history.tsx`
- Create: `apps/studio/src/components/workspace/render-history.tsx`
- Modify: `apps/studio/src/app/sources/[id]/page.tsx`
- Modify: `apps/studio/src/app/sources/[id]/discovery/page.tsx`
- Modify: `apps/studio/src/app/library/page.tsx`
- Modify: `apps/studio/src/components/spool/context.tsx`
- Create: `apps/studio/test/source-workspace.test.tsx`

- [ ] Replace every Source/Transcript/Candidate/Clip/Render projection from jobs with domain
      resource queries and event invalidation.
- [ ] Show Candidate title, rationale, named score factors, boundaries, speakers, crop and
      caption previews, and persisted proposed/approved/rejected state.
- [ ] Expose retry/recovery for failed, cancelled, interrupted, and timed-out prerequisites;
      never advance the chain after a non-success terminal state.
- [ ] Preserve loading, empty, error, stale-event recovery, pagination, and keyboard behavior
      in component tests.
- [ ] Commit as `feat(studio): make Source the domain workspace`.

### Slice 3C — editor restoration and structured preflight

**Files:**

- Create: `engine/clip/preflight.py`
- Modify: `engine/routes/domain_v1.py`
- Modify: `apps/studio/src/app/clips/[id]/page.tsx`
- Modify: `apps/studio/src/app/clips/[id]/reframe/page.tsx`
- Modify: `apps/studio/src/app/clips/[id]/caption/page.tsx`
- Modify: `apps/studio/src/components/spool/timeline.tsx`
- Create: `apps/studio/src/components/workspace/preflight-panel.tsx`
- Modify: `apps/studio/src/lib/use-clip-seeded-state.ts`
- Test: `engine/tests/test_preflight.py`
- Create: `apps/studio/test/editor-restoration.test.tsx`

- [ ] Restore aspect, reframe mode, crop boxes, caption style, TimelineMap, and current
      revision from the selected ClipRevision whenever the editor opens.
- [ ] Save edits as immutable revisions; Undo selects or creates a real revision rather than
      mutating transient component state.
- [ ] Return structured preflight findings for boundary quality, caption overflow/timing,
      crop confidence, silence, black frames, disk reserve, aspect, duration, and estimated size.
- [ ] Block render only on declared errors; show warnings and evidence without hiding them.
- [ ] Commit as `feat(editor): restore revisions and gate renders with preflight`.

### Slice 3D — open Delivery bundle

**Files:**

- Create: `contracts/v1/spool-project.schema.json`
- Modify: `engine/domain/deliveries.py`
- Modify: `engine/clip/exporter.py`
- Modify: `engine/routes/domain_v1.py`
- Create: `apps/studio/src/components/workspace/delivery-panel.tsx`
- Modify: `apps/studio/src/app/clips/page.tsx`
- Test: `engine/tests/test_delivery_bundle.py`
- Create: `apps/studio/test/delivery-panel.test.tsx`

- [ ] Build a versioned bundle with MP4, SRT, VTT, ASS, transcript JSON, thumbnail,
      suggested title/copy, and `spool-project.json`.
- [ ] Include source provenance, IDs and schema versions, TimelineMap, exact settings,
      provider-receipt IDs/digests, Artifact checksums, and manifest version; exclude exact
      remote request content by default.
- [ ] Validate every member and checksum before Delivery creation; reject missing roles,
      unpublished Artifacts, absolute paths, and checksum mismatch.
- [ ] Verify a user can understand and validate the bundle without Spool installed.
- [ ] Commit as `feat(delivery): export verifiable open project bundles`.

### Slice 3E — responsive/accessibility E2E and Phase 3 certification

**Files:**

- Modify: `apps/studio/src/components/spool/shell.tsx`
- Modify: `apps/studio/src/app/spool.css`
- Modify: `packages/ui/src/ui.tsx`
- Create: `apps/studio/e2e/golden-workflow.spec.ts`
- Create: `apps/studio/e2e/golden-workflow-a11y.spec.ts`
- Create: `docs/qa/golden-workflow-keyboard-checklist.md`
- Modify: `apps/studio/package.json` and `pnpm-lock.yaml` for axe-core only

- [ ] Implement desktop rail/workspace/optional agent, tablet collapsible navigation and
      agent drawer, and phone import/review/approval/queue/delivery without horizontal clipping.
- [ ] Use semantic buttons, switches, tabs, dialogs, labels, managed focus, visible focus,
      44px targets, AA contrast, reduced motion, and live progress announcements.
- [ ] Run the golden journey at 390, 768, and 1440 CSS pixels; capture release screenshots
      and record route bundle size and interaction latency.
- [ ] Restart after import, transcript, discovery approval, edit, preflight, render, and
      Delivery; assert the same IDs, current revisions, and settings return.
- [ ] Run Playwright plus axe-core with zero serious/critical violations and complete the
      checked-in manual keyboard/focus checklist.
- [ ] Prove one local Source and one URL Source each reach a verified Delivery without agent
      assistance; prove invalid inputs and failed stages never appear successful.
- [ ] From one Source, approve and render three Clips, restarting at every major boundary;
      assert each Clip returns with the same current revision, settings, and Render history.
- [ ] Record exact E2E duration, screenshots, bundle digest, accessibility result, route
      metrics, test counts, and behavior commit.
- [ ] Commit code as `feat(studio): ship the golden private clip workflow`, then evidence as
      `docs(phase3): record golden workflow completion`.

**Phase 3 exit gate:** All nine master acceptance items pass in a production build. The new
workspace flag can default on, while the legacy adapter remains available only for the
compatibility window.

## 9. Phase 4 — private intelligence and safe agent

**Outcome:** Local reasoning works with outbound networking blocked; every remote egress is
explicit and auditable; every mutation is frozen, approved, verified, and revision-reversible.

**Estimated effort:** 10-15 focused engineering days across four slices.

### Slice 4A — provider interface, Ollama, and EgressReceipts

**Files:**

- Create: `engine/reasoning/__init__.py`
- Create: `engine/reasoning/base.py`
- Create: `engine/reasoning/ollama.py`
- Create: `engine/reasoning/registry.py`
- Modify: `engine/state/egress_repository.py`
- Modify: `engine/clip/llm.py`
- Modify: `engine/settings.py`
- Modify: `engine/network_policy.py`
- Modify: `engine/routes/domain_v1.py`
- Test: `engine/tests/test_reasoning_provider.py`
- Test: `engine/tests/test_ollama_provider.py`
- Test: `engine/tests/test_egress_receipts.py`

- [ ] Define provider name, locality, health, model list, structured request, structured
      response, cancellation, and declared egress categories behind one interface.
- [ ] Support Ollama on an explicitly configured loopback origin; reject non-loopback Ollama
      configuration under local-provider mode.
- [ ] Keep provider `none` as default. Require provider-specific consent before any remote
      request and show exact locally inspectable request data before approval.
- [ ] Persist provider, purpose, categories, payload digest, approved time, response status,
      related entities, and encrypted-or-managed exact request Artifact for each remote action.
- [ ] Prove Offline blocks remote egress before lease/client/socket work while local Ollama
      and the rest of the local workflow remain usable.
- [ ] Commit as `feat(reasoning): add local Ollama and auditable egress`.

### Slice 4B — frozen agent proposal and execution state machine

**Files:**

- Create: `engine/agent_changes.py`
- Modify: `engine/state/models.py`
- Add a checksummed agent-change schema migration under `engine/state/schema/`
- Modify: `engine/routes/domain_v1.py`
- Modify: `engine/clip/agent.py`
- Modify: `engine/clip/agent_tools.py`
- Modify: `engine/mcp_server.py`
- Test: `engine/tests/test_agent_changes.py`
- Test: `engine/tests/test_agent_mcp_parity.py`

- [ ] Persist `request → frozen proposal → visible diff → approved → executing → verified`
      with terminal cancelled/failed/reverted outcomes and immutable execution IDs.
- [ ] Hash the canonical proposal and arguments; approval binds to that hash. Execution must
      reject any re-plan or argument drift.
- [ ] Allow read-only inspection immediately. Require approval for transcript/boundary/
      reframe/caption changes, remote egress, expensive renders, and destruction.
- [ ] Execute only domain services; prohibit direct file writes or current-pointer mutation
      from agent/MCP code.
- [ ] Cancel not-yet-started steps, let active owned processes unwind safely, and record an
      honest partial result.
- [ ] Prove manual and agent execution of the same frozen plan have canonically equal
      ClipRevision and Render manifests.
- [ ] Commit as `feat(agent): execute only approved frozen changes`.

### Slice 4C — Studio approval/diff/undo and EgressReceipt inspection

**Files:**

- Modify: `apps/studio/src/components/spool/agent.tsx`
- Create: `apps/studio/src/components/agent/change-proposal.tsx`
- Create: `apps/studio/src/components/agent/change-diff.tsx`
- Create: `apps/studio/src/components/agent/execution-progress.tsx`
- Create: `apps/studio/src/components/agent/egress-receipt.tsx`
- Modify: `packages/api-client/src/domain.ts`
- Create: `apps/studio/test/agent-approval.test.tsx`
- Create: `apps/studio/e2e/agent-approval.spec.ts`

- [ ] Show frozen actions, arguments, affected revisions, expected cost/egress, and semantic
      diff before approval.
- [ ] Make approval, cancellation, verification, failure, partial execution, and revert
      states survive refresh/restart.
- [ ] Implement Undo by selecting or creating a compensating TranscriptRevision or
      ClipRevision; label external side effects as non-reversible.
- [ ] Make exact request bytes locally viewable while retained and support the documented
      `purge_pending` → unlink → redacted receipt lifecycle.
- [ ] Commit as `feat(studio): add inspectable agent approval and undo`.

### Slice 4D — deterministic TasteProfiles and Phase 4 certification

**Files:**

- Create: `engine/taste_profiles.py`
- Add a checksummed TasteProfile schema migration under `engine/state/schema/`
- Modify: `engine/clip/moments.py`
- Modify: `engine/routes/domain_v1.py`
- Create: `apps/studio/src/components/agent/taste-profile.tsx`
- Test: `engine/tests/test_taste_profiles.py`
- Create: `apps/studio/test/taste-profile.test.tsx`
- Create: `apps/studio/e2e/offline-ollama-workflow.spec.ts`

- [ ] Derive inspectable factors only from explicit approvals/rejections, boundary changes,
      crop changes, and caption choices; use deterministic input ordering and calculations.
- [ ] Provide edit, export, reset, disable, and delete. Disabled/deleted profiles immediately
      restore explicit defaults.
- [ ] Block non-loopback sockets during the local-file golden workflow with Ollama and prove
      zero observed non-loopback connections.
- [ ] Run proposal approval/argument-tamper/cancel/restart/undo/receipt-purge/TasteProfile
      matrices and all Phase 0-3 gates.
- [ ] Record socket evidence, receipt digests, semantic-manifest equality, test counts, and
      behavior commit.
- [ ] Commit code as `feat(taste): learn inspectable local preferences`, then evidence as
      `docs(phase4): record private intelligence completion`.

**Phase 4 exit gate:** All nine master acceptance items pass. Remote egress remains off by
default, Ollama is optional, and disabling the agent leaves the complete Phase 3 manual
workflow intact.

## 10. Phase 5 — automation, connectors, and real analytics

**Entry gate:** Phase 3 passes in a tagged release and has been dogfooded on real projects.
Phase 4 may be finishing in parallel, but any Phase 5 agent behavior must obey its approval
and egress contracts.

**Estimate:** Intentionally withheld until a connector is selected and its current platform
access, review rules, token lifecycle, rate limits, media constraints, and API terms are
verified in a child spec.

### Slice 5A — Recipes as frozen domain-operation plans

**Files:**

- Refactor: `engine/recipes.py`
- Modify: `engine/routes/domain_v1.py`
- Modify: `engine/mcp_server.py`
- Modify: `apps/studio/src/app/recipes/page.tsx`
- Test: `engine/tests/test_recipes.py`
- Create: `apps/studio/test/recipes-domain.test.tsx`

- [ ] Version Recipe schemas and persist canonical domain operations, inputs, required
      approvals, idempotency scope, and compatibility version.
- [ ] Execute through the same domain services as manual and MCP actions; prohibit direct
      file/current-pointer writes.
- [ ] Prove manual and MCP runs create the same frozen plan and semantic manifest.
- [ ] Commit as `feat(recipes): save versioned domain operation plans`.

### Slice 5B — Watches as idempotent triggers

**Files:**

- Refactor: `engine/watches.py`
- Refactor: `engine/watcher.py`
- Modify: `engine/routes/domain_v1.py`
- Modify: `apps/studio/src/app/watches/page.tsx`
- Test: `engine/tests/test_watches.py`
- Test: `engine/tests/test_watch_capacity_atomicity.py`
- Create: `engine/tests/test_watch_idempotency.py`

- [ ] Make a Watch select a Recipe plus trigger/cursor configuration; it cannot contain a
      separate mutation implementation.
- [ ] Derive idempotency from trigger identity plus Recipe version and preserve reviewable
      Source/Candidate/Clip records even after trigger retries.
- [ ] Apply NetworkPolicy, bounded admission, Phase 4 approval, and EgressReceipt checks at
      the same domain boundaries as manual execution.
- [ ] Prove duplicate and restart-replayed triggers create one logical plan.
- [ ] Commit as `feat(watches): trigger idempotent domain recipes`.

### Slice 5C — select and specify the first publishing connector

No connector code starts in this slice. Produce one approved child spec that records:

- the selected platform/integration and why it is the smallest valuable first connector;
- current official API access and review requirements;
- credential storage in the OS keychain or an equivalently reviewed secret store;
- token refresh/revocation, scopes, rate limits, retries, idempotency, upload/resume limits,
  content status polling, and webhook verification;
- the exact Delivery artifact/metadata mapping;
- failure and rollback semantics that never damage Delivery integrity or report a false
  publish;
- provenance fields required for later analytics.

- [ ] Approve that child spec before naming provider-specific source files.
- [ ] Commit it as `docs(connectors): specify first publishing integration`.

### Slice 5D — connector interface and first implementation

**Files fixed before provider selection:**

- Create: `engine/publishing/__init__.py`
- Create: `engine/publishing/base.py`
- Create: `engine/publishing/credentials.py`
- Create: `engine/publishing/service.py`
- Add a checksummed publishing-attempt schema migration under `engine/state/schema/`
- Modify: `engine/routes/domain_v1.py`
- Modify: `apps/studio/src/app/publish/page.tsx`
- Test: `engine/tests/test_publishing_service.py`

Provider-specific paths are named by the approved Slice 5C child plan.

- [ ] Define connector capabilities, validated Delivery input, credential reference,
      idempotent submission, progress/status, retry classification, cancellation, and external
      content identity.
- [ ] Store secrets outside SQLite/media/manifests; persist only the reviewed credential
      reference and non-secret provenance.
- [ ] Require a verified Delivery and, where agent-triggered, an approved frozen proposal.
- [ ] Prove timeout/retry/duplicate/webhook/revocation/platform-rejection paths never mark a
      publish successful without authoritative platform evidence.
- [ ] Commit interface and provider implementation separately.

### Slice 5E — provenance-backed analytics and optional taste feedback

**Files:**

- Create: `engine/analytics.py`
- Add a checksummed analytics schema migration under `engine/state/schema/`
- Modify: `engine/routes/domain_v1.py`
- Modify: `apps/studio/src/app/analytics/page.tsx`
- Modify: `engine/taste_profiles.py`
- Test: `engine/tests/test_analytics.py`
- Create: `apps/studio/test/analytics-provenance.test.tsx`

- [ ] Store only genuine connector observations with platform, account, content ID,
      collection time, source timestamp, freshness, and raw-response digest.
- [ ] Display unavailable/stale/partial/error states; never synthesize totals or trends.
- [ ] Feed outcomes into TasteProfiles only behind an explicit switch and as named,
      inspectable factors that can be removed and recomputed.
- [ ] Prove disabling outcome feedback restores the pre-analytics deterministic profile.
- [ ] Commit as `feat(analytics): show connector data with provenance`.

### Phase 5 child gate

Every Recipe, Watch, MCP trigger, and connector child spec must prove:

1. Equivalent entry points produce the same frozen plan and domain manifest.
2. Duplicate triggers and submissions are idempotent.
3. Credentials never enter client bundles, SQLite payloads, logs, or Delivery manifests.
4. Connector failure cannot damage a Delivery or report a successful publish.
5. Each analytics value carries platform/account/content/time/freshness provenance.

## 11. Verification floor for every behavioral slice

Run focused tests first, then the relevant repository floor from the worktree root:

```bash
(cd engine && .venv/bin/python -m pytest -q)
pnpm typecheck
pnpm lint
pnpm test
pnpm build
pnpm format:check
git diff --check
```

Interpretation rules:

- A child plan records the fresh baseline before edits. It may compare a known repository-wide
  formatting baseline, but changed eligible files must pass their focused formatter/linter.
- FFmpeg/ffprobe-dependent gates fail if the required binaries are missing; they do not skip
  silently.
- A clean process exit is not enough. Acceptance scripts verify domain outcomes, Artifact
  checksums, current pointers, process trees, and ports.
- E2E uses isolated state and captured processes. It never reads or mutates the user's live
  library.
- Each release records `api_version`, `contract_version`, `storage_schema_version`, app
  commit, provider configuration, and Delivery manifest version.

## 12. Program verification matrix

| Risk                                  | Earliest owning phase      | Permanent regression evidence                                        |
| ------------------------------------- | -------------------------- | -------------------------------------------------------------------- |
| Queue cleanup deletes product data    | 0, reinforced in 1         | History-clear byte/count/restart matrix                              |
| Cancelled/stale attempt publishes     | 0, persisted in 1          | Barrier races + generation CAS + kill/restart matrix                 |
| SQLite migration loses/mis-links work | 1                          | Dry-run parity, double import, injected rollback, stable alias tests |
| Partial/invalid file appears current  | 1, applied everywhere in 2 | Crash-window reconciler + ffprobe/decode + current-pointer checks    |
| Ripple/captions/reframe disagree      | 2                          | Typed timeline unit tests + generated golden media                   |
| UI fabricates or loses state          | 0, replaced in 3           | Domain-backed components + restart-at-each-stage E2E                 |
| Delivery is opaque or incomplete      | 3                          | JSON Schema, role/checksum validation, outside-Spool inspection      |
| Privacy claim hides egress            | 0, completed in 4          | NetworkPolicy socket test + consent + visible EgressReceipt          |
| Agent changes after approval          | 4                          | Proposal hash/argument-tamper tests + immutable execution ID         |
| Undo is cosmetic                      | 4                          | Restart-safe revision-selection/compensation test                    |
| Automation bypasses manual rules      | 5                          | Manual/Recipe/Watch/MCP semantic-manifest equality                   |
| Connector/analytics lies              | 5                          | Idempotent publish state + authoritative provenance/freshness tests  |

## 13. Release and rollback checkpoints

### After Phase 1

- Runtime backend is SQLite; legacy metadata remains untouched and read-only.
- Roll forward against SQLite after it accepts writes. Never fall back to stale JSON.
- The Phase 0 safety branch remains a known recovery point for pre-migration user data.

### After Phase 2

- Immutable revisions allow the current pointer to return to the last validated Render.
- Old artifacts remain reachable until the grace period passes; failed replacements never
  become current.

### After Phase 3

- Enable the domain workspace only after migration, restart, golden-media, responsive, and
  accessibility gates pass.
- Keep `/api/v1` compatibility for the first tagged Phase 3 release plus at least 90 days.

### After Phase 4

- Local and remote providers have separate flags; remote stays off by default.
- Agent and TasteProfile UI can be disabled without affecting the manual workflow.
- EgressReceipt audit records survive provider rollback; exact request bytes follow explicit
  purge semantics.

### During Phase 5

- Each Recipe, Watch, connector, and analytics surface remains independently hidden until
  its child gate passes.
- Connector rollback cannot delete Deliveries, local artifacts, or publish provenance.

## 14. Explicit non-goals through Phase 5

- No Flask/Next.js rewrite, distributed queue, render farm, native app, cloud organization,
  billing, SSO, or multi-user authorization.
- No full NLE, multi-track audio workstation, automatic B-roll, emoji generator, or opaque
  virality score.
- No direct publishing before the connector child spec and Phase 3 gate.
- No analytics without genuine connector provenance.
- No remote training or hidden transcript upload.
- No broad content-addressing or relocation of existing media during migration.
- No bit-for-bit MP4 equality requirement across hardware encoders; compare semantic plans
  and validated media properties.
- No removal of Recipes, Watches, Brand Kits, settings, or historical metadata just because
  a route is hidden.

## 15. Master completion checklist

- [x] Phase 0 removes immediate data-loss, security, privacy-truth, contract, and agent-write
      hazards and is verified at `8060b88`.
- [ ] Phase 1 persists domain identity, revisions, attempts, and Artifacts in validated
      SQLite state while preserving legacy IDs and bytes.
- [ ] Phase 2 proves one canonical timeline across cut, captions, diarization, reframe, and
      export with real generated media.
- [ ] Phase 3 ships the manual local-file/URL-to-Delivery workflow with restart, responsive,
      keyboard, accessibility, and open-bundle evidence.
- [ ] Phase 4 ships local Ollama, explicit remote egress, inspectable receipts, frozen agent
      proposals, immutable execution IDs, revision undo, and reversible TasteProfiles.
- [ ] Phase 5 reintroduces Recipes and Watches through domain plans, then ships only approved
      connectors and provenance-backed analytics.
- [ ] Every phase passes its child acceptance matrix in CI and on a clean local install.
- [ ] Queue history can be erased without changing Library entities or published bytes.
- [ ] Migration preserves every explainable record and stable ID.
- [ ] Process termination at every major stage produces a legal restart state and no phantom
      entity.
- [ ] Identical inputs produce identical TimelineMaps, captions, settings, and semantic
      manifests.
- [ ] Every published Artifact validates; every Delivery is verifiable outside Spool.
- [ ] Offline local-provider mode produces zero non-loopback connections.
- [ ] Every remote operation has informed consent and an inspectable EgressReceipt.
- [ ] Every agent mutation is approved, auditable, canonically equivalent to manual work,
      and revision-reversible.

When the last unchecked item through Phase 4 passes, the Private Clip Foundry recovery
program is complete. Phase 5 then proceeds capability by capability under its own child
gates; it is not a reason to delay a truthful, durable, editor-grade Phase 4 release.
