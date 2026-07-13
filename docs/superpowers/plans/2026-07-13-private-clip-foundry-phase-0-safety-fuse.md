# Private Clip Foundry Phase 0 Safety Fuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the current alpha's data-loss paths and false product promises, enforce a minimum local security boundary, and make the existing Studio/engine/CLI/MCP contract safe enough to build the durable domain core on top of.

**Architecture:** Keep the current Flask, JSON stores, job managers, Next Studio, and `/api/v1` compatibility surface intact for Phase 0. Add narrow safety fuses around them: persisted history visibility instead of deletion, in-memory attempt identities around legacy workers, admission control before executors, explicit remote-reasoning consent, defense-in-depth agent mutation rejection, and a same-origin Studio proxy for authenticated JSON/SSE/media traffic. Phase 1 replaces these temporary legacy protections with SQLite domain identity and persisted attempt generations.

**Tech Stack:** Python 3.12 · Flask · pytest · TypeScript 5 · Next.js 16 / React 19 · Vitest / Testing Library · Playwright · pnpm 10 / Turborepo.

---

## 1. Authority, scope, and execution rules

This is the required Safety Fuse child plan for the approved master spec:

- `docs/superpowers/specs/2026-07-13-private-clip-foundry-roadmap-design.md`
- Phase 0 scope and acceptance gate in Section 7
- invariants 1, 6, 7, 8, 9, 10, 11, and 12 in Section 5
- security/privacy contract in Section 6.6

The master spec remains authoritative. If this plan and the master spec disagree, stop and repair this plan before touching production code.

Execution rules:

1. Work on `codex/phase-0-safety-fuse`, never directly on `main`.
2. Use an isolated worktree created with `superpowers:using-git-worktrees` after user approval.
3. Follow red-green-refactor. No production behavior change lands before its failing test is observed.
4. Execute slices in order. A later slice may not weaken an earlier safety gate.
5. Keep each commit limited to the files named by its task. Preserve the existing Recipes, Watches, brand kits, routes, and user data even when their UI entry points are hidden.
6. Do not touch the pre-existing untracked paths `.claude/`, `docs/CODE_REVIEW.md`, or `docs/superpowers/plans/2026-06-12-code-review-high-medium-fixes.md`.
7. Existing `/api/v1/events` remains a full-snapshot stream in Phase 0. Add fields only; do not remove or rename existing fields.
8. Manual REST and CLI mutations remain available. Only agent-originated mutation surfaces are fused off.
9. The PostCSS/dependency change is its own final commit and contains no behavior edits.

## 2. Locked implementation decisions

### 2.1 History-only dismissal

Legacy download jobs currently stand in for Sources, and legacy clip jobs stand in for Clips/Renders. Popping their records would therefore destroy product identity even if file unlinking were removed.

Phase 0 will add a persisted nullable `dismissed_at` timestamp to all three job records. Dismiss and TTL sweep set this marker; they never pop the record and never unlink a managed artifact. Queue projections omit dismissed attempts, while Library/source/clip projections continue to see them. Direct `GET` by ID and `/jobs` history continue to return them with additive `dismissed` and `dismissed_at` fields.

This is a temporary compatibility fuse. Phase 1 moves Source/Clip identity out of job records and turns `/jobs` into explicit attempt history.

### 2.2 Attempt identity

Each submitted or resumed execution captures an in-memory monotonically increasing attempt number on the job object. Cancel and pause invalidate the active attempt. Start, progress, result publication, exception handling, completion, and nested child submission must prove all three conditions before mutating current state:

```python
current_entry is captured_job and captured_attempt == captured_job._attempt and captured_job.status is expected_status
```

Validation and mutation occur inside one manager-lock critical section; a boolean precheck followed by an unlocked mutation is forbidden. Candidate files remain in attempt staging and promotion occurs inside the guarded completion transition. The token is deliberately not persisted in Phase 0. Phase 1 replaces it with durable generation/compare-and-set semantics.

### 2.3 Queue capacity semantics

`pending_capacity` limits admitted executor wrappers that have not drained, both queued and running. Normal queued cancellation invalidates the attempt but does not call `Future.cancel()`; the stale wrapper eventually enters, fails its attempt gate, and releases its reservation in `finally`. Submit/cancel churn therefore cannot grow the executor's private queue. The default is:

```python
min(32, max(4, 4 * max_workers))
```

Admission is reserved under the manager lock before a job record is inserted and before `executor.submit()` is called. Overflow raises `QueueFullError`, creates no record, touches no executor, and maps to HTTP `429` with body `{ "error": "queue_full", "retry_after": 1 }` and header `Retry-After: 1`. Each reservation is an idempotent one-shot lease. The submitted wrapper releases it in `finally`; an `executor.submit()` exception releases it while rolling back the new record and persisted snapshot. The only other early release is rollback of an atomic multi-submit batch when `Future.cancel()` succeeds before a provisional wrapper starts.

`shutdown(wait=False)` preserves the managers' current nonblocking contract: it rejects new admissions and lets already-admitted wrappers drain, so its counter may remain nonzero temporarily. It never uses `cancel_futures=True`. `shutdown(wait=True)` waits for every wrapper and guarantees a zero counter before returning.

### 2.4 Browser Origin and proxy trust

- A present Origin on a state-changing `/api/*` request must be a loopback Studio origin.
- `Origin: null` and hostile origins receive structured `403 origin_forbidden` before the view executes, even when the bearer token is valid.
- A missing Origin remains allowed so authenticated CLI/MCP calls work.
- The token-bearing Next proxy is stricter: its own request URL must use a loopback host, every present Origin on every method must be loopback and exactly equal that Next request origin, state-changing requests require it, and only `GET`/`HEAD` may omit it. The validated browser Origin is never forwarded with the bearer, and CORS response headers are never relayed downstream.
- `X-Forwarded-For` is ignored by default. `TROVE_TRUST_PROXY_HOPS=N` enables exactly `N` trusted right-most proxy hops; malformed or undersized chains fall back to `request.remote_addr`.
- Rate-limiter keys are capped at 4096 by default (`TROVE_RATE_LIMIT_MAX_KEYS`). Expired keys are pruned first; if a new key still arrives at capacity, evict the least-recently-seen key (lexical key order breaks timestamp ties) before inserting it.

### 2.5 Remote reasoning consent

Persist two settings:

```json
{
  "reasoning_provider": "none",
  "reasoning_egress_consent": false
}
```

Phase 0 supports `none` and `codex`. `none` is the default. Changing provider resets consent unless the same validated update explicitly supplies consent for the new provider. Every egress provider call checks the persisted/env-applied consent immediately before network execution. `offline` remains the stronger gate and always blocks remote reasoning.

`reasoning_egress_consent: true` is invalid while the effective provider is `none`; choosing `none` always stores consent as false. A consent-only patch is valid only when the already-persisted provider is `codex`.

Manual discovery maps a missing provider to `409 { "error": "reasoning_provider_required" }`, missing consent to `409 { "error": "egress_consent_required" }`, and active offline mode to the existing structured offline response. No blocked case creates a ClipJob or invokes the provider.

One engine-wide `NetworkPolicy` makes `Offline` literal: no new URL download, remote model install, remote watch listing, or remote reasoning lease can begin. Switching Offline on is atomic and fails with `network_work_active` while an existing non-loopback lease is active; queued work rechecks before its first socket. Loopback API clients and local folder/transcription/render work remain allowed.

The three UI labels are exactly `Fully local`, `Remote reasoning enabled`, and `Offline`.

### 2.6 Authenticated Studio transport

The Studio uses a same-origin Next route at `/api/engine/[...path]`. The proxy reads server-only `SPOOL_ENGINE_URL` and optional `SPOOL_ENGINE_TOKEN`, adds the bearer header when the token is configured, streams request/response bodies (including SSE and media), and isolates request/response headers. The Next request URL itself must be loopback. Every present browser Origin, including on reads, must also be loopback and exactly match that request origin; mutations require it. Originless `GET`/`HEAD` remains available for DOM media, while the proxy strips upstream Origin and downstream CORS headers. Originless CLI/MCP never uses this token-bearing proxy and continues to call Flask directly. The URL defaults to the loopback engine so unauthenticated loopback development remains available; the token-authenticated acceptance run supplies both the Flask token and the matching server-only Studio token.

The browser-facing `SpoolApiClient` points at `/api/engine`; its existing synchronous media URL helpers therefore remain usable by `<video>` and `<a>` without exposing a token. Direct CLI/MCP clients continue to call Flask.

### 2.7 Canonical Phase 0 wire shapes

- Transcript word edit request: `{ "op": "set_text", "w": "replacement" }`. Flask temporarily accepts legacy `text` and emits `Warning: 299 Spool "text is deprecated; use w"`.
- Bulk submit response: `{ "submitted": number, "failed": number, "results": BulkSubmitResult[] }` with HTTP 201 or 207.
- Agent mutation rejection: `/api/v1/agent` returns HTTP 409 and MCP returns a structured tool error, both with `{ "error": "agent_mutation_disabled", "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships." }` and zero underlying calls.

## 3. Verified baseline and known pre-existing failure

Fresh baseline on 2026-07-13:

```text
(cd engine && .venv/bin/python -m pytest -q)
860 passed, 4 dependency deprecation warnings

pnpm typecheck
9/9 Turbo tasks passed

pnpm test
7 files, 48 Vitest tests passed

pnpm build
Next production build passed; 15 routes generated

pnpm lint
PRE-EXISTING FAIL: Prettier reports packages/ui/src/ui.tsx and
packages/api-client/src/index.ts. Do not claim repo-wide lint green until
the touched API client file is formatted and the unrelated UI baseline is
either separately fixed or explicitly reported.

pnpm format:check
PRE-EXISTING FAIL: the repo-wide Prettier check reports 51 paths across
Studio source/tests, generated Playwright test-results artifacts, docs (including the
protected untracked review/plan files), packages/api-client/src/index.ts,
packages/ui/src/ui.tsx, and README.md. Do not reformat those unrelated paths
as part of Phase 0. Freeze the clean-worktree failure list; require targeted
Prettier checks for changed eligible files that were clean at baseline and
`git diff --check` for every hunk, while reporting the repo-wide baseline
without claiming the gate passed.
```

Always run engine pytest from `engine/`; the CLI tests intentionally depend on that working directory.

## 4. Target file map

### New files

