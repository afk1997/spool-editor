# Spool — build progress

> **Living tracker. Read this first to resume.** Maintained as work proceeds, mapped to
> `Spool_Engineering-Spec.md` (§5 roadmap, §6 front-end standards). Status legend:
> ✅ done & verified · 🟡 in progress · ◻️ not started.
>
> **Last updated:** 2026-06-02 · **Phase 0 — ✅ COMPLETE.** Phase 1 **backend done & green**
> (engine chain → `api_v1` clip surface → MCP tools + elicitation + `spool://`, CLI parity).
> **Studio UI in progress:** design tokens + typed client/SSE + app shell + S0/S1/S2/S3/S10
> wired & verified. Next: the editor screens (S4/S5/S7/S8/S11) + Agent panel + ⌘K + e2e.

## Roadmap at a glance

```mermaid
graph TD
    subgraph P0["Phase 0 — Foundation: engine headless"]
      B0["Monorepo bootstrap (pnpm+turbo, CI, Apache-2.0)"]:::done
      B1["Fold trove into engine/ (flat, byte-identical)"]:::done
      B2["Studio app + packages scaffold, types, wired home"]:::done
      B3["Green test baseline — 591 tests, local uv venv (py3.12)"]:::done
      B5["Dependency-doctor endpoint (machine probe + tool checks)"]:::done
      B4["Strip htmx UI (routes/templates/editor) — done, suite green (467)"]:::done
      B6["Docker headless + root compose — verified (builds + serves)"]:::done
    end
    subgraph P1["Phase 1 — Core clip loop + own UI (MVP)"]
      C1["Engine: moments · cutter · reframe(diar plus ROI) · captioner · exporter"]:::done
      C2["MCP: clip tools + elicitation + spool:// resources"]:::done
      C3["UI: S0-S5, S7 basic, S8 presets, S10, S11 + Agent panel + Cmd-K"]:::wip
      C4["Port demo design tokens into the Tailwind theme; component library"]:::done
      C5["e2e: URL to 9:16 clip (Playwright)"]:::todo
    end
    P2["Phase 2 — Studios + editor (timeline, ROI editor, caption studio, brand kits, SQLite FTS5)"]:::todo
    P3["Phase 3 — Discovery + automation (glass-box ranking, watch-folder, recipes)"]:::todo
    P4["Phase 4 — Publish + analyze"]:::todo

    P0 --> P1 --> P2 --> P3 --> P4

    classDef done fill:#bbf7d0,stroke:#16a34a,color:#052e16;
    classDef wip  fill:#fde68a,stroke:#d97706,color:#451a03;
    classDef todo fill:#e5e7eb,stroke:#9ca3af,color:#374151;
```

## Phase 0 — Foundation (current)

