# Spool — Engineering Spec (for Claude Code)

**What this is.** Build **Spool**, a local-first, open-source clip studio that turns long videos into platform-ready vertical clips, running entirely on the user's machine. Spool is assembled from two existing codebases plus a new studio UI — **reuse, don't rebuild.**

- **`trove` — first-party foundation** (Kaivan's own; built with Claude Code). Provides the whole front half + infrastructure: yt-dlp **downloader**, **whisper.cpp transcription**, **speaker diarization**, a **job system**, security/safe-bind, packaging, and a working **stdio MCP server**. Adopt freely; **no attribution**; may merge into the Spool repo.
- **base clip engine — upstream** (MIT; **README credit only**, name kept out of the product). Provides the back half: clip-moment finding, the no-ML **speaker-following pan** (`analyze.py` + `build_pan.py`), **ASS captions** (`build_ass.py`), audio alignment (`audio_align.py`).
- **Spool studio — new** (this project): Spool's **own Next.js UI** + the glue that turns the two engines into one agentic product. The **approved visual design is `Spool (standalone) (1).html`** (reviewed in `Spool_Design-Review.md`) — treat it as the **visual source of truth**, to be **rebuilt as a production app with all dummy content stripped and every screen wired to the engine** (see §6).

**Stack (decided).** Backend: keep trove's **Python 3.12 + Flask** headless JSON API (`routes/api_v1.py`). Persistence: trove's **atomic JSON job store** now → **SQLite (FTS5)** in Phase 2. Transcription: **whisper.cpp** (`pywhispercpp`) — drop `openai-whisper`. MCP: **extend trove's FastMCP stdio server**. UI: **Next.js + Tailwind + shadcn** (own), over HTTP + a progress stream (SSE/WS). Packaging: trove's Docker + script (docker-first; Tauri later). Diarization (PyTorch via resemblyzer, ~1.3 GB) stays **opt-in**.

**Golden target.** The UI and the MCP server are **two clients of the same JSON API → same engine → same job store → same files on disk.** Phase 0 already shares read state, but deliberately blocks agent mutations; manual/agent mutation parity starts only after the Phase 4 approval and undo contract ships.

**Read with:** `Spool_00_Product-Overview.md` (vision, features, risks), `Spool_Design-Brief.md` (UI direction), `Spool_Design-Review.md` (review of the approved demo), and the demo itself, **`Spool (standalone) (1).html`**. **The build order is the phased roadmap in §5; the front-end build + engineering standards are in §6.**

---

## 1. What we build on

### 1.0 Source repositories & bootstrap (clone these first)

| Repo | URL | Role | Branding |
|---|---|---|---|
| **trove** (first-party) | `https://github.com/afk1997/trove` | The backend foundation — clone and fold into the Spool monorepo (or vendor as a package). | Kaivan's own; adopt freely, no attribution. |
| **base clip engine** | `https://github.com/louisedesadeleer/clipify` | Take the 4 back-half scripts (`analyze.py`, `build_pan.py`, `build_ass.py`, `audio_align.py`) into the Spool engine. | **Build-time source only.** Rename to neutral module names (`reframe`/`captioner`/…); keep the upstream name out of shipped code, UI, and user-facing strings; **credit in the README only.** |
| reclip (trove's lineage) | `https://github.com/averygan/reclip` | Reference only — trove is "inspired by" it (MIT). | If any reclip source was copied into trove, preserve its MIT notice in the README. |

**Prerequisites** (the Phase-0 dependency-doctor verifies/installs these): `ffmpeg` (+`libx264`, hardware encoders), `yt-dlp` (pin to **master** — YouTube breaks on stable), **Python 3.12** + `pywhispercpp` (whisper.cpp) + `numpy`; for the studio, **Node 20+ and pnpm**. Diarization extras (`resemblyzer`, `silero-vad`, `scikit-learn`; ~1.3 GB PyTorch) are **opt-in**.

**First steps:** clone **trove** → run it headless (`trove.sh` / Docker) → confirm `api_v1` + the MCP server work (Phase 0) → pull the 4 clip scripts from the **base clip engine** into the Spool engine (renamed) → build the studio from `Spool (standalone) (1).html` wired to `api_v1` (Phase 1; §5/§6). Everything needed to start is in this doc — no external context required.

### 1.1 `trove` — the first-party foundation (keep / drop / extend)

| Action | trove parts | Notes |
|---|---|---|
| **Keep as-is** | `runner.py` (yt-dlp), `jobs.py` + `jobs_store.py` (queue + atomic persistence), `transcriber.py` (whisper.cpp), `transcribe_jobs.py`, `diarizer.py`, `models_store.py`, `machine.py`, `config.py` (safe-bind), `safety.py`, `transcript_io.py`, `routes/api_v1.py`, `trove_client.py`, `mcp_server.py`, `Dockerfile`/`trove.sh`/`pyproject.toml`, `tests/` | The headless engine + JSON API + MCP + job system. The foundation. |
| **Drop** | `templates/`, `static/`, `styles/`, `tailwind.config.js`, `routes/transcript_editor.py` + htmx partials | Replaced by Spool's own Next.js UI. **Keep the transcript editor's *behavior* as the reference spec** for the React editor (autosave, word-edit, find/replace, bookmarks, exports). |
| **Extend** | `mcp_server.py` (+clip tools), `jobs.py` (+clip/render job types, or a `ClipJobManager` mirroring `transcribe_jobs.py`), `api_v1.py` (+clip endpoints), `trove_client.py` (+clip methods), `safety.py` scopes, `server_capabilities` | Mirror trove's existing, tested patterns — don't invent new ones. |

trove's job model is rich already (`id, url, title, status, thumbnail, file_path, downloaded/total_bytes, speed, eta, fragment_index/count, format_choice, format_id, auto_transcribe`), with pause/resume/cancel, TTL sweep, restart-downgrade, and a worker pool. Spool's render queue is this same machinery with new job types.

### 1.2 Base clip engine — the upstream back half (README credit only)

Four small Python scripts wrapping `ffmpeg`, plus the technique that makes them special:

- `analyze.py` — two ROI motion-energy files → smoothed, hysteresis speaker timeline (`segments.json`).
- `build_pan.py` — segments → an `ffmpeg` hard-cut crop-x expression that pans a vertical strip to the active speaker.
- `build_ass.py` — whisper word timestamps → styled ASS captions (opus / karaoke / minimal).
- `audio_align.py` — FFT cross-correlation to locate a sub-clip in a longer source (de-dupe / find no-subs master).

The defensible bit: **no face-detection model.** The camera is static within a clip, so motion-differencing two eyeballed face rectangles is enough to know who's talking. Cheap, offline, fast.

### 1.3 The combined pipeline + the diarization⊕ROI win

```
URL/file ─[trove download job]→ 16k WAV → whisper.cpp words → VAD realign → (opt) diarize
        → transcript (words.json + .srt/.vtt)
        ─[Spool clip back-half]→ find moments (over words.json) → cut → REFRAME → captions → export
```

**REFRAME is the upgrade.** Build the speaker timeline by **fusing** trove's *audio* diarization (who's speaking, when) with the base engine's *video* ROI motion (where each face is on screen), then drive `build_pan.py`. Audio + video is far more robust than either alone — the base engine's pan is video-motion-only and fails on still speakers or off-mic hand motion; the audio turn resolves those. Two more free wins: **VAD-tightened word starts** → cleaner karaoke captions; and the **editable transcript** lets users fix recognition errors *before* captions are burned (kills the "~70% of clips need manual cleanup" problem). Caption timing needs **no re-transcribe** — slice `words.json` to the clip's range.

---

## 2. The agentic model

The target product has two ways to drive one engine (Overview §3):

- **Agent mode** — the LLM calls MCP tools and pauses for **elicitation** at human-judgment points (which candidates? 9:16/16:9/1:1? pan vs split? confirm ROI? caption style?). Rendered as inline cards in the UI *or* answered in chat.
- **Manual mode** — the user clicks through the studio; the engine runs deterministically.

Both ultimately use the **same engine functions over the same API**. The MCP layer is trove's Python `mcp_server.py` **extended** with the clip tools (§4) — never a parallel implementation. During Phase 0, external MCP is read-only and every mutation returns `agent_mutation_disabled`; mutation parity begins only after the Phase 4 approval and undo contract ships.

---

## 3. System architecture

```
┌──────────────── user's machine ─────────────────────────────────────────────────┐
│   Spool studio (OWN, Next.js)                MCP client (Claude Desktop/Code/…)    │
│        │  HTTP + SSE/WS (progress)                 │  stdio                        │
│        ▼                                           ▼                               │
│   ┌──────────────── trove JSON API (Flask, headless) — routes/api_v1.py ────────┐ │
│   └──────────────┬──────────────────────────────────────────────┬──────────────┘ │
│        ┌─────────▼──────────┐                          ┌──────────▼─────────┐      │
│        │ trove engine (kept)│   words.json /            │ clip engine (new)  │      │
│        │ runner·transcriber │   speaker turns ───────▶  │ moments·cutter     │      │
│        │ diarizer·models    │                          │ reframe(pan+diar)  │      │
│        │ jobs+jobs_store    │◀── shared job system + files on disk ──────────┘      │
│        │ config·safety      │                          captioner·exporter          │
│        └────────────────────┘     mcp_server.py (extended: trove + clip tools)     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**Engine package.** Clean functions callable by the API, the workers, and the MCP server: trove's `download/transcribe/diarize/...` plus new `find_moments / cut / detect_faces / speaker_track (diar⊕ROI) / reframe_render / caption_generate / caption_burn / export`. No business logic in the UI or the MCP adapter.

**Job queue & state.** trove's `JobManager` (+ a `ClipJobManager` for clip/render jobs): `queued → running → (paused-for-elicitation) → done | failed | cancelled`, with progress %, logs, TTL sweep, and the restart-downgrade rule. The UI render-queue, MCP progress, and the agent's status updates all read this one job model.

**Data model.** Reuse trove's `Job` + `TranscribeJob` records; add **Candidate** (sourceId, start, end, title, rationale, mode, signals, score), **Clip** (sourceId, candidateId?, start, end), **SpeakerTrack** (clipId, segments, roiL, roiR, source=diar/roi/fused), **Caption** (clipId, style, assPath, overrides), **Render** (clipId, aspect, mode, captionId?, brandKitId?, preset, outputPath, version), **BrandKit**, **Recipe**, **PublishPost** (P4). JSON store now; migrate to **SQLite (FTS5)** in Phase 2 for library-wide transcript search.

**On-disk layout.** Extend trove's per-job folders with a clips/renders tree: `…/sources/{id}/{source.mp4,audio.wav,transcript words.json,thumb}` and `…/clips/{id}/{clip.mp4, probe/verify.jpg, L/R.txt, segments.json, captions.ass, renders/{id}.mp4}`. Everything is a plain file the user owns.

**Config & security (trove, kept).** `config.assert_safe_bind` (refuses public bind without a token), `TROVE_TOKEN` bearer auth, `safety.py` signed-scope URLs, rate limiting, localhost-by-default. Secrets (publish OAuth, optional hosted-LLM key) in the OS keychain. yt-dlp argv is array-built (no shell injection); cookies-from-browser stays an explicit, default-off setting. An **offline-mode** switch hard-disables all egress except the active download.

**Packaging.** trove's `Dockerfile` + `trove.sh` (docker-first), `HOST/PORT/TROVE_*` env. yt-dlp pinned to **master** (YouTube breaks on stable) + an in-app updater. Hardware-aware encoders (VideoToolbox/NVENC/QSV/VAAPI/x264) with a "fast vs quality" preset switch.

---

## 4. Local MCP surface (extends trove's `mcp_server.py`)

**Keep (trove, already built):** `download_media`, `bulk_download`, `get_job`/`list_jobs`, `pause`/`resume`/`cancel`/`dismiss_download`, `transcribe`, `get_transcript`(`_chunk`/`_status`), `search_transcripts`, `list`/`install`/`set_active`/`remove_model`, `storage_info`, `server_capabilities`, and the `trove://` resources. Note the LLM-friendly touches to copy: paginated `get_transcript_chunk` (context-budget aware) and pre-formatted `human.summary` progress strings.

**Add (clip back-half):** `discover.find_moments` (mode: funny/insightful/…), `discover.rank` (P3), `clip.cut`, `reframe.detect_faces`, `reframe.speaker_track` (**diar⊕ROI**), `reframe.render` (pan/split/center; 9:16/16:9/1:1), `caption.generate` / `caption.burn`, `render.export` (platform preset + brand kit), `render.pipeline` (one-shot: ingest→…→export, pausing only at open decisions). **Elicitation** for pick-candidates / aspect / pan-vs-split / ROI-confirm / caption-style. Add resources `spool://clips/{id}`, `…/candidates`, `…/renders`. Transport stays **stdio**; the same server can run over Streamable HTTP for remote control if ever needed.

---

## 5. Engineering roadmap (the build plan, in phases)

Each phase is independently shippable. Format: **Goal · Reuse (already built) · Build · Done-when.**

### Phase 0 — Foundation: trove headless *(mostly done)*
- **Goal:** stand trove up as Spool's headless backend; strip its UI; confirm the API + MCP drive the front half.
- **Reuse:** the entire `Keep` column of §1.1 — downloader, jobs, transcription, diarization, models, security, MCP, Docker, tests.
- **Build:** remove `templates/static/styles/transcript_editor` (htmx); confirm `api_v1` exposes everything the UI needs (jobs, transcripts, models, storage, capabilities) + a progress stream (SSE/WS); fold trove into the Spool monorepo (or as a package dep); standardize on **whisper.cpp** (remove any `openai-whisper`); a **dependency-doctor** endpoint (`machine.probe` + ffmpeg/yt-dlp/whisper.cpp checks).
- **Done-when:** from a clean checkout, `docker compose up` → POST a URL to `api_v1` → file downloads with live progress → transcribe yields `words.json` + `.srt`; Claude Desktop can inspect that same state through the Python MCP server, while MCP mutations fail closed with the Phase 0 envelope and make zero underlying calls; no htmx anywhere.

### Phase 1 — Core clip loop + own UI (MVP)
- **Goal:** the end-to-end "paste → clip," in Spool's own UI **and** via the agent. This is the demo.
- **Reuse:** trove ingest/transcribe/jobs/MCP/diarization; base engine `analyze.py`/`build_pan.py`/`build_ass.py`.
- **Build — engine:** new modules `moments.py` (LLM moment-finding over `words.json`), `cutter.py` (`ffmpeg -c copy` trim), `reframe.py` (detect ROIs on a sample frame; build the **diar⊕ROI** speaker timeline; wrap `build_pan.py`; pan/split/center), `captioner.py` (`build_ass.py` fed from `words.json` sliced to the clip; opus/karaoke/minimal), `exporter.py` (final mp4 + platform preset); register clip/render job types.
- **Build — MCP:** add the clip tools + elicitation + `spool://` resources (§4).
- **Build — UI (own Next.js; rebuilt from the approved demo `Spool (standalone) (1).html`):** the Phase-1 screens — S0 Onboarding/Dependency-Doctor, S1 Home, **S2 Import/Downloader**, S3 Library, S4 Project/Transcript, S5 Clip Discovery, S7 Reframe (basic), S8 Caption Studio (presets), S10 Render Queue, S11 Clips Library — plus the global **Agent panel**, **⌘K**, and the **status/queue bar**. **Strip every bit of the demo's dummy data and wire each screen to `api_v1` + the progress stream; the Agent panel uses REST `/api/v1/agent`, while external MCP clients use the Python stdio server** (§6). Follow the front-end standards in §6 (componentized, typed clients, no in-browser Babel, perf budget).
- **Done-when:** paste a YouTube URL → download → transcribe → agent proposes candidates → user picks (card *or* chat) → a 2-person 16:9 moment renders to **9:16 with a working diar⊕ROI speaker-pan + opus captions** → lands in the Clips library as a real `.mp4`; achievable **both** via the UI **and** via one agent sentence; works **offline** after the download.

### Phase 2 — Studios + editor
- **Goal:** editor-grade control + a repeatable brand look.
- **Reuse:** trove's transcript editor *behavior* as the reference; the clip engine from P1.
- **Build:** timeline editor (S6) with snap-to-word/sentence/scene; full visual **ROI + speaker-track editor** (S7) — draggable boxes, live motion preview, editable diar⊕ROI timeline, min-dwell/margin controls; full **Caption Studio** (S8) — live styling + match-from-image; **Brand Kits** (S9); **transcript-based editing** (delete words → cut video) adapted from trove's editor into React. **Infra:** migrate job store → **SQLite (FTS5)** for library-wide transcript search and many-project queries.
- **Done-when:** fix an ROI box and a caption style by hand and re-render; apply a brand kit across clips; cut a clip by editing its transcript; full-text search transcripts across the library.

### Phase 3 — Discovery + automation
- **Goal:** find the best moments at scale, hands-off.
- **Build:** a **glass-box** ranking/opportunity score (named, reweightable factors — *not* an opaque 0–99); content-type modes (funny/insightful/hot-take/story/how-to/Q&A); hook analysis; auto title/description/hashtags; **batch render**; **watch-folder + channel/playlist automation** (drop a video → auto clips → "for review" queue); recipes/templates; B-roll & emoji; de-dupe via `audio_align.py`; multi-language.
- **Done-when:** point at a folder/channel → new videos auto-produce ranked clips per a recipe into a review queue; ranking factors are visible and reweightable.

### Phase 4 — Publish + analyze
- **Goal:** close the create→publish→learn loop.
- **Build:** publish/schedule to TikTok/Reels/Shorts/LinkedIn/X (OAuth tokens in the OS keychain); content calendar; per-platform caption/hashtags; analytics (views/retention/likes); feed performance back into ranking weights. Optional light multi-seat/agency.
- **Done-when:** schedule a clip from the calendar, see its performance, and watch the ranking adapt.

**Cross-phase invariants:** keep yt-dlp on master + updater; diarization opt-in (one-click enable); UI and MCP read the same state; everything inspectable, undoable, offline-capable. Mutation parity is gated on the Phase 4 approval and undo contract and fails closed until then.

---

## 6. Front-end build & engineering standards

> The approved visual design is **`Spool (standalone) (1).html`** (the Claude Design demo, reviewed in `Spool_Design-Review.md`). It is the **visual source of truth** — match its layout, type, palette, components, and screen set. It is **not** the artifact we ship.

### 6.1 Demo → production (do **not** ship the demo)
The demo is a single ~1.6 MB HTML file that compiles React with the **in-browser Babel transformer** (the console warns about it) behind an "Unpacking…" loader. Fine for a mockup, wrong for the product. Rebuild it properly:
- **Next.js + TypeScript + Tailwind + shadcn/ui**, precompiled — no in-browser Babel, no monolithic single file.
- Port the demo's **design tokens** (`--bg/--text/--accent/--roi-*/--caption-hl/--radius/--font-*`, plus the `data-density` / `data-accent` hooks) into **one theme layer** (Tailwind theme + CSS variables) — a single source of truth for color/space/radius/type.
- Rebuild each screen as **composed components**, not one file.

### 6.2 Strip ALL dummy content & wire everything up *(core directive)*
The demo is full of hard-coded sample data (fake projects like "Ep.42 — Why Local-First…", fake clips/jobs, scores 92/88, "talking-head/interview" cards). **Remove all of it.** Nothing ships with mock data baked in.
- Every screen pulls **live data** from trove's **`api_v1`**. External MCP clients read the same engine through the Python FastMCP server. No placeholder arrays, no lorem, no `setTimeout` fake progress.
- Every enabled manual control invokes a **real endpoint**. Phase 0 agent mutations remain explicitly disabled until the approval and undo contract exists.
- **Loading / empty / error / progress** states are driven by **real** job + request state (the SSE/WS progress stream), not fakery.
- Wire the Studio **Agent panel** to REST `/api/v1/agent`; in Phase 0 it renders the structured `agent_mutation_disabled` response. A later gated mutation phase adds approval and inline elicitation.

**Screen → wiring map**

| Screen | Wired to |
|---|---|
| Import / Downloader | `ingest.download` / `import_file`; downloads list ← `jobs.*` + progress stream |
| Library (sources) | `media.list_sources` |
| Project / Transcript | `media.get_transcript`, `analyze.transcribe` |
| Clip Discovery | `discover.find_moments` / `discover.rank` (+ elicitation) |
| Reframe / ROI editor | `reframe.detect_faces` / `speaker_track` (diar⊕ROI) / `render` |
| Caption Studio | `caption.generate` / `caption.burn` |
| Render Queue | `jobs.*` + progress stream |
| Clips Library | `library.query` |
| Settings | `models.*`, `server_capabilities`, config |
| Agent panel | REST `/api/v1/agent` (Phase 0 mutation-disabled; gated execution + elicitation are future work) |

### 6.3 Modularization & architecture
- **Monorepo** (pnpm + turbo): `apps/studio` (Next.js), `packages/ui` (design system), `packages/api-client` (typed REST client mirroring `trove_client`), and `packages/types` (shared TS types ↔ the engine's data model in §3), alongside the Python `engine`. Agent integrations use the working FastMCP stdio server in `engine/mcp_server.py`, which delegates through `trove_client.py` to the same JSON API; there is no TypeScript MCP transport package.
- **Typed clients** — keep TS types in sync with the API schema; one client module, no `fetch` scattered through components.
- **Component library** — the recurring pieces from the Design Brief as isolated, prop-typed, documented components: MediaCard, CandidateCard + ScoreBar, AspectToggle, ReframeModeToggle, VideoPreview, Timeline, ROIEditor, CaptionStyler, TranscriptView, JobRow/QueueDrawer, AgentPanel, ElicitationCard, CommandPalette, DependencyDoctor, EmptyState/Skeleton/ErrorState.
- **State/data** — server state via React Query/SWR (caching, retries, the progress stream); local UI state via hooks/Zustand. **Never `useState` for continuous drag/scrub** (ROI boxes, timeline playhead) — use refs/motion values to avoid re-render storms.
- **Routing** — one route per screen; Editor/ROI/Timeline are **lazy-loaded** leaves.

### 6.4 Performance budget
- Route-level **code splitting**; lazy-load the heavy Editor/ROI/Timeline; small initial bundle (the demo's 1.6 MB monolith is the anti-pattern).
- **Virtualize** long lists (Library, Clips, Jobs, Transcript words).
- Animate only `transform`/`opacity`; honor `prefers-reduced-motion`; 50–300 ms.
- **Preload display fonts** (no FOUT / "Unpacking…" flash); lazy images with width/height; preview media via low-res proxies.
- Targets: **LCP < 2 s, CLS < 0.1**, 60 fps timeline scrub.

### 6.5 Quality bar (best practices)
- **TypeScript strict**, ESLint + Prettier, pre-commit hooks, **CI** (typecheck / lint / test / build).
- **Accessibility** (from the review): fix `--text-faint` contrast to AA, visible focus rings, 44px targets, tabular-nums, keyboard nav, ARIA on custom widgets (timeline / ROI).
- **Error boundaries** per route; typed error states; never a blank screen on failure.
- **Tests** — unit (engine functions + components), integration (API + MCP tools), one **e2e** on the core "URL → 9:16 clip" flow (Playwright). Mirror trove's existing `tests/` discipline.
- **Security** — no secrets in the client; tokens via the engine/OS keychain; localhost-bound by default; respect offline-mode.
- **Feature-flag** unfinished phases. **Light/paper only — no dark mode** (an optional charcoal media-stage behind the preview is allowed).

### 6.6 Carried design-review items (see `Spool_Design-Review.md`)
- ✅ **Import** rebuilt with real states (fixed in this demo).
- ✅ **Agent panel** now contextual/collapsible (improved).
- ⏳ **Glass-box score** — confirm the candidate score expands to named, reweightable factors, not a bare number.
- ⏳ **Contrast/a11y pass** — `--text-faint` fails AA; promote meaningful text to `--text-dim`.
- ⏳ **Onboarding copy** — trim the marketing paragraph to one line + the dependency check.
- ⏳ **Rail labels** — add labels under the icon rail (or an expand toggle).

### 6.7 Phasing the front end
- **Phase 1:** production rebuild of the Phase-1 screens (S0–S5, S7 basic, S8 presets, S10, S11 + Agent panel + ⌘K), fully wired, **zero mock data**. Stand up the monorepo, design-system package, typed clients, CI, and the e2e test here.
- **Phase 2:** the heavy editor surfaces (Timeline S6, full ROI S7, Caption Studio S8, Brand Kit S9) + list virtualization + transcript editing.
- **Phase 3–4:** discovery/automation and publish/analytics screens.
- Apply the perf + quality bar **continuously**, not as a final pass.

---

## 7. Appendix

### 7.1 Capability → source → where it's built

| Capability | Source | Phase |
|---|---|---|
| Download (any site) | trove `runner.py` | 0 |
| Transcribe (word-level) | trove `transcriber.py` (whisper.cpp) | 0 |
| Diarization / VAD realign | trove `diarizer.py` | 0 (opt-in) |
| Job queue + persistence | trove `jobs.py`/`jobs_store.py` | 0 |
| Local MCP server | trove `mcp_server.py` (+clip tools) | 0 → 1 |
| Security / models / HW probe | trove `config`/`safety`/`models_store`/`machine` | 0 |
| Find moments | new `moments.py` | 1 |
| Cut | base `ffmpeg -c copy` → `cutter.py` | 1 |
| Speaker timeline | base `analyze.py` **⊕** trove diarizer → `reframe.py` | 1 |
| Pan / split / center | base `build_pan.py` → `reframe.py` | 1 |
| Captions | base `build_ass.py` ← trove `words.json` → `captioner.py` | 1 |
| Export | new `exporter.py` | 1 |
| Editor / studios / brand kits | new (React) | 2 |
| Ranking / automation | new | 3 |
| Publish / analytics | new | 4 |

### 7.2 Open decisions / flags
1. **Flask now vs FastAPI later** — recommend keep Flask (don't rewrite working, tested code).
2. **JSON job store now vs SQLite** — recommend JSON in P0/P1; SQLite (FTS5) in P2 when search/scale demand it. **DECIDED (P2):** shipped the *additive* form — an FTS5 **trigram** transcript index (`engine/transcript_index.py`) that accelerates `/transcripts/search` as a candidate filter over the existing word-scan (full-scan fallback ⇒ no user-facing change). The **job store stays JSON** (the whole-store migration is high-risk, optimization-only, and not yet warranted by scale).
3. **whisper.cpp standard** (drop `openai-whisper`) — recommend yes.
4. **Diarization default off** (heavy PyTorch dep), one-click enable — recommend yes.
5. **Repo strategy** — fold trove into the Spool monorepo (simplest for solo/local-first) vs. internal package dep.

### 7.3 Credits / licensing
- **trove = first-party** (Kaivan + Claude Code) → adopt freely, no README credit needed.
- **Base clip engine** (`github.com/louisedesadeleer/clipify`, MIT; clone URL in §1.0) → **README credit only**; rename its modules, keep the upstream name out of the **shipped product** (UI, code, user-facing strings). The URL lives in this internal build spec so Code can clone it — that's fine; just don't surface the name in the app.
- trove notes "inspired by `averygan/reclip`" (MIT) — if any reclip source was copied, preserve its MIT notice in the README; quick check before launch.

### 7.4 Glossary
**ROI** — face rectangle for motion-based speaker detection. **ASS** — styled, word-level subtitle format Spool burns. **Elicitation** — MCP server-initiated mid-task question. **diar⊕ROI** — fused audio-diarization + video-ROI speaker timeline driving the pan. **Recipe** — a saved end-to-end pipeline. **words.json** — trove's word-timestamped transcript, the caption + moment-finding input.