- `engine/job_capacity.py` — shared capacity formula and `QueueFullError` only.
- `engine/attempt_staging.py` — legacy attempt-local output roots and guarded same-filesystem promotions.
- `engine/network_policy.py` — atomic Offline/non-loopback lease boundary.
- `engine/tests/test_phase0_artifact_safety.py` — cross-manager managed-artifact byte-identity and restart matrix.
- `engine/tests/test_attempt_staging.py`, `engine/tests/test_network_policy.py` — cancellation publication and literal Offline regressions.
- `contracts/v1/phase0-contract.json` — canonical word-edit, bulk, queue-full, and agent-disabled fixtures consumed by Python and TypeScript tests.
- `apps/studio/src/lib/engine-proxy.ts` — testable same-origin forwarding policy.
- `apps/studio/src/lib/action-error.ts` — one structured API-error-to-actionable-copy helper for visible Studio mutations.
- `apps/studio/src/app/api/engine/[...path]/route.ts` — thin Next route-handler adapter.
- `apps/studio/test/product-truth.test.tsx` — visible-control/product-copy regressions.
- `apps/studio/test/privacy-mode.test.tsx` — provider/consent/status-label regressions.
- `apps/studio/test/engine-proxy.test.ts` — JSON, SSE, media, token, and Origin proxy tests.

### Existing engine files

- `engine/jobs.py`, `engine/jobs_store.py` — history marker, non-destructive sweep/dismiss/cancel, attempt and capacity guards.
- `engine/transcribe_jobs.py`, `engine/clip_jobs.py` — same lifecycle, capacity, and attempt rules.
- `engine/app.py`, `engine/clip_runner.py` — initialize limits/settings; guard transcript publication and produce child fan-out.
- `engine/safety.py`, `engine/config.py` — mapped IPv6 normalization, browser Origin gate, proxy-derived client IP, bounded limiter configuration.
- `engine/settings.py`, `engine/clip/llm.py` — provider selection and egress consent enforcement.
- `engine/routes/api_v1.py` — additive lifecycle fields, structured errors, settings contract, word/bulk compatibility.
- `engine/clip/agent.py`, `engine/clip/agent_tools.py`, `engine/mcp_server.py` — mutation fuse with read-only inspection preserved.
- `engine/trove_client.py`, `engine/cli.py` — canonical word payload and shared contract fixtures.
- Focused tests under `engine/tests/` named in each task.

### Existing Studio/workspace files

- `apps/studio/src/lib/engine.ts`, `apps/studio/.env.example` — same-origin proxy configuration.
- `packages/types/src/index.ts`, `packages/api-client/src/index.ts` — additive lifecycle/settings/bulk/error types.
- `apps/studio/src/components/spool/context.tsx` — omit dismissed attempts, remove fabricated projections, read-only agent behavior, privacy mode.
- `apps/studio/src/components/spool/shell.tsx`, `overlays.tsx`, `agent.tsx`, `panels.tsx` — honest navigation/actions/copy.
- Studio Import, Queue, Clips, Analytics, Settings, Onboarding, Home, Source, and Editor pages named below.
- `apps/studio/e2e/url-to-clip.spec.ts`, `apps/studio/playwright.config.ts` — authenticated golden-flow proof and sufficient real-pipeline timeout.
- Remove `packages/mcp-client/` and its references because it is an unused stub, not a working MCP client.
- `README.md`, `docs/Spool_Engineering-Spec.md`, `docs/PROGRESS.md` — replace stale TypeScript MCP-client claims with the retained Python MCP server.
- `package.json`, `pnpm-lock.yaml` — isolated dependency override/update only in the final task.

## 5. Slice and commit order

```text
0A persisted history + artifact safety
 -> 0B attempt/cancellation guards
 -> 0C product truth + privacy + agent fuse
 -> 0D security boundary + bounded admission
 -> 0E authenticated Studio proxy + token E2E
 -> 0F shared contracts + TS MCP stub removal
 -> 0G isolated dependency remediation
```

Each slice must pass its focused tests and the full relevant side before the next slice begins.

## 6. Task 0 — Create the isolated worktree and record the baseline

**Files:**

- Add: `docs/superpowers/plans/2026-07-13-private-clip-foundry-phase-0-safety-fuse.md`
- Do not modify production files in this task.

- [ ] **Step 1: Put this approved plan on the feature branch**

From the current checkout, create the branch, commit only this approved plan, and return the checkout to `main`. The unrelated untracked files named in Section 1 must remain untouched:

```bash
git switch -c codex/phase-0-safety-fuse
git add docs/superpowers/plans/2026-07-13-private-clip-foundry-phase-0-safety-fuse.md
git diff --cached --check
git commit -m "docs(plan): define Phase 0 safety fuse"
git switch main
```

- [ ] **Step 2: Create the approved global worktree**

Use this path so the repository needs no `.gitignore` setup change. It becomes the explicit worktree preference when the user approves this plan:

```bash
mkdir -p /Users/kaivan108icloud.com/.config/superpowers/worktrees/spool-editor
git worktree add \
  /Users/kaivan108icloud.com/.config/superpowers/worktrees/spool-editor/phase-0-safety-fuse \
  codex/phase-0-safety-fuse
cd /Users/kaivan108icloud.com/.config/superpowers/worktrees/spool-editor/phase-0-safety-fuse
git status --short --branch
```

Expected: branch `codex/phase-0-safety-fuse`, based on the committed master spec at `9deb66d`, with none of the main checkout's unrelated untracked files copied into it.

- [ ] **Step 3: Verify the committed plan record in the worktree**

Verify the plan-only branch commit before installing dependencies:

```bash
git show --stat --oneline HEAD
```

- [ ] **Step 4: Install/link dependencies and re-run the clean baseline inside the worktree**

The Python virtual environment is ignored and will not be present in a new linked worktree. Reuse the verified repository environment without copying it into Git, then install/link the pnpm workspace:

```bash
ln -s /Users/kaivan108icloud.com/Documents/spool-editor/engine/.venv engine/.venv
test -f /Users/kaivan108icloud.com/Documents/spool-editor/engine/models/ACTIVE
ACTIVE_MODEL="$(tr -d '\r\n' </Users/kaivan108icloud.com/Documents/spool-editor/engine/models/ACTIVE)"
test -n "$ACTIVE_MODEL"
test -f "/Users/kaivan108icloud.com/Documents/spool-editor/engine/models/$ACTIVE_MODEL"
ln -s /Users/kaivan108icloud.com/Documents/spool-editor/engine/models engine/models
pnpm install --frozen-lockfile
pnpm exec prettier --check docs/superpowers/plans/2026-07-13-private-clip-foundry-phase-0-safety-fuse.md
PRETTIER_BASELINE="$(git rev-parse --git-path phase0-prettier-baseline.txt)"
pnpm exec prettier --list-different "**/*.{ts,tsx,js,jsx,json,md,css,yaml,yml}" | \
  LC_ALL=C sort >"$PRETTIER_BASELINE"
test -s "$PRETTIER_BASELINE"
```

The model link is a read-only prerequisite for the real URL-to-clip acceptance flow; Phase 0 must not install, remove, or switch models through this shared directory. If the active model checks fail, stop and report the missing local prerequisite instead of silently downloading a model.

```bash
(cd engine && .venv/bin/python -m pytest -q)
pnpm typecheck
pnpm test
pnpm build
pnpm lint
pnpm format:check
```

Expected: Python, typecheck, tests, and build match Section 3. `pnpm lint` retains only the exact known package-lint baseline. The clean worktree's frozen Prettier list is expected to be a subset of the main checkout's 51-path result because protected untracked docs and ignored Playwright artifacts are not copied into it. Any worktree formatting path not accounted for by that tracked subset stops execution for user direction. Preserve the frozen list in the linked worktree's Git metadata for the final comparison; do not turn unrelated formatting cleanup into a broad Phase 0 rewrite.

## 7. Task 1 — Slice 0A: preserve history and every published byte

**Files:**

- Modify: `engine/jobs.py`
- Modify: `engine/jobs_store.py`
- Modify: `engine/transcribe_jobs.py`
- Modify: `engine/clip_jobs.py`
- Modify: `engine/app.py`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/tests/test_jobs.py`
- Modify: `engine/tests/test_jobs_store.py`
- Modify: `engine/tests/test_transcribe_jobs.py`
- Modify: `engine/tests/test_clip_jobs.py`
- Modify: `engine/tests/test_api_v1.py`
- Modify: `engine/tests/test_api_v1_clips.py`
- Create: `engine/tests/test_phase0_artifact_safety.py`
- Modify: `packages/types/src/index.ts`
- Modify: `apps/studio/src/components/spool/context.tsx`
- Modify: `apps/studio/test/context.test.ts`

- [ ] **Step 1: Invert the destructive legacy expectations**

Change the existing terminal-cancel, dismiss, and sweep tests so they assert that the job record and final file remain. Use a deterministic persisted marker:

```python
assert manager.dismiss(job_id) is True
hidden = manager.get(job_id)
assert hidden is not None
assert hidden.dismissed_at is not None
assert Path(hidden.file_path).read_bytes() == original_bytes

# Dismissal is idempotent and does not change the first timestamp.
first_dismissed_at = hidden.dismissed_at
assert manager.dismiss(job_id) is True
assert manager.get(job_id).dismissed_at == first_dismissed_at
```

For an active job, assert `cancel()` changes only lifecycle state and never removes any already-published managed file. For `DONE`, `ERROR`, or already-`CANCELLED`, assert cancellation is a no-op: it returns `False`, preserves the existing terminal state, and preserves bytes.

- [ ] **Step 2: Add the cross-manager artifact matrix**

In `test_phase0_artifact_safety.py`, keep an explicit list of published artifact paths and hash only those before and after the operation. Do not hash mutable metadata stores such as `jobs.json`, `transcribe_jobs.json`, `clip_jobs.json`, settings, indexes, or attempt-staging files:

```python
def artifact_hashes(root: Path, paths: list[Path]) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }
```

Parameterize these cases:

1. completed download: terminal cancel, dismiss, and TTL sweep;
2. completed transcription: terminal cancel, dismiss, and TTL sweep;
3. completed clip/render: terminal cancel, dismiss, and TTL sweep;
4. queue-style clear-finished: dismiss every terminal download/transcribe/clip record;
5. each case followed by manager reconstruction from the same JSON store.

The fixture tree must contain source media, `.words.json`, `.srt`, `.vtt`, `.txt`, a clip intermediate, caption sidecar, rendered video, and export file. After each operation and restart, require exact hash-map equality and direct record retrieval by ID.

- [ ] **Step 3: Observe the red tests**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_jobs.py \
  tests/test_transcribe_jobs.py \
  tests/test_clip_jobs.py \
  tests/test_phase0_artifact_safety.py \
  -k "cancel or dismiss or sweep or clear_finished or restart"
```