- [x] **Monorepo bootstrap** — pnpm + turbo workspace, strict TS, Prettier, GitHub Actions CI, Apache-2.0. `pnpm install`+`typecheck`+`build` green. *(commit `58f5763`)*
- [x] **Fold trove → `engine/`** — flat layout intact, verified **byte-identical** to validated upstream; `engine/clip/` Phase-1 scaffolds; clipify back-half vendored + renamed + MIT-credited (`THIRD_PARTY_LICENSES.md`).
- [x] **Studio + packages** — real Next.js 16 app; `@spool/types` (full data model, spec §3), `api-client`, `mcp-client`, `ui`; home screen wired to the engine. *(fix `d788686`: health() shape)*
- [x] **whisper.cpp standard** — already the only transcription dep (`pywhispercpp`); no `openai-whisper` to remove.
- [x] **Progress stream** — already exists: `GET /api/v1/events` (SSE, jobs + transcripts snapshots).
- [x] **Green test baseline** — **591 tests pass** (whole trove suite) on Python 3.12 via a local **uv venv**. Docker got corrupted by a full disk and was reset; the dev/test loop is now the local venv (seconds per run, vs Docker's 14-min rebuilds). Docker packaging is deferred to B6.
- [x] **Dependency-doctor endpoint** — `GET /api/v1/doctor` (unauthenticated): `machine.probe()` + ffmpeg / yt-dlp / whisper.cpp / Python presence & versions + available ffmpeg encoders. Test + OpenAPI contract entry; verified in the 591-test run.
- [x] **Strip the htmx UI** — removed `templates/`, `static/`, `styles/`, `tailwind.config.js`, `routes/transcript_editor.py`, the inline HTML/`*-card`/transcribe-setup/transcript-view routes in `app.py`, `_card_view`, the htmx jinja globals + editor `txn_locks`, and 7 obsolete htmx test files. Preserved the `app.extensions["trove.actions"]` helpers `api_v1` needs; trimmed now-dead imports. **Migrated** `test_transcribe_pipeline.py` to drive the real pipeline via `POST /api/v1/jobs/<id>/transcribe` (was the removed HTML route) so coverage isn't lost. CSP coverage confirmed in `test_safety.py`. Also fixed a pre-existing trove test race (`test_cancel_from_paused_removes_partial_files`) the suite reordering exposed. **Suite green: 467 passed, exit 0.**
- [x] **De-couple Docker** — `engine/Dockerfile` rewritten **multi-stage** (builder compiles webrtcvad's C ext with build-essential + python3-dev; lean runtime carries ffmpeg + libgomp1 + libsndfile1, copies the installed prefix); `trove.sh` headless (drops Tailwind; installs `requirements.txt`); root `docker-compose.yml` (host-bind + token + `PYTHONUNBUFFERED`, persisted models volume, host-owned downloads) + `engine/.dockerignore`. **Verified:** `docker compose up` builds the image and serves `/api/v1/health` → `{ok, v1}` both inside the container and from the host (`0.0.0.0:8899`). The container needs ~5–15 s to load whisper.cpp on first start; the cold build is a one-time ~14 min torch install (layer-cached after).

**Phase 0 done-when:** from a clean checkout, the engine runs headless → POST a URL to `api_v1` → file downloads with live progress → transcribe yields `words.json` + `.srt`; the same flow works from Claude Desktop via the MCP server; no htmx anywhere.

## Phase 1 — Core clip loop + UI (in progress)

Engine clip modules first (TDD, matching trove's ffmpeg/job conventions), then `api_v1`
clip endpoints + clip/render job types, then MCP clip tools + elicitation, then the
studio screens wired to `api_v1` with the demo's design tokens ported in.

- [x] **`cutter`** — lossless `ffmpeg -c copy` trim (input-seek + duration), cancel/
  error-handled like `transcriber.extract_audio`. 7 tests, green.
- [x] **`captioner`** — slices `words.json` to the clip window (re-based to 0) → styled
  ASS (opus/karaoke/minimal) via the vendored `ass_captions`; `burn` rasterizes via
  ffmpeg's subtitles filter. Shared ffmpeg plumbing extracted to `clip/_ffmpeg.py`
  (cutter refactored onto it; reframe/exporter will reuse it). 6 tests.
- [x] **`reframe`** — `detect_faces` (sample frame + default L/R ROIs) + `speaker_track`
  (per-ROI ffmpeg motion → vendored `roi_motion` → **diar⊕ROI fusion**, still/off-mic
  speakers resolved by audio turns) + `render` (pan via vendored `pan_expr`, split, center;
  9:16/16:9/1:1/4:5). 15 tests.
- [x] **`moments`** — LLM moment-finding over `words.json` via a **pluggable provider
  layer** (`clip/llm.py`): DEFAULT = **codex bridge** (`codex exec`, read-only sandbox,
  prompt piped over stdin — the user's ChatGPT/Codex subscription, no key/GPU), plus a
  `CallableProvider` for the injected **agent** LLM and room for Claude/local. Prompt reuses
  **clipify's Step-1 heuristics** (punchlines/reversals/awkward pauses/quotable one-liners/
  audio peaks), mode-tuned (funny/insightful/hot-take/story/how-to/q&a). Tolerant JSON parse
  (bare/fenced/prose), range clamp-to-duration, `transcript_window`, glass-box-ready
  `signals`. Only transcript text egresses; **`SPOOL_OFFLINE=1` disables the bridge**.
  16 (`llm`) + 15 (`moments`) tests, provider mocked. **(Supersedes spec §10 #2's Ollama default.)**
- [x] **`exporter`** — platform presets (tiktok/reels/shorts/linkedin/x/youtube) →
  codec/bitrate/fps + -14 LUFS loudnorm, hardware encoder (VideoToolbox/NVENC/x264),
  fast-vs-quality. Brand kits deferred to P2. 9 tests.
- [x] **`api_v1` clip endpoints + clip/render job types** — `ClipJobManager`
  (mirrors `transcribe_jobs.py`; one `kind` per op + `params`/`result`) + `clip_runner.py`
  (orchestration: clip tree, per-clip `meta.json` Clip record, diar⊕ROI prep, artifact
  chaining, one-shot pipeline, cancel/progress). Wired into `app.py` (extensions +
  TTL-sweeper pin). Endpoints: `POST /sources/<id>/{moments,cut,render}`,
  `POST /clips/<id>/{reframe,captions,renders}`, `GET /clip-jobs[?kind,status]` +
  get/cancel/dismiss, `GET /clips/<id>/renders/<rid>/file`. Plus `/capabilities` +
  SSE snapshot + OpenAPI + `TroveClient` methods. 13+10+13+23+13 tests.
- [x] **MCP clip tools** + elicitation + `spool://` resources — extended trove's FastMCP
  server (`mcp_server.py`) with `find_moments`/`cut_clip`/`reframe_clip`/`caption_clip`/
  `render_clip`/`render_pipeline`/`list`/`get`/`cancel`/`dismiss_clip_job` (delegate to the
  client → same API → same engine). `reframe_clip` **elicits** `{aspect,mode}` when omitted
  (graceful fallback). Resources `spool://clips` + `spool://clips/{job_id}`. CLI⇄MCP parity
  kept (`cli.py` clip subcommands + `MCP_TO_CLI`). 2 MCP tests (incl. a real elicitation
  round-trip) + parity test.
- [~] 🟡 **Studio screens** — **foundation + core loop done & verified:** demo design
  tokens → one Tailwind v4 `@theme` layer (+ data-accent/density, 4 display fonts, AA
  fix); `@spool/types` reconciled to the real api_v1 wire shapes; `@spool/api-client`
  fleshed out to the full surface + `subscribeEvents` SSE; a live-data layer (one SSE
  subscription → context, `useEngine`/`useLive`/`useEngineQuery`); the app shell (rail +
  top bar + live status/queue bar); UI primitives; and **S0** Dependency-Doctor + **S1**
  Home + **S2** Import + **S3** Library + **S10** Render Queue — all wired to `api_v1`,
  zero mock. `pnpm typecheck` 9/9, studio build (5 routes) + lint green.
  **Remaining:** S4 Transcript, S5 Clip Discovery (candidate cards + glass-box score),
  S7 Reframe (basic), S8 Caption Studio (presets), S11 Clips Library; the **Agent panel**
  (`@spool/mcp-client`) + **⌘K**; promote primitives to `@spool/ui`; Playwright e2e (URL→9:16).

## What's verified now

- `pnpm install` → 6 workspace projects, 354 packages, clean.
- `pnpm typecheck` → 9/9 tasks pass. `pnpm build` → Next.js 16 compiles, static pages generate.
- `engine/` `.py` files diff byte-identical against the validated trove clone.
- **engine: 595 tests pass** (exit 0) on Python 3.12 via uv venv — headless trove suite + `clip.cutter` (7) + `clip.captioner` (6) + `clip.reframe` (15) + `clip.exporter` (9) + `clip.llm` (16) + `clip.moments` (15) + `clip_jobs` (13) + `clip_runner` (10) + `api_v1` clips (23) + `trove_client` clips (13) + MCP clip tools/elicitation (e2e). The whole agent + API + CLI clip surface is wired to one engine + one queue.
- **studio:** `pnpm typecheck` 9/9, `pnpm --filter @spool/studio build` compiles 5 routes (/, /import, /library, /queue, /_not-found) and prerenders, ESLint clean. Tokens/types/client/shell + S0/S1/S2/S3/S10 wired to `api_v1` (no mock data).
- **`docker compose up`** builds the multi-stage image and serves `/api/v1/health` from the host — the packaged engine works end to end.
- **headless serving** (venv): `/api/v1/doctor` reports real tooling — ffmpeg 7.1.1, whisper.cpp 1.5.0, yt-dlp 2026.3.17, VideoToolbox encoders.

## How to resume (cold start)

```bash
# JS workspace
pnpm install
pnpm dev            # studio → http://localhost:3000 (shows engine status)
pnpm typecheck && pnpm build

# engine (Python 3.11+; local default is 3.9 → use the uv venv)
cd engine
uv venv --python 3.12               # if .venv is missing (uv auto-fetches 3.12)
uv pip install -r requirements.txt  # heavy once: torch, pywhispercpp, yt-dlp@master, …
.venv/bin/pytest -q                 # full suite, ~60s
```

Spec: `docs/Spool_Engineering-Spec.md` (§5 phases, §6 front-end). Visual source of truth: `docs/Spool (standalone) (1).html`.

## Locked decisions

- License **Apache-2.0**; diarization kept in **core install** (heavier base, no missing-dep step).
- **Moment-finding LLM = pluggable provider, default "codex bridge"** — the user's
  ChatGPT/Codex subscription via the Codex CLI (no API key, no local GPU). Ditches the
  spec's local-Ollama default (§10 #2). Local-first preserved: only transcript text is
  sent (media never leaves the machine), agent mode uses the agent's own LLM, and
  offline-mode disables the bridge. Pluggable so a Claude/local provider can be added.
  Implemented in `clip/llm.py`. **New Spool config uses the `SPOOL_*` env namespace**
  (not `TROVE_*`): `SPOOL_LLM_PROVIDER` (default `codex`), `SPOOL_CODEX_BIN/MODEL/TIMEOUT`,
  and the engine-wide offline switch `SPOOL_OFFLINE=1`.
- Engine = flat fold-in of trove (reuse, don't rebuild); htmx stripped in Phase 0, not at bootstrap.
- **Dev/test loop = local uv venv (Python 3.12)**, not Docker. Docker is reserved for packaging (B6) and was reset after a full-disk corruption.
- Internal TS packages export raw source; Next `transpilePackages` compiles them.
- TS types are currently idealized (camelCase); **Phase-1 wiring reconciles them with the real `api_v1` shape** (snake_case; `/api/v1/openapi.json` can seed generation).

## Open / deferred

- Docker was reset (empty image) after the disk filled; B6 (Dockerfile de-coupling + compose) needs Docker Desktop working again — verify a clean `docker build engine/` then.
- `Spool_Design-Brief.md` and `Spool_Design-Review.md` are referenced by the spec but **absent**; proceeding from the demo + §6.6 carried review items.
- Reclip MIT attribution: confirm before launch whether any reclip source was copied into trove (spec §7.3).