Expected before implementation: failures show record removal and/or file removal. If a proposed assertion already passes, retain it only when it protects an uncovered invariant.

- [ ] **Step 4: Persist history visibility without deleting identity**

Add `dismissed_at: str | None = None` to each legacy job record and include it in serialization/deserialization. Missing fields in old JSON decode to `None`.

Remove the `JobManager._load_from_store()` branch that currently drops `CANCELLED` records. Restart may downgrade interrupted queued/running work as before, but it must retain every terminal `DONE`, `ERROR`, and `CANCELLED` history record and its dismissal marker.

Implement one idempotent helper per manager using that manager's existing `_persist()` function. Transcription and clip managers use a non-reentrant lock, so mutate under the lock, release it, and persist afterward; do not call `_persist()` while holding those locks and do not invent a nonexistent `store.save()` API:

```python
changed = False
with self._lock:
    job = self._jobs.get(job_id)
    if job is None or job.status not in TERMINAL:
        return False
    if job.dismissed_at is None:
        job.dismissed_at = utc_now_rfc3339()
        changed = True
if changed:
    self._persist()
return True
```

Replace every terminal dismiss/sweep `pop()` and every final-artifact `os.remove()`/`Path.unlink()` with that marker. Keep deletion of active-attempt staging/partial files only; never classify a published source, transcript, clip, caption, render, or export as a partial.

Give transcription and clip managers the same `ttl_seconds`, transient `last_accessed`, history-only `sweep()`, and daemon `start_sweeper()` contract as downloads. `get()` refreshes access; sweep marks only newly expired terminal records and returns that count. Initialize all three from the configured `TROVE_JOB_TTL_SECONDS` in `app.py` and start all three sweepers.

Retire the download sweeper's `_keep_source`/`keep_predicate` preservation path: it existed only because sweep deleted the source record/file. With history-only marking, source identity remains available and no dependency needs to pin queue visibility.

- [ ] **Step 5: Expose additive history fields and truthful projections**

Add these fields to download, transcription, and clip API views:

```json
{
  "dismissed": true,
  "dismissed_at": "2026-07-13T12:34:56.789Z"
}
```

`GET` by ID and history lists retain dismissed records. Queue-facing filters omit `dismissed == true`; source/library and clip projections do not. No existing field is removed or renamed.

Add optional `dismissed`/`dismissed_at` fields to the TypeScript job views. In the Studio adapter, filter dismissed records only from Queue and Import-progress projections; keep completed download records in `mapSources` and completed clip records in `mapClips`. Add a context regression with one dismissed completed source and render: both remain in Library/Clips while neither appears in Queue/Import progress.

- [ ] **Step 6: Run focused and full engine verification**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_jobs.py \
  tests/test_transcribe_jobs.py \
  tests/test_clip_jobs.py \
  tests/test_api_v1.py \
  tests/test_api_v1_clips.py \
  tests/test_phase0_artifact_safety.py
.venv/bin/python -m pytest -q
cd ..
pnpm --filter @spool/studio exec vitest run test/context.test.ts
pnpm --filter @spool/studio typecheck
```

- [ ] **Step 7: Commit Slice 0A**

```bash
git add engine/jobs.py engine/jobs_store.py engine/transcribe_jobs.py engine/clip_jobs.py \
  engine/app.py engine/routes/api_v1.py engine/tests/test_jobs.py engine/tests/test_jobs_store.py \
  engine/tests/test_transcribe_jobs.py \
  engine/tests/test_clip_jobs.py engine/tests/test_api_v1.py \
  engine/tests/test_api_v1_clips.py engine/tests/test_phase0_artifact_safety.py \
  packages/types/src/index.ts apps/studio/src/components/spool/context.tsx \
  apps/studio/test/context.test.ts
git diff --cached --check
git commit -m "fix(engine): make job cleanup history-only"
```

Rollback rule: if a consumer cannot tolerate the additive marker, keep unlink/pop disabled and repair that consumer; never restore destructive cleanup as a compatibility shortcut.

## 8. Task 2 — Slice 0B: fence stale and cancelled worker attempts

**Files:**

- Create: `engine/attempt_staging.py`
- Create: `engine/tests/test_attempt_staging.py`
- Modify: `engine/jobs.py`
- Modify: `engine/transcribe_jobs.py`
- Modify: `engine/clip_jobs.py`
- Modify: `engine/app.py`
- Modify: `engine/clip_runner.py`
- Modify: `engine/tests/test_jobs.py`
- Modify: `engine/tests/test_transcribe_jobs.py`
- Modify: `engine/tests/test_transcribe_pipeline.py`
- Modify: `engine/tests/test_clip_jobs.py`
- Modify: `engine/tests/test_api_v1.py`
- Modify: `engine/tests/test_api_v1_clips.py`

- [ ] **Step 1: Write barrier-based queued-cancel tests for all managers**

Block the sole worker with a first job, enqueue a second job, cancel the second, release the worker, and prove the second target was never called:

```python
assert manager.cancel(cancelled_id) is True
release_first.set()
wait_until_terminal(first_id)
assert second_target.call_count == 0
assert manager.get(cancelled_id).status == "cancelled"
```

Cover initial download, download `resume()`, transcription, and clip submission.

- [ ] **Step 2: Write running-cancel and stale-publication tests**

For each manager, let the target start, cancel it, then make the target either return or raise. Assert the final status remains `cancelled`, no completion/error field is published, and later progress callbacks are ignored.

Add boundary regressions for:

- cancellation during transcription diarization cannot replace transcript artifacts;
- cancellation during `produce_target` cannot submit child renders;
- a dismissed captured job cannot be mutated by its old closure;
- a new resume attempt cannot be mutated by the old attempt's callback;
- cancelled download/transcribe/clip attempts may write only inside their attempt-staging directory: existing published hashes stay unchanged and a new final path never appears;
- a successful attempt promotes staged outputs, rewrites result paths to their final locations, then becomes terminal.

Add a pause/resume barrier: pausing preserves the attempt's yt-dlp `.part` staging files, but `resume()` returns structured `409 attempt_unwinding` until the paused worker has exited. Only then may the new attempt reuse that staging path. The old worker's `finally` must not clean a path now owned by a resume.

- [ ] **Step 3: Observe the race tests fail**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_jobs.py \
  tests/test_transcribe_jobs.py \
  tests/test_clip_jobs.py \
  tests/test_attempt_staging.py \
  tests/test_api_v1.py \
  tests/test_api_v1_clips.py \
  -k "queued_cancel or running_cancel or stale_attempt or attempt_staging or cancel_diarization or cancel_produce or attempt_unwinding"
```

Expected: transcribe/clip targets start after cancellation, exception paths can overwrite `cancelled`, or post-cancel publication/fan-out occurs.

- [ ] **Step 4: Add atomic attempt transitions, not boolean prechecks**

Add transient `_attempt: int = field(default=0, repr=False, compare=False)` and `_worker_active: bool = field(default=False, repr=False, compare=False)` fields. Do not persist either field. Replace the managers' non-reentrant locks with `RLock` because guarded persistence and atomic child admission re-enter manager helpers.

Every state mutation uses one helper whose validation and mutator run under the same lock:

```python
def _mutate_current(self, job_id, captured_job, attempt, expected, mutate) -> bool:
    with self._lock:
        current = self._jobs.get(job_id)
        valid = (
            current is captured_job
            and current._attempt == attempt
            and current.status in expected
            and current.dismissed_at is None
        )
        if not valid:
            return False
        mutate(current)
        return True
```

Never call a boolean “is current” helper and mutate after it returns; that is a time-of-check/time-of-use race. Start, guarded progress, process-handle registration, error, completion, and result application each perform validation plus mutation in one `_mutate_current` critical section, then persist the accepted snapshot.

Increment before every initial submission/resume and capture the number in the worker closure. Cancel/pause increment again so old callbacks become invalid. Set `_worker_active` before executor submission and clear it only in that captured worker's `finally`. A paused job may resume only when `_worker_active` is false.

Represent the pause race with a typed `AttemptUnwindingError`; `/jobs/<id>/resume` maps only that type to `409 {"error":"attempt_unwinding"}`. Preserve the existing not-found/not-resumable behavior for other false returns.

- [ ] **Step 5: Stage every candidate output away from published paths**

`attempt_staging.py` owns same-filesystem attempt directories and promotion descriptors:

```text
<download-dir>/.attempts/download/<job-id>/...
<download-dir>/.attempts/transcribe/<transcribe-id>/...
<download-dir>/.attempts/clip/<clip-job-id>/...
```

Targets write only to their captured staging root and return local result/update data; they do not assign `job.result`, `job.file_path`, terminal status, diarization fields, or final paths directly.

- Download: initialize/persist an attempt staging `out_template` before submission; yt-dlp writes there. On success, promote the completed media to `<download-dir>/<job-id>.<ext>` and only then set `file_path`/`filename`.
- Transcription: extract WAV and write `.words.json`, `.srt`, `.vtt`, and `.txt` under its staging root. Promote all sidecars to the source base only after the current-attempt check; update FTS only after promotion.
- Clip/media: make `ClipRunner` accept a staging output root. Reads resolve from staged output first, then the existing published clip directory; `meta.json`, cut, frame/track, reframe, captions, preview, and render outputs are written under staging. A successful commit promotes the returned relative paths into the published clip tree and rewrites the result payload to final paths.

Promotion occurs inside the same manager lock section that revalidates the captured object/attempt/status and applies the result. `os.replace` stays on the same filesystem. Phase 0 does not claim crash-atomic multi-file promotion; Phase 1 replaces this fuse with Artifact publication/reconciliation.

- [ ] **Step 6: Guard errors, progress, cleanup, and child fan-out**

Exception handlers must mutate only through the atomic helper:

```python
self._mutate_current(
    job.id,
    job,
    attempt,
    {RUNNING},
    lambda current: set_error(current, exc),
)
```

Progress callbacks and process registration carry the captured attempt explicitly and use the same helper. Stale callbacks become no-ops.

In `finally`, clean only the captured attempt's staging root after cancel/error. Preserve paused download staging; do not allow resume until the prior worker clears `_worker_active`. Never glob or unlink a published path.

Add `ClipJobManager.submit_children_if_current(parent, attempt, specs)`: under the manager `RLock`, revalidate the parent once, create/submit all child specs, and apply the parent's child-ID result before releasing the lock. Worker closures cannot start until that lock is released. If cancellation linearized first, it creates zero children; if fan-out linearized first, cancellation does not retroactively erase admitted children.

- [ ] **Step 7: Verify focused races, staging, then the entire engine**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_attempt_staging.py \
  tests/test_jobs.py \
  tests/test_transcribe_jobs.py \
  tests/test_transcribe_pipeline.py \
  tests/test_clip_jobs.py \
  tests/test_api_v1.py \
  tests/test_api_v1_clips.py
.venv/bin/python -m pytest -q
```

- [ ] **Step 8: Commit Slice 0B**

```bash
git add engine/attempt_staging.py engine/jobs.py engine/transcribe_jobs.py \
  engine/clip_jobs.py engine/app.py engine/clip_runner.py \
  engine/tests/test_attempt_staging.py engine/tests/test_jobs.py \
  engine/tests/test_transcribe_jobs.py engine/tests/test_transcribe_pipeline.py \
  engine/tests/test_clip_jobs.py \
  engine/tests/test_api_v1.py engine/tests/test_api_v1_clips.py
git diff --cached --check
git commit -m "fix(engine): fence stale job attempts"
```

## 9. Task 3 — Slice 0C: make the product truthful and fuse off agent mutation

This slice has three independently revertible commits: visible product truth, privacy/offline enforcement, and the agent mutation fuse.

### 9.1 Remove false controls and fabricated output

**Files:**

- Create: `apps/studio/test/product-truth.test.tsx`
- Modify: `apps/studio/src/components/spool/shell.tsx`
- Modify: `apps/studio/src/components/spool/overlays.tsx`
- Modify: `apps/studio/src/components/spool/agent.tsx`
- Modify: `apps/studio/src/components/spool/panels.tsx`
- Modify: `apps/studio/src/components/spool/context.tsx`
- Modify: `apps/studio/src/components/spool/work.tsx`
- Modify: `apps/studio/src/components/spool/cards.tsx`
- Create: `apps/studio/src/lib/action-error.ts`
- Modify: `apps/studio/src/app/import/page.tsx`
- Modify: `apps/studio/src/app/library/page.tsx`
- Modify: `apps/studio/src/app/queue/page.tsx`
- Modify: `apps/studio/src/app/clips/page.tsx`
- Modify: `apps/studio/src/app/clips/[id]/page.tsx`
- Modify: `apps/studio/src/app/clips/[id]/reframe/page.tsx`
- Modify: `apps/studio/src/app/analytics/page.tsx`
- Modify: `apps/studio/src/app/page.tsx`
- Modify: `apps/studio/src/app/sources/[id]/page.tsx`
- Modify: `apps/studio/src/app/brand/page.tsx`
- Modify: `apps/studio/src/app/recipes/page.tsx`
- Modify: `apps/studio/src/app/watches/page.tsx`

- [ ] **Step 1: Add the complete visible-control regression inventory**

Render the relevant pages/components with a mocked `SpoolApiClient` and assert:

- rail and command palette omit Recipes, Watches, Publish, and Analyze/Analytics;
- selected clips expose real Export behavior but no Publish;
- Import exposes URL ingestion and truthful Video/Audio choices only;
- no Files tab/drop zone, Best/1080p/720p choices, dead Retry, fake Undo/Redo, mutation recipe chip, or fabricated analytics number/chart is present;
- the no-op `This week` collection is absent until real clip timestamps exist;
- invalid URL input makes zero API calls and renders an inline actionable error;
- a structured submit rejection preserves the URL, shows no success toast, and renders its error code/message;
- source/clip cards do not invent `fps: 30`, `scenes: 1`, `talking-head`, `9:16`, `opus`, `tiktok`, a local-file origin for an unknown URL, speaker counts, or other unknown values; unknown fields render `—` or are omitted;
- the home/agent surfaces contain no hard-coded recipe list and never claim the read-only agent can drive or mutate the whole app;
- Source/Clip not-found copy no longer claims queue clearing deleted work; it reports an unavailable ID or incomplete import/render and preserves the recovery action;
- every visible mutating API action on Library, Queue, Source, Editor, Reframe, Brand, Recipes, and Watches reports failure; batch actions use `Promise.allSettled`, report exact succeeded/failed counts, and never emit success before the promises settle.

- [ ] **Step 2: Observe the product-truth test fail**

```bash
pnpm --filter @spool/studio exec vitest run test/product-truth.test.tsx
```

- [ ] **Step 3: Remove or truthfully wire each inventory item**

For URL import, validate with `new URL(value)` and allow only `http:`/`https:` before calling the client. Convert structured client errors to visible state; clear the input and show success only after a successful response.

Hide navigation and action entry points only. Do not delete routes, Recipes/Watches/brand-kit data, or backend implementations. Replace analytics content with a plain unavailable-state explanation and no metric values.

Add `action-error.ts` to extract `SpoolApiError.code` and map known actionable codes (`queue_full`, `invalid_url`, `origin_forbidden`, `agent_mutation_disabled`, `egress_consent_required`, `not_resumable`, `timeout`, and `unreachable`) to concise copy while retaining the code for diagnostics. Replace swallowed mutation rejections on every file listed in Section 9.1. A browser autoplay rejection may remain non-fatal; a failed engine mutation may not.

- [ ] **Step 4: Verify Studio behavior and commit it**

```bash
pnpm --filter @spool/studio exec vitest run test/product-truth.test.tsx
pnpm --filter @spool/studio test
pnpm --filter @spool/studio typecheck
pnpm --filter @spool/studio lint
git add apps/studio/test/product-truth.test.tsx \
  apps/studio/src/components/spool/shell.tsx \
  apps/studio/src/components/spool/overlays.tsx \
  apps/studio/src/components/spool/agent.tsx \
  apps/studio/src/components/spool/panels.tsx \
  apps/studio/src/components/spool/context.tsx \
  apps/studio/src/components/spool/work.tsx apps/studio/src/components/spool/cards.tsx \
  apps/studio/src/lib/action-error.ts \
  apps/studio/src/app/import/page.tsx apps/studio/src/app/library/page.tsx \
  apps/studio/src/app/queue/page.tsx \
  apps/studio/src/app/clips/page.tsx 'apps/studio/src/app/clips/[id]/page.tsx' \
  'apps/studio/src/app/clips/[id]/reframe/page.tsx' \
  apps/studio/src/app/analytics/page.tsx apps/studio/src/app/page.tsx \
  'apps/studio/src/app/sources/[id]/page.tsx' apps/studio/src/app/brand/page.tsx \
  apps/studio/src/app/recipes/page.tsx apps/studio/src/app/watches/page.tsx
git diff --cached --check
git commit -m "fix(studio): remove false controls"
```

Before committing, inspect `git diff --cached --name-only` and unstage any generated/cache file; every staged source path must be one of the files listed in Section 9.1.

### 9.2 Require explicit egress consent and make Offline real

**Files:**

- Create: `engine/network_policy.py`
- Create: `engine/tests/test_network_policy.py`
- Create: `apps/studio/test/privacy-mode.test.tsx`
- Modify: `engine/settings.py`
- Modify: `engine/app.py`
- Modify: `engine/clip/llm.py`
- Modify: `engine/models_store.py`
- Modify: `engine/watcher.py`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/tests/test_settings.py`
- Modify: `engine/tests/test_llm.py`
- Modify: `engine/tests/test_moments.py`
- Modify: `engine/tests/test_models_store.py`
- Modify: `engine/tests/test_watcher.py`
- Modify: `engine/tests/test_api_v1.py`
- Modify: `packages/types/src/index.ts`
- Modify: `packages/api-client/src/index.ts`
- Modify: `apps/studio/src/app/layout.tsx`
- Modify: `apps/studio/src/app/onboarding/page.tsx`
- Modify: `apps/studio/src/app/settings/page.tsx`
- Modify: `apps/studio/src/app/import/page.tsx`
- Modify: `apps/studio/src/components/spool/shell.tsx`
- Modify: `apps/studio/src/components/spool/context.tsx`

- [ ] **Step 1: Write provider, consent, and last-moment race tests**

Engine tests must prove:

1. defaults are `reasoning_provider == "none"` and `reasoning_egress_consent is False`;
2. selecting `codex` without consent invokes no provider and creates no ClipJob;
3. setting provider plus explicit consent permits the provider call;
4. changing `codex -> none -> codex` resets consent;
5. `offline=True` blocks egress even with consent;
6. a failed atomic settings save cannot activate provider, consent, or offline state;
7. a barrier holds execution after a consented Codex request is selected, consent is revoked, then the barrier releases; the provider spy stays at zero and the caller receives `egress_consent_required`.

The manual moments route preflights provider/consent before job admission so it can return the structured 409 with no ClipJob. The LLM adapter repeats the check against current settings immediately before network execution so queued or non-route callers cannot use a stale consent snapshot.

- [ ] **Step 2: Write an engine-wide offline network-policy matrix**

Create a small lock-protected `NetworkPolicy` whose `egress(purpose)` context manager rejects when offline and counts active non-loopback operations until `finally`. Its atomic `enable_offline()` fails with `network_work_active` while the count is non-zero, so the UI never displays Offline while a remote socket is active.

Tests with DNS, runner, `urlopen`, watcher, and provider spies must prove:

- offline URL single/bulk submission returns `409 {"error":"offline_network_disabled"}` before URL DNS validation, job admission, or executor submission;
- offline model install and channel/playlist watch create/update/scan return the same code before DNS, `urlopen`, or yt-dlp;
- the opt-in background watch poller skips remote watches while offline;
- local folder scanning, local transcription, rendering, and loopback API clients still work;
- queued network work accepted before Offline was enabled rechecks the policy in its worker and performs zero network calls;
- an active policy lease makes the Offline settings patch return `409 {"error":"network_work_active"}` without persisting the toggle.

- [ ] **Step 3: Observe the privacy/offline tests fail**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_network_policy.py tests/test_settings.py tests/test_llm.py \
  tests/test_moments.py tests/test_models_store.py tests/test_watcher.py tests/test_api_v1.py \
  -k "provider or consent or egress or offline or network_policy"
cd ..
pnpm --filter @spool/studio exec vitest run test/privacy-mode.test.tsx
```

- [ ] **Step 4: Implement atomic provider/settings semantics**

Extend settings validation and API types with:

```ts
type ReasoningProvider = "none" | "codex";

interface EngineSettings {
  offline: boolean;
  reasoning_provider: ReasoningProvider;
  reasoning_egress_consent: boolean;
}
```

`SPOOL_LLM_PROVIDER` may seed `none|codex` at boot. `SPOOL_LLM_EGRESS_CONSENT=1` may seed consent only when the effective provider is `codex`; no environment default silently opts in. Provider changes reset consent unless the same valid patch explicitly grants it, and `none` always forces false.

Make `SettingsStore.update()` copy-on-write: build the next override dict, write/replace it atomically, then assign it in memory. The Offline transition holds the `NetworkPolicy` lock from active-lease validation through settings-file replacement and in-memory/env commit, so a new lease cannot enter between preflight and persistence. If validation or replacement fails, neither policy state, settings state, nor process environment changes.

- [ ] **Step 5: Put every exposed non-loopback operation behind the policy**

Acquire a short `NetworkPolicy.egress("url_validation")` lease around DNS validation and any synchronous metadata probe before admission, then acquire a new worker lease around yt-dlp. Apply the same lease to model download, remote watcher listing/target validation, and Codex execution. The settings route, background poller, manual endpoints, CLI, MCP, and direct helper functions must all converge on those checks; a boolean precheck or UI-only toggle is insufficient.

- [ ] **Step 6: Correct privacy copy and labels**

Onboarding and Settings name the selected provider and state that transcripts leave the machine for remote reasoning. Show consent only for `codex`; failed persistence leaves the UI disabled. Render exactly one status:

```ts
offline
  ? "Offline"
  : reasoning_provider === "codex" && reasoning_egress_consent
    ? "Remote reasoning enabled"
    : "Fully local";
```

`Fully local` is explicitly the reasoning mode while URL ingestion still discloses that it needs network access. When `Offline` is active, Studio disables URL import and remote model/watch actions, while local media operations remain usable. Remove blanket “everything runs on your machine” claims whenever remote reasoning is active.

- [ ] **Step 7: Verify and commit the privacy/offline fuse alone**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_network_policy.py tests/test_settings.py tests/test_llm.py \
  tests/test_moments.py tests/test_models_store.py tests/test_watcher.py tests/test_api_v1.py
.venv/bin/python -m pytest -q
cd ..
pnpm --filter @spool/studio exec vitest run test/privacy-mode.test.tsx
pnpm --filter @spool/studio test
pnpm --filter @spool/studio typecheck
pnpm --filter @spool/studio lint
pnpm --filter @spool/api-client lint
pnpm --filter @spool/types lint
git add engine/network_policy.py engine/settings.py engine/app.py engine/clip/llm.py \
  engine/models_store.py engine/watcher.py engine/routes/api_v1.py \
  engine/tests/test_network_policy.py engine/tests/test_settings.py engine/tests/test_llm.py \
  engine/tests/test_moments.py engine/tests/test_models_store.py \
  engine/tests/test_watcher.py engine/tests/test_api_v1.py \
  packages/types/src/index.ts packages/api-client/src/index.ts \
  apps/studio/test/privacy-mode.test.tsx apps/studio/src/app/layout.tsx \
  apps/studio/src/app/onboarding/page.tsx apps/studio/src/app/settings/page.tsx \
  apps/studio/src/app/import/page.tsx apps/studio/src/components/spool/shell.tsx \
  apps/studio/src/components/spool/context.tsx
git diff --cached --check
git commit -m "fix(privacy): require consent and enforce offline mode"
```

### 9.3 Reject every agent write

**Files:**

- Modify: `apps/studio/test/context-mutations.test.tsx`
- Modify: `apps/studio/src/components/spool/context.tsx`
- Modify: `apps/studio/src/components/spool/agent.tsx`
- Modify: `engine/clip/agent.py`
- Modify: `engine/clip/agent_tools.py`
- Modify: `engine/mcp_server.py`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/tests/test_agent_loop.py`
- Modify: `engine/tests/test_mcp.py`
- Modify: `engine/tests/test_api_v1.py`

- [ ] **Step 1: Replace mutation approval tests with zero-write tests**

Define an explicit frozen read-only allowlist from the current catalog (`list/get/search/status/storage/capabilities/source-energy/source-scenes/rank` operations). Add a classification test requiring every catalog entry to be either on that allowlist with `writes is False`, or outside it with `writes is True`; an unclassified future tool fails the suite. Force selection of every non-allowlisted tool and assert its implementation/client spy is never called and the result is exactly:

```json
{
  "error": "agent_mutation_disabled",
  "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships."
}
```

Enumerate mutating/exporting MCP tools. Keep their schemas advertised for Phase 0 contract compatibility, but route every invocation through one central named guard before any `TroveClient` call. Each returns the same disabled error; read-only inspection tools still execute. Assert the exact advertised tool-name set so a mutator cannot accidentally bypass the guard by being registered through a different path. In `context-mutations.test.tsx`, a stale/malicious confirmation response cannot trigger a second `agent()` call and `confirmTool` is never sent.

- [ ] **Step 2: Observe the mutation suites fail**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_agent_loop.py tests/test_mcp.py tests/test_api_v1.py \
  -k "mutation or writes or confirm or read_only"
cd ..
pnpm --filter @spool/studio exec vitest run test/context-mutations.test.tsx
```

- [ ] **Step 3: Add the defense-in-depth mutation fuse**

Render only the explicit read-only allowlist in the Phase 0 in-app agent prompt. Before any `Tool` implementation executes in `run_agent`, reject a tool outside that allowlist or with `tool.writes is True`; do not rely on prompt omission, the flag alone, or UI state.

For MCP, change the shared wrapper to accept tool identity (for example `_safe(tool_name, call)`) and reject names outside the explicit read-only MCP allowlist before evaluating `call`. Every registered MCP function, including word edit and bulk download, must pass its own canonical name through this wrapper. Propagate the structured code through `/api/v1/agent` and MCP without converting it to a generic exception.

Remove confirmation replay, mutating slash commands, fake Undo, and mutating recipe chips from Studio. Manual REST and CLI mutation methods stay intact.

- [ ] **Step 4: Verify and commit the agent fuse alone**

```bash
cd engine
.venv/bin/python -m pytest -q tests/test_agent_loop.py tests/test_mcp.py tests/test_api_v1.py
.venv/bin/python -m pytest -q
cd ..
pnpm --filter @spool/studio exec vitest run test/context-mutations.test.tsx
pnpm --filter @spool/studio test
pnpm --filter @spool/studio typecheck
pnpm --filter @spool/studio lint
git add engine/clip/agent.py engine/clip/agent_tools.py engine/mcp_server.py \
  engine/routes/api_v1.py engine/tests/test_agent_loop.py engine/tests/test_mcp.py \
  engine/tests/test_api_v1.py apps/studio/src/components/spool/context.tsx \
  apps/studio/src/components/spool/agent.tsx apps/studio/test/context-mutations.test.tsx
git diff --cached --check
git commit -m "fix(agent): disable mutation until Phase 4"
```

Before each Section 9 commit, inspect `git diff --cached --name-only`; no generated/cache file or path outside that subsection may be staged.

## 10. Task 4 — Slice 0D: enforce local security and bounded admission

**Files:**

- Create: `engine/job_capacity.py`
- Modify: `engine/safety.py`
- Modify: `engine/config.py`
- Modify: `engine/app.py`
- Modify: `engine/jobs.py`
- Modify: `engine/transcribe_jobs.py`
- Modify: `engine/clip_jobs.py`
- Modify: `engine/clip_runner.py`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/tests/test_safety.py`
- Modify: `engine/tests/test_config.py`
- Modify: `engine/tests/test_jobs.py`
- Modify: `engine/tests/test_transcribe_jobs.py`
- Modify: `engine/tests/test_clip_jobs.py`
- Modify: `engine/tests/test_api_v1.py`
- Modify: `engine/tests/test_api_v1_clips.py`

### 10.1 Security boundary

- [ ] **Step 1: Add mapped-address, Origin, proxy, and limiter regressions**

Parameterize direct literals and mocked DNS answers for:

```text
::ffff:127.0.0.1
::ffff:10.0.0.1
::ffff:172.16.0.1
::ffff:192.168.0.1
::ffff:169.254.169.254
::ffff:8.8.8.8
```

The first five must fail safe-URL validation. Mapped public `::ffff:8.8.8.8`, native public IPv4, and native public IPv6 controls must pass.

Use a mutation route with a counter. Hostile and literal `null` Origins must return `403 origin_forbidden` and leave both the route counter and the requester's rate-limiter bucket unchanged, even with a correct bearer. A loopback Studio Origin and missing-Origin authenticated direct Flask request must execute. Register this guard through/alongside `attach_cors()` before `app.py` registers rate limiting.

Assert spoofed XFF does not change the rate-limit identity by default. With `TROVE_TRUST_PROXY_HOPS=1`, assert one valid right-most hop is honored and malformed/undersized chains fall back to `remote_addr`.

With a fake clock and `TROVE_RATE_LIMIT_MAX_KEYS=4`, create more than four identities and prove expired keys are pruned, retained keys never exceed four, and the exact least-recently-seen key is evicted; lexical key order breaks timestamp ties.

Invalid or negative `TROVE_TRUST_PROXY_HOPS` safely becomes `0` (ignore XFF), and invalid/non-positive `TROVE_RATE_LIMIT_MAX_KEYS` becomes `4096`; add config tests and a warning assertion for both fallbacks.

Retain explicit `test_config.py` coverage that the default bind is loopback and a non-loopback bind refuses startup without authentication; Phase 0 must not regress the security boundary already present.

- [ ] **Step 2: Observe the focused security failures**

```bash
cd engine
.venv/bin/python -m pytest -q tests/test_safety.py tests/test_config.py tests/test_api_v1.py \
  -k "mapped or origin or forwarded or proxy or rate_limit_retention"
```

- [ ] **Step 3: Implement the smallest security boundary**

Normalize `IPv6Address.ipv4_mapped` before blocked-range checks. Add a `before_request` mutation-Origin guard for `/api/*` and `POST|PUT|PATCH|DELETE` before view execution.

Derive client IP from `request.remote_addr` unless an explicitly positive trusted-hop count validates the forwarding chain. Prune expired limiter entries under its existing lock before enforcing the configured deterministic key cap.

- [ ] **Step 4: Verify and commit the security boundary alone**

```bash
cd engine
.venv/bin/python -m pytest -q tests/test_safety.py tests/test_config.py tests/test_api_v1.py
.venv/bin/python -m pytest -q
cd ..
git add engine/safety.py engine/config.py engine/app.py engine/routes/api_v1.py \
  engine/tests/test_safety.py engine/tests/test_config.py engine/tests/test_api_v1.py
git diff --cached --check
git diff --cached --name-only
git commit -m "fix(security): harden local request boundaries"
```

### 10.2 Bounded admission

- [ ] **Step 5: Write capacity-plus-one tests for every pool**

Create the shared contract test around:

```python
class QueueFullError(RuntimeError):
    pass


def pending_capacity(max_workers: int) -> int:
    return min(32, max(4, 4 * max_workers))
```

Barrier-fill exactly `pending_capacity(workers)` admitted, not-yet-drained wrappers for download, download resume, transcription, and clip/media pools. The next submission must:

- raise `QueueFullError` at the manager boundary;
- create no job/store row;
- leave inflight accounting unchanged;
- make zero additional executor `submit` calls.

For download `resume()`, the pre-existing paused row remains byte-for-byte/logically unchanged and no new attempt number is consumed when admission fails.

Add recovery tests for each manager: the reservation returns after target success, target exception, and queued cancellation followed by stale-wrapper drain. Prove normal cancel never calls `Future.cancel()`. For shutdown, assert `wait=False` returns without accepting new work and releases outstanding reservations only as its wrappers drain; assert `wait=True` returns with accounting at zero. Inject an executor whose `submit()` raises and assert atomic rollback of the reservation, newly-created record, and persisted JSON; for resume, assert the existing paused record is restored unchanged.

Remove the legacy `queue_size` constructor argument and `_queue_size` branch from `JobManager`. Rewrite its `queue_size=0 -> RuntimeError("pool full")` test to the shared capacity/`QueueFullError` contract so two saturation semantics cannot coexist.

At HTTP level, assert exact status/body/header:

```python
assert response.status_code == 429
assert response.json == {"error": "queue_full", "retry_after": 1}
assert response.headers["Retry-After"] == "1"
```

Cover bulk partial overflow: accepted rows count toward `submitted`, each over-capacity row is `{ "url": "...", "error": "queue_full", "retry_after": 1 }`, the response is 207 with `Retry-After: 1`, and no hidden jobs exist.

Nested `produce` fan-out is all-or-none: `submit_children_if_current` calls `reserve_many(len(specs))` under the manager lock before allocating any child ID. If the whole batch does not fit, it creates zero children and records a visible parent error/result `{ "error": "queue_full", "requested": N, "clip_jobs": [] }`. If it fits, every admitted child ID is published in the parent result before workers can start. No partial or hidden child set is allowed.

Inject a mid-batch `executor.submit()` failure too. Because the manager lock is held, started wrappers cannot pass their current-attempt gate; cancel queued futures, invalidate/remove every provisional child, release reservations for futures that will not run, and let any already-running stale wrapper release its own reservation. The persisted store and parent result expose zero children.

- [ ] **Step 6: Observe the admission tests fail**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_jobs.py tests/test_transcribe_jobs.py tests/test_clip_jobs.py \
  tests/test_api_v1.py tests/test_api_v1_clips.py \
  -k "capacity or queue_full or bulk_overflow or fanout_overflow"
```

- [ ] **Step 7: Reserve atomically before creating work**

Use one locked admitted-wrapper counter and idempotent reservation lease per manager. Reserve before record insertion, persistence, or executor submission. Normal cancellation only invalidates state; it leaves the queued future intact so its wrapper can enter, fail the current-attempt gate, and release in `finally`. If executor submission itself raises, release the lease, roll back the record atomically, and persist the restored state. For multi-submit rollback only, a successfully cancelled not-yet-started future releases its lease explicitly; an already-running stale wrapper releases its own lease in `finally`. `shutdown(wait=False)` calls executor shutdown without cancelling futures and permits the counter to drain asynchronously; `shutdown(wait=True)` guarantees zero before return.

Catch `QueueFullError` before `_submit_one`'s existing generic `RuntimeError -> busy` mapping and map only that type to structured 429. Preserve distinct validation, auth, not-found, not-resumable, and legacy runtime responses.

Expose the exact additive capabilities shape:

```json
{
  "limits": {
    "pending_capacity": {
      "download": 16,
      "transcription": 4,
      "media": 8
    }
  }
}
```

Values come from the live manager instances and their configured worker counts.

- [ ] **Step 8: Run focused and full verification**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_safety.py tests/test_config.py tests/test_jobs.py \
  tests/test_transcribe_jobs.py tests/test_clip_jobs.py \
  tests/test_api_v1.py tests/test_api_v1_clips.py
.venv/bin/python -m pytest -q
```

- [ ] **Step 9: Commit bounded admission alone**

```bash
git add engine/job_capacity.py engine/app.py engine/jobs.py engine/transcribe_jobs.py \
  engine/clip_jobs.py engine/clip_runner.py engine/routes/api_v1.py \
  engine/tests/test_jobs.py engine/tests/test_transcribe_jobs.py \
  engine/tests/test_clip_jobs.py engine/tests/test_api_v1.py engine/tests/test_api_v1_clips.py
git diff --cached --check
git diff --cached --name-only
git commit -m "fix(engine): bound worker admission"
```

## 11. Task 5 — Slice 0E: proxy authenticated Studio JSON, SSE, and media

**Files:**

- Create: `apps/studio/src/lib/engine-proxy.ts`
- Create: `apps/studio/src/app/api/engine/[...path]/route.ts`
- Create: `apps/studio/test/engine-proxy.test.ts`
- Modify: `apps/studio/src/lib/engine.ts`
- Modify: `apps/studio/.env.example`
- Modify: `apps/studio/e2e/url-to-clip.spec.ts`
- Modify: `apps/studio/playwright.config.ts`
- Modify: `apps/studio/test/api-client.test.ts`
- Modify: `packages/api-client/src/index.ts`

- [ ] **Step 1: Write proxy unit tests with a fetch spy**

Cover all of these before implementing the proxy:

1. upstream URL is constructed only from `SPOOL_ENGINE_URL`, catch-all segments, and the incoming query string;
2. configured server-only `SPOOL_ENGINE_TOKEN` becomes `Authorization: Bearer ...` and is never returned downstream; when omitted, no Authorization header is invented;
3. method and body are preserved for JSON mutations;
4. `text/event-stream` response bodies stream incrementally rather than being buffered;
5. video/download bodies, `Range`, `Content-Type`, `Content-Length`, `Content-Range`, `Accept-Ranges`, and `Content-Disposition` survive;
6. the upstream request forwards only `Accept`, `Content-Type`, `Range`, `If-Range`, standard `If-*` conditionals, `Idempotency-Key`, and `Last-Event-ID`; browser `Origin`, cookies, host, and incoming authorization never reach Flask, only the configured server token may create Authorization, every fixed/dynamically `Connection`-nominated hop-by-hop header is stripped, and downstream `Set-Cookie` plus every `Access-Control-*` header is stripped;
7. a non-loopback Next request URL, hostile/public/other-loopback/`null` Origin receives `403 origin_forbidden` for every method before the fetch spy is called, and missing mutation Origin is rejected too;
8. a present exact loopback same-origin mutation is forwarded, while exact loopback same-origin or missing-Origin `GET`/`HEAD` media remains available;
9. decoded `..`, encoded slash/backslash, scheme-relative, absolute-URL, and host-changing path inputs cannot escape the configured engine origin or cause bearer forwarding to another host.
10. the API client preserves `Accept: text/event-stream` on SSE instead of overwriting it with `application/json`.
11. upstream redirects are returned without automatic following, so a redirect cannot carry the server token to another origin.

- [ ] **Step 2: Observe the new proxy tests fail**

```bash
pnpm --filter @spool/studio exec vitest run test/engine-proxy.test.ts test/api-client.test.ts
```

- [ ] **Step 3: Implement one testable forwarding function**

`engine-proxy.ts` owns configuration validation, URL joining, Origin policy, header filtering, bearer injection, and streaming `Response` construction. First require the parsed incoming Next request URL to use `localhost`, `127.0.0.0/8`, or `::1`; a spoofed public `Host` cannot define a trusted origin. For every request with an `Origin`, require that parsed Origin to be loopback and exactly match the incoming Next request's own origin before token injection, including `GET`/`HEAD`; reject other-loopback, hostile, and `null` values. When `Origin` is missing, allow only `GET`/`HEAD` so DOM media works without opening originless mutations. Build the upstream path by percent-encoding each catch-all segment, append only the incoming `URLSearchParams`, and assert the resulting `origin` equals the parsed configured engine origin before adding Authorization. Reject traversal, slash/backslash, scheme-relative, or absolute-URL segments instead of normalizing them. Set upstream fetch `redirect: "manual"`; never automatically follow an engine redirect while holding its bearer token.

Build the outbound request from this explicit allowlist only: `Accept`, `Content-Type`, `Range`, `If-Range`, `If-Match`, `If-None-Match`, `If-Modified-Since`, `If-Unmodified-Since`, `Idempotency-Key`, and `Last-Event-ID`. Validate browser Origin locally and then strip it rather than forwarding it to Flask. Add only the configured bearer. In addition to the fixed hop-by-hop set, parse `Connection` tokens case-insensitively and remove each nominated header in both directions. Never forward browser cookies or emit engine `Set-Cookie`; strip every downstream `Access-Control-*` header because the browser-facing route is same-origin and owns its own boundary.

In Next 16 the route context is `params: Promise<{ path: string[] }>`. Each `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS` adapter awaits `params` and passes the resolved path to the testable forwarding function.

Pass the incoming `ReadableStream` directly; on Node, set the request-init `duplex: "half"` extension only when a body exists. Do not buffer SSE or media responses with `.text()`/`.arrayBuffer()`.

Default `SPOOL_ENGINE_URL` to `http://127.0.0.1:8899`. Treat an empty `SPOOL_ENGINE_TOKEN` as unauthenticated loopback development, but require the token in the authenticated acceptance test. Never use a `NEXT_PUBLIC_*` token.

- [ ] **Step 4: Point the browser client at same-origin `/api/engine`**

Construct the singleton as:

```ts
export const engine = new SpoolApiClient({ baseUrl: "/api/engine" });
```

Keep the API client's existing `/api/v1/...` suffixes. Its synchronous source/render/artifact helpers must now yield `/api/engine/api/v1/...`, so `<video>`, ranged playback, and `<a download>` all traverse the authenticated proxy without exposing credentials.

Repair `SpoolApiClient.headers()` so it sets `Accept: application/json` only when the caller did not supply an Accept header. This preserves `text/event-stream` for `subscribeEvents()` through the proxy.

Document only server-side variables and mark the token as required whenever Flask `TROVE_TOKEN` is configured:

```dotenv
SPOOL_ENGINE_URL=http://127.0.0.1:8899
# SPOOL_ENGINE_TOKEN=replace-with-the-engine-token
```

- [ ] **Step 5: Token-enable the existing Playwright golden flow**

Start Flask with token auth enabled and Studio with the same server-only token. Add bearer auth to direct test-helper polling that intentionally bypasses Studio. At the beginning of the UI flow, open Settings, select Codex, acknowledge transcript egress, save successfully, and assert the `Remote reasoning enabled` label before starting discovery. Then drive import through Studio and verify:

- Studio connects through the proxy;
- SSE reaches a completed snapshot;
- source and rendered clip media return playable bytes and range headers through `/api/engine`;
- the rendered download succeeds through the proxy;
- direct unauthenticated Flask equivalents return 401.

Update the Playwright config/header comment to name the external token harness below and `E2E_ENGINE_API_URL`; do not add a second implicit server launcher that can race the explicit isolated-state processes. Set the Playwright test timeout to `900_000`: the existing sequential poll budgets total 690 seconds before ordinary UI assertions, so the current 360-second cap cannot represent the documented flow.

- [ ] **Step 6: Verify unit, build, and authenticated E2E**

Use isolated engine state and launch both servers with one shared token. Rename the E2E helper variable to `E2E_ENGINE_API_URL`; `SPOOL_ENGINE_URL` is reserved for the Studio server's engine origin and must not include `/api/v1`.

```bash
set -euo pipefail

pnpm --filter @spool/studio exec vitest run test/engine-proxy.test.ts test/api-client.test.ts
pnpm --filter @spool/studio test
pnpm --filter @spool/studio typecheck
pnpm --filter @spool/studio lint
pnpm --filter @spool/api-client lint
pnpm --filter @spool/studio build

TOKEN="phase0-e2e-$(openssl rand -hex 16)"
RUN_DIR="$(mktemp -d /tmp/spool-phase0-e2e.XXXXXX)"
ENGINE_LOG="$RUN_DIR/engine.log"
STUDIO_LOG="$RUN_DIR/studio.log"

cleanup_phase0_e2e() {
  for PID in "${STUDIO_PID:-}" "${ENGINE_PID:-}"; do
    test -n "$PID" || continue
    pkill -TERM -P "$PID" 2>/dev/null || true
    kill "$PID" 2>/dev/null || true
  done
  wait "${STUDIO_PID:-}" "${ENGINE_PID:-}" 2>/dev/null || true
  rm -rf "$RUN_DIR"
}
trap cleanup_phase0_e2e EXIT INT TERM

test -z "$(lsof -nP -iTCP:8899 -sTCP:LISTEN -t 2>/dev/null)"
test -z "$(lsof -nP -iTCP:3000 -sTCP:LISTEN -t 2>/dev/null)"
test -L engine/models
test -f "engine/models/$(tr -d '\r\n' <engine/models/ACTIVE)"
codex login status >/dev/null

(
  cd engine
  exec env TROVE_TOKEN="$TOKEN" TROVE_RATE_LIMIT=0 \
    TROVE_DOWNLOAD_DIR="$RUN_DIR/data" .venv/bin/python app.py
) >"$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!

(
  cd apps/studio
  exec env SPOOL_ENGINE_URL="http://127.0.0.1:8899" \
    SPOOL_ENGINE_TOKEN="$TOKEN" \
    ./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000
) >"$STUDIO_LOG" 2>&1 &
STUDIO_PID=$!

for _ in {1..120}; do
  kill -0 "$ENGINE_PID"
  curl -fsS -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8899/api/v1/health >/dev/null && break
  sleep 0.25
done
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8899/api/v1/health >/dev/null

for _ in {1..120}; do
  kill -0 "$STUDIO_PID"
  curl -fsS http://127.0.0.1:3000/import >/dev/null && break
  sleep 0.25
done
curl -fsS http://127.0.0.1:3000/import >/dev/null

AUTH_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8899/api/v1/jobs)"
NO_AUTH_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8899/api/v1/jobs)"
PROXY_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:3000/api/engine/api/v1/jobs)"
test "$AUTH_CODE" = 200
test "$NO_AUTH_CODE" = 401
test "$PROXY_CODE" = 200

E2E_ENGINE_API_URL="http://127.0.0.1:8899/api/v1" \
TROVE_TOKEN="$TOKEN" SPOOL_STUDIO_URL="http://127.0.0.1:3000" \
  pnpm --filter @spool/studio e2e -- e2e/url-to-clip.spec.ts
```

The E2E requires the repository's documented Chrome, ffmpeg, yt-dlp, network, and linked active Whisper-model prerequisites. `TROVE_RATE_LIMIT=0` disables throttling only for this isolated acceptance process so UI/SSE/polling traffic cannot make the flow flaky. A missing prerequisite is reported separately from a behavioral test failure.

- [ ] **Step 7: Commit Slice 0E**

```bash
git add apps/studio/src/lib/engine-proxy.ts \
  'apps/studio/src/app/api/engine/[...path]/route.ts' \
  apps/studio/test/engine-proxy.test.ts apps/studio/src/lib/engine.ts \
  apps/studio/.env.example apps/studio/e2e/url-to-clip.spec.ts \
  apps/studio/playwright.config.ts apps/studio/test/api-client.test.ts \
  packages/api-client/src/index.ts
git diff --cached --check
git diff --cached --name-only
git commit -m "fix(studio): proxy authenticated engine traffic"
```

## 12. Task 6 — Slice 0F: make one contract pass across every client

**Files:**

- Create: `contracts/v1/phase0-contract.json`
- Modify: `packages/types/src/index.ts`
- Modify: `packages/api-client/src/index.ts`
- Modify: `apps/studio/test/api-client.test.ts`
- Modify: `engine/routes/api_v1.py`
- Modify: `engine/trove_client.py`
- Modify: `engine/cli.py`
- Modify: `engine/mcp_server.py`
- Modify: `engine/tests/test_api_v1.py`
- Modify: `engine/tests/test_trove_client.py`
- Modify: `engine/tests/test_trove_client_automation.py`
- Modify: `engine/tests/test_cli.py`
- Modify: `engine/tests/test_mcp.py`
- Delete: `packages/mcp-client/src/index.ts`
- Delete: `packages/mcp-client/package.json`
- Delete: `packages/mcp-client/tsconfig.json`
- Modify: `apps/studio/package.json`
- Modify: `apps/studio/next.config.ts`
- Modify: `README.md`
- Modify: `docs/Spool_Engineering-Spec.md`
- Modify: `docs/PROGRESS.md`
- Modify: `pnpm-lock.yaml`

- [ ] **Step 1: Check in the canonical fixture**

The JSON fixture contains concrete request/response cases, not schemas with unresolved placeholders. `response_subset` is intentionally named: the word record is additive and carries timestamps/edit metadata, so consumers assert these stable fields while typing the complete response as `TranscriptWord`.

```json
{
  "word_edit": {
    "request": { "op": "set_text", "w": "corrected" },
    "legacy_request": { "op": "set_text", "text": "corrected" },
    "response_subset": { "tid": "tx_1", "word": { "idx": 7, "w": "corrected" } }
  },
  "bulk_submit": {
    "request": { "urls": ["https://8.8.8.8/video-a", "--exec=evil"] },
    "response": {
      "submitted": 1,
      "failed": 1,
      "results": [
        {
          "url": "https://8.8.8.8/video-a",
          "id": "job_1",
          "title": "https://8.8.8.8/video-a"
        },
        { "url": "--exec=evil", "error": "unsupported_url" }
      ]
    }
  },
  "queue_full": { "error": "queue_full", "retry_after": 1 },
  "agent_mutation_disabled": {
    "error": "agent_mutation_disabled",
    "message": "Agent changes are disabled until the Phase 4 approval and undo contract ships."
  }
}
```

The Flask bulk fixture test replaces only `enqueue_download` with a deterministic in-memory stub returning `job_1`; it still exercises the real bulk route, safety validation, deferred-probe title, status, and response assembly without network or executor timing.

- [ ] **Step 2: Make every surface consume the fixture and observe failures**

Add fixture-backed assertions for:

- Flask word edit and bulk endpoints;
- TypeScript `editWord()` and `bulkSubmit()` request/response types, asserting the word fixture as a stable response subset;
- Python `TroveClient.edit_word()`;
- CLI word edit command;
- MCP word-edit and bulk tool schemas accept the canonical fixture fields but execution returns the Phase 0 `agent_mutation_disabled` envelope with zero Python-client calls;
- structured queue-full and agent-disabled envelopes.

Also assert the existing `/api/v1/openapi.json` entries for word edit and bulk submission contain these exact request/response properties. Phase 1 makes OpenAPI authoritative; Phase 0 may not leave its exposed documentation knowingly stale.

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_api_v1.py tests/test_trove_client.py tests/test_trove_client_automation.py \
  tests/test_cli.py tests/test_mcp.py \
  -k "contract_fixture or word_edit or bulk_submit"
cd ..
pnpm --filter @spool/studio exec vitest run test/api-client.test.ts
```

Expected: Python/CLI still send `text`, the MCP tool schema still advertises `text`, and the TypeScript bulk return type still expects `{jobs}`. The already-landed MCP mutation fuse must continue to prevent an underlying call during this red test.

- [ ] **Step 3: Repair the wire additively**

Define discriminated `BulkSubmitResult` success/error rows and:

```ts
interface BulkSubmitResponse {
  submitted: number;
  failed: number;
  results: BulkSubmitResult[];
}
```

In `packages/types`, also replace the current open-ended word-edit shape with the exact discriminated request and typed response:

```ts
export type WordEditRequest =
  | { op: "set_text" | "insert_after"; w: string }
  | { op: "delete" | "merge_next"; w?: never };

export interface WordEditResponse {
  tid: string;
  word: TranscriptWord;
}
```

`SpoolApiClient.editWord()` accepts `WordEditRequest` and returns `Promise<WordEditResponse>`; it must not retain `op: string` or `Record<string, unknown>`.

Make the TypeScript client, Python client, and CLI send `w`. Change the disabled MCP word-edit schema from `text` to `w`, but keep its central mutation guard ahead of the Python client so it returns `agent_mutation_disabled` and performs zero writes. Flask accepts `w` canonically; for a request using legacy `text`, apply the same operation and attach `Warning: 299 Spool "text is deprecated; use w"`. If both fields are supplied and differ, return structured `400 conflicting_word_text` rather than choosing silently.

- [ ] **Step 4: Remove the unused TypeScript MCP stub clearly**

Remove `@spool/mcp-client` from Studio dependencies and transpilation config, delete its three package files, and replace the current architecture claims in README, `docs/Spool_Engineering-Spec.md`, and `docs/PROGRESS.md` with the truthful Python MCP surface. Refresh only the affected lockfile entries:

```bash
pnpm install --lockfile-only
pnpm install --frozen-lockfile
! git grep -n -E "@spool/mcp-client|packages/mcp-client|api-client.*, *mcp-client" -- \
  apps packages README.md docs/Spool_Engineering-Spec.md docs/PROGRESS.md \
  package.json pnpm-lock.yaml
```

Expected: the tracked current architecture surfaces return no matches. The protected untracked historical review at `docs/CODE_REVIEW.md` remains untouched. The Python MCP server stays in place and its fixture tests pass.

- [ ] **Step 5: Verify every contract consumer and workspace gate**

```bash
cd engine
.venv/bin/python -m pytest -q \
  tests/test_api_v1.py tests/test_trove_client.py tests/test_trove_client_automation.py \
  tests/test_cli.py tests/test_mcp.py
.venv/bin/python -m pytest -q
cd ..
pnpm --filter @spool/studio exec vitest run test/api-client.test.ts
pnpm --filter @spool/api-client lint
pnpm --filter @spool/types lint
pnpm test
pnpm typecheck
pnpm build
```

- [ ] **Step 6: Commit Slice 0F**

```bash
git add contracts/v1/phase0-contract.json packages/types/src/index.ts \
  packages/api-client/src/index.ts \
  apps/studio/test/api-client.test.ts engine/routes/api_v1.py engine/trove_client.py \
  engine/cli.py engine/mcp_server.py engine/tests/test_api_v1.py \
  engine/tests/test_trove_client.py engine/tests/test_trove_client_automation.py \
  engine/tests/test_cli.py engine/tests/test_mcp.py \
  apps/studio/package.json apps/studio/next.config.ts README.md \
  docs/Spool_Engineering-Spec.md docs/PROGRESS.md pnpm-lock.yaml
git add -u packages/mcp-client
git diff --cached --check
git diff --cached --name-only
git commit -m "fix(contract): align Phase 0 clients"
```

## 13. Task 7 — Slice 0G: isolate dependency remediation

**Files:**

- Modify: `package.json`
- Modify: `pnpm-lock.yaml`

- [ ] **Step 1: Record the vulnerable dependency paths before changing them**

```bash
pnpm why -r postcss
pnpm why -r undici
pnpm audit --json > /tmp/spool-phase0-audit-before.json
```

The 2026-07-13 baseline resolves vulnerable `postcss@8.4.31` through Next and `undici@7.27.0` through jsdom. Re-read the fresh audit rather than relying on version memory.

- [ ] **Step 2: Apply only targeted root package-policy changes**

Use root `pnpm.overrides` with parent-qualified selectors so unrelated PostCSS/undici consumers do not move:

```json
{
  "pnpm": {
    "overrides": {
      "next@16.2.7>postcss": "8.5.19",
      "jsdom@29.1.1>undici": "7.28.0"
    }
  }
}
```

Before applying, confirm the fresh audit still identifies these parent/version paths and that both targets satisfy its fixed ranges. If the parent version has changed, update the selector to the exact installed parent shown by `pnpm why`; do not use a blanket `postcss` override because Vite already has an independent fixed resolution.

Do not update unrelated packages and do not mix behavior files into this commit.

```bash
pnpm install --lockfile-only
pnpm install --frozen-lockfile
pnpm why -r postcss
pnpm why -r undici
```

- [ ] **Step 3: Prove the advisories and regressions are gone**

```bash
pnpm audit --json > /tmp/spool-phase0-audit-after.json
pnpm test
pnpm typecheck
pnpm build
pnpm lint
pnpm exec prettier --check package.json pnpm-lock.yaml
git diff -- package.json pnpm-lock.yaml
```

Acceptance: the targeted PostCSS advisory is absent. Any remaining high-severity advisory blocks this slice unless it is demonstrably outside the installed/runtime graph and is documented for explicit review. The two dependency files pass their targeted Prettier check. Preserve the known unrelated `packages/ui/src/ui.tsx` package-lint baseline when interpreting `pnpm lint`.

- [ ] **Step 4: Commit Slice 0G alone**

```bash
git add package.json pnpm-lock.yaml
git diff --cached --check
git diff --cached --name-only
git commit -m "chore(deps): remediate Phase 0 advisories"
```

Expected staged names: exactly `package.json` and `pnpm-lock.yaml`.

## 14. Final Phase 0 verification and acceptance matrix

- [ ] **Step 1: Run all repository gates from the correct working directories**

```bash
(cd engine && .venv/bin/python -m pytest -q)
pnpm test
pnpm typecheck
pnpm build
pnpm lint
pnpm audit --json

PRETTIER_BASELINE="$(git rev-parse --git-path phase0-prettier-baseline.txt)"
PRETTIER_CURRENT="$(mktemp /tmp/spool-prettier-current.XXXXXX)"
PRETTIER_CHANGED="$(mktemp /tmp/spool-prettier-changed.XXXXXX)"
PRETTIER_REQUIRED="$(mktemp /tmp/spool-prettier-required.XXXXXX)"
PRETTIER_NEW_FAILURES="$(mktemp /tmp/spool-prettier-new.XXXXXX)"

pnpm exec prettier --list-different "**/*.{ts,tsx,js,jsx,json,md,css,yaml,yml}" | \
  LC_ALL=C sort >"$PRETTIER_CURRENT"
comm -13 "$PRETTIER_BASELINE" "$PRETTIER_CURRENT" >"$PRETTIER_NEW_FAILURES"
test ! -s "$PRETTIER_NEW_FAILURES"

git diff --name-only --diff-filter=ACMR 9deb66d...HEAD -- \
  '*.ts' '*.tsx' '*.js' '*.jsx' '*.json' '*.md' '*.css' '*.yaml' '*.yml' | \
  LC_ALL=C sort >"$PRETTIER_CHANGED"
comm -23 "$PRETTIER_CHANGED" "$PRETTIER_BASELINE" >"$PRETTIER_REQUIRED"
while IFS= read -r FILE; do
  test -z "$FILE" || pnpm exec prettier --check "$FILE"
done <"$PRETTIER_REQUIRED"

git diff --check 9deb66d...HEAD
```

No path absent from the frozen worktree baseline may become a new Prettier failure, and every changed eligible path that was clean at baseline must pass its targeted check. Changed legacy files already present in the baseline are not mechanically reformatted wholesale; `git diff --check` covers their edited hunks. Run `pnpm format:check` separately as an informational baseline comparison. `pnpm lint` may retain only its unrelated package-lint baseline. Report every remaining repo-wide formatting path explicitly, and do not claim either failing repo-wide command passed.

- [ ] **Step 2: Run the token-authenticated golden flow**

Repeat the complete isolated launch/readiness/Playwright/teardown harness from Section 11 Step 6; do not point this gate at pre-existing servers or user data. Capture evidence for authenticated JSON, SSE, inline/ranged playback, and rendered download.

- [ ] **Step 3: Re-run the destructive-operation byte matrix**

```bash
(cd engine && .venv/bin/python -m pytest -q tests/test_phase0_artifact_safety.py)
```

The test output must cover download, transcription, clip/render, clear-finished, TTL sweep where applicable, and restart.

- [ ] **Step 4: Check all ten master acceptance gates**

| Master gate               | Evidence required                                                                                                                                                                                    |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Managed bytes survive  | `test_phase0_artifact_safety.py` hash matrix plus restart                                                                                                                                            |
| 2. Cancel races fenced    | queued/running/stale-attempt tests for all three managers                                                                                                                                            |
| 3. Mapped IPv6 blocked    | parameterized direct and DNS safety tests                                                                                                                                                            |
| 4. Origin enforcement     | Flask counter/limiter stay zero for hostile/null; local Studio and originless authenticated direct CLI/MCP pass Flask; token proxy rejects originless mutations but allows originless media GET/HEAD |
| 5. Bounded capacity       | capacity-plus-one manager/API tests and zero hidden work                                                                                                                                             |
| 6. Token Studio works     | Playwright URL-to-clip JSON/SSE/media/download proof                                                                                                                                                 |
| 7. Product truth          | product-truth component inventory and visible errors                                                                                                                                                 |
| 8. Egress consent         | provider spy remains zero until explicit active consent                                                                                                                                              |
| 9. Contracts agree        | shared fixture passes Flask, TS, Python, CLI, and MCP                                                                                                                                                |
| 10. Agent writes disabled | enumerated in-app/MCP mutation tests with zero calls                                                                                                                                                 |

- [ ] **Step 5: Audit the final diff for scope and accidental artifacts**

```bash
git status --short
git diff main...HEAD --check
git diff main...HEAD --stat
git diff main...HEAD --name-only
git log --oneline --decorate main..HEAD
```

Reject `.next`, `.turbo`, `test-results`, Python caches, local media, environment secrets, and changes to the protected pre-existing untracked files.

- [ ] **Step 6: Request code review before merge**

Use `superpowers:requesting-code-review` with the master spec, this plan, commit list, test evidence, known lint baseline, audit result, and rollback notes. Resolve high/medium findings before presenting the branch as complete.

- [ ] **Step 7: Run verification again after review fixes**

Follow `superpowers:verification-before-completion`. Re-run every command affected by a review fix and then the complete gate in Step 1. Completion language is allowed only from this fresh output.

## 15. Rollback and execution handoff

Each behavior slice is independently revertible in reverse order. If rollback becomes necessary:

1. retain Slice 0A's non-destructive behavior even if a downstream projection regresses;
2. retain Slice 0B's stale-attempt fences unless a more restrictive equivalent replaces them;
3. retain the provider-consent and agent-mutation fuses while UI defects are repaired;
4. retain hostile-Origin rejection and admission bounds while proxy/client issues are repaired;
5. use direct authenticated loopback CLI/MCP as the recovery path if the Studio proxy is reverted;
6. revert the dependency commit separately from behavior commits.

Recommended execution mode: use `superpowers:subagent-driven-development` in this task, one fresh implementation subagent and one independent spec-review pass per task, while the primary agent owns integration, tests, and commits. Use `superpowers:executing-plans` only if implementation moves to a separate session.
