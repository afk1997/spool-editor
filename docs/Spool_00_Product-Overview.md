# Spool — Product Overview

**Read this first.** Shared context for the whole project: what we're building, why, the full feature set, the roadmap, and the open decisions. Hand the two role-specific files (below) to Claude Design and Claude Code; this one is the canonical reference both should skim.

**The document set**

| File | Hand to | Contains |
|---|---|---|
| **Spool_00_Product-Overview.md** (this) | You + everyone | Repo analysis, vision/positioning, full feature catalog, roadmap, risks & decisions, glossary, sources |
| **Spool_Design-Brief.md** | Claude Design | The agentic model + the full UI direction (design language, components, every screen & element) |
| **Spool_Engineering-Spec.md** | Claude Code | The build plan **in phases**: what we reuse from the first-party `trove` foundation + the upstream clip engine, the architecture, the MCP surface, the data model, and per-phase acceptance criteria |

Each file is self-contained. In short: **vision, features, risks** are here (Overview); the **UI direction** is in the Design Brief; the **architecture, MCP surface, and phased build plan** are in the Engineering Spec. The agentic model appears in all three.

---

## 1. What Spool builds on (the base engine)

### 1.1 What it is

Spool's engine starts from a proven open-source **Claude Code skill** (credited in the README): a single `SKILL.md` prompt plus four small Python helper scripts. There is no UI, no server, no database, no API, no job queue. The "intelligence" lives in the *prompt* — Claude reads a transcript, eyeballs faces on a single frame, picks funny moments, and matches caption styles. The Python scripts are deterministic glue around `ffmpeg`. It runs **100% locally**, no cloud APIs, tuned for **talking-head dialogue** (podcasts, interviews, 2-person setups).

### 1.2 The pipeline (as implemented)

| Step | What happens | Tooling |
|---|---|---|
| 1. Transcribe | Extract mono 16kHz WAV, run Whisper `tiny.en` with word timestamps → JSON | `ffmpeg`, `openai-whisper` |
| 2. Find moments | **Claude** reads the transcript, scans for punchlines, reversals, awkward pauses, audio peaks → proposes 3–5 candidates `[start, end, why, title]` | LLM (no model) |
| 3. Trim | Instant cut of chosen segment | `ffmpeg -c copy` |
| 4. Reframe 16:9→9:16 | (a) **face-pan** following the speaker, (b) **split-screen**, or center-crop for one face | `ffmpeg` + scripts |
| 5. Caption | Burn opus-style word-by-word ASS captions (3 presets, or match a reference image) | `build_ass.py`, `ffmpeg` |
| 6. Deliver | Save to `<source_dir>/spool_out/`, open the result, offer to iterate | `ffmpeg`, `open` |

### 1.3 The clever bit — speaker tracking with no ML

There is **no face-detection model**. Because the camera is static within a clip, the base engine: (1) has Claude eyeball each face's mouth/chin as a rectangle on one sample frame; (2) uses `ffmpeg` frame-differencing (`tblend=difference` + `signalstats`) to measure **motion energy** in each rectangle; (3) whoever's rectangle moves more = the speaker; (4) builds a **hard-cut x-coordinate expression** that pans a vertical strip to the active speaker. Cost: a few seconds of `ffmpeg` per clip. This is the product's defensible, cheap, offline secret sauce.

### 1.4 The four scripts

| Script | Role | Reused as |
|---|---|---|
| `analyze.py` | Parse two ROI motion files → normalize → smooth → hysteresis speaker decision → merge/collapse segments → speaker-timeline JSON | **Speaker-track service** |
| `build_pan.py` | Segments → nested `ffmpeg` `if()` crop-x expression (hard cuts) | **Reframe / pan renderer** |
| `build_ass.py` | Whisper words → chunked ASS events with active-word highlight (opus / karaoke / minimal presets) | **Caption renderer** |
| `audio_align.py` | FFT cross-correlation to find a sub-clip's offset in a longer source | **De-dupe / "find the no-subs master"** |

### 1.5 Strengths to preserve (these *are* the brand)

- **Local-first & private.** Nothing leaves the machine. No per-clip credits. This is the #1 differentiator vs. Opus Clip / Vizard / Submagic.
- **Fast & cheap.** ~20s of work for a 20s clip on Apple Silicon; no GPU cloud bill.
- **Agent-native.** It already *is* a conversational workflow with decision points — a natural fit for an MCP-driven agent.
- **No heavy CV stack.** No OpenCV, no face model. Tiny dependency surface.
- **Quality captions.** Opus-style word-by-word is exactly what wins on TikTok/Reels.

### 1.6 Gaps = the product opportunity

| Gap today | Product capability it unlocks |
|---|---|
| No way to get videos in except local files | **Phase 1 yt-dlp downloader** (YT/IG/TikTok/X/any) |
| No persistence — every run is throwaway | **Library / projects / job history** |
| One clip at a time, fully interactive | **Batch + queue + "drop a video → auto clips"** |
| Moment-finding is ad-hoc prompt scanning | **Structured discovery + ranking / virality score** |
| Captions/format chosen via chat | **Visual caption studio + brand kits + timeline editor** |
| Output dies in a folder | **Publish / schedule / content calendar** |
| No feedback loop | **Analytics → better future picks** |
| Only a Claude Code skill | **Local MCP server → works in any agent, plus a real UI** |
| Talking-head only | **Content-type presets** (gaming, screen-rec, webinar, vlog) |

**The thesis:** the base engine already has the *brain* (an agent that reasons over transcripts and frames). The product's job is to give that brain a **body** — persistent memory (library/DB), hands (an MCP tool surface), a face (the UI), and reach (download + publish) — without sacrificing the local-first, no-credits soul.

---

## 2. Product vision & positioning

### 2.1 One-liner

> **The open-source, local-first clip studio that turns any long video into platform-ready shorts — driven by an agent, running entirely on your machine, with no per-clip credits and no uploads.**

### 2.2 Positioning vs. the field (researched, June 2026)

| Tool | Model | Strength | Weakness we exploit |
|---|---|---|---|
| **Opus Clip** | Cloud SaaS, credits | Multimodal AI + 0–100 **Virality Score**, sort top clips | Virality score widely flagged unreliable; ~70% of clips need manual cleanup; accuracy drops to ~40% on multi-speaker; uploads required |
| **Vizard** | Cloud SaaS | High-volume, clean cuts respecting sentence boundaries | Cloud, credits, less control |
| **Submagic** | Cloud SaaS | Dynamic animated captions, emoji, trendy styles | Cloud, subscription, caption-first only |
| **OSS clones** (SupoClip, AI-Youtube-Shorts-Generator, OpenShorts, ClippedAI, ComfyUI nodes) | Self-host scripts/Docker | Free, no watermark | **Scripted pipelines, not agentic; thin/no UI; not MCP-native; not editor-grade** |

**The wedge (what no one else has):** *agent-native + local-first + MCP*. Existing OSS tools are batch scripts with a thin web form. Spool is a **conversational agent with a professional editing UI**, where the same engine is exposed as a **local MCP server** so it plugs into Claude Desktop, Claude Code, Cursor, or any MCP client — *and* runs as a standalone self-hosted app. You can say "make me 5 shorts from this podcast, vertical, my brand captions, queue them for posting" in chat, **or** drive every frame by hand in the editor. Same engine underneath.

### 2.3 Principles (non-negotiable)

1. **Local-first by default.** All processing on-device; the network is opt-in and only for (a) downloading source videos, (b) publishing, (c) optional model downloads. The app must be fully usable airplane-mode after setup.
2. **No credits, no lock-in.** Files are plain `.mp4`/`.ass`/`.json` on disk in an open layout. Export everything, anytime.
3. **Agent and manual are equals.** Every agent action has a UI equivalent and vice-versa. The agent never does something you can't inspect, undo, or redo by hand.
4. **Transparent & inspectable.** Show the transcript, the speaker track, the crop boxes, the reasoning behind each pick. No black box.
5. **Bring-your-own-model.** Whisper variants local by default; allow swapping the "moment-finding" LLM (local via Ollama/llama.cpp, or hosted via the user's own key).

### 2.4 Target users (personas)

- **The solo creator / founder** (Louise's original user): records long-form, wants LinkedIn/TikTok cuts fast, cares about privacy and not paying per clip.
- **The podcaster / interviewer:** 2-person talking-head — the sweet spot of the existing pan/split engine.
- **The agency / ghostwriter:** runs many clients' footage; needs batch, brand kits per client, review/approval, no cloud upload of client material (a real selling point).
- **The developer / tinkerer:** wants the MCP server to wire clipping into their own agent or automation.

### 2.5 Non-goals

Full NLE (Premiere/Resolve) replacement; multi-track music production; cloud rendering farm; team SSO/billing (this is local-first OSS — see §10 for the optional hosted future).

---

## 3. The agentic model — "one agentic workflow"

This is the spine of the whole product. **The UI is a window into an agent loop, and the MCP server is the agent's hands.**

### 3.1 Two modes, one engine

| Mode | Who drives | When | Surface |
|---|---|---|---|
| **Agent mode** | The LLM, via MCP tools, pausing for **elicitation** at decision points | "Just make me clips" — hands-off, conversational | Chat/agent panel + live progress in the UI |
| **Manual mode** | The user, clicking through the UI; the engine runs deterministically | Fine control, fixing the agent's choices | Full editor UI |

Both call the **same engine functions** (download, transcribe, find-moments, cut, reframe, caption, render). Agent mode = the engine functions wrapped as MCP tools; Manual mode = the engine functions wrapped as UI actions. There is no second code path.

### 3.2 The canonical agent loop

```
  INGEST            ANALYZE              PROPOSE            DECIDE             RENDER             PUBLISH
 ┌────────┐       ┌──────────┐       ┌────────────┐      ┌─────────┐       ┌──────────┐       ┌──────────┐
 │download│  ──▶  │transcribe│  ──▶  │ find &     │ ──▶  │elicit:  │  ──▶  │ cut →    │  ──▶  │ schedule │
 │ or pick│       │ + detect │       │ rank       │      │ pick/   │       │ reframe →│       │ or post  │
 │ file   │       │ faces    │       │ candidates │      │ format/ │       │ caption  │       │ or save  │
 └────────┘       └──────────┘       └────────────┘      │ style   │       └──────────┘       └──────────┘
   yt-dlp          whisper +           LLM over            └─────────┘        ffmpeg + scripts    publish API
                   roi-motion          transcript           (MCP            (Tasks ext:           / calendar
                                       + signals            elicitation)     long-running job)
        ▲                                                        │
        └──────────────── agent may loop back on user feedback ◀─┘
```

Decision points (★) are where the current skill asks questions in chat. In the product these become **MCP elicitation** requests that the UI renders as inline cards (pick clips, choose 9:16/16:9/1:1, pan vs split, caption style). The user answers in chat *or* by clicking the card — same event.

### 3.3 Why this is more than a script

A scripted pipeline (the OSS clones) runs start→finish with fixed params. The agent loop lets the user **intervene in natural language at any step** ("actually only the funny ones", "make the captions bigger", "the left box is on his hand, move it"), and lets the agent **reason** ("this clip has 3 scene cuts so I'll center-crop instead of pan, and warn you"). That conversational, inspect-and-adjust loop — backed by a real editor for when you want pixels — is the product.

---

## 4. Phased roadmap (overview)

Build in phases; each phase is independently shippable and dogfood-able. Phase 1 is the MVP and **must include the yt-dlp downloader** (you have the code).

| Phase | Theme | Ships | Outcome |
|---|---|---|---|
| **P1 — Core loop + Ingest** | "Get a video in, get clips out, locally" | yt-dlp **downloader**, library, transcribe, agent moment-finding, cut, **face-pan / split / center reframe**, opus/karaoke/minimal captions, render queue, local MCP server (core tools), basic UI | A usable local Opus-Clip alternative |
| **P2 — Editor + Caption/Brand Studio** | "Make it look pro and on-brand" | Timeline editor, speaker-track editor (fix ROIs visually), caption studio (live styling), brand kits, reframe presets per content type, transcript-based editing | Editor-grade control, repeatable brand look |
| **P3 — Discovery + Automation** | "Find the best moments at scale, hands-off" | Ranking/virality scoring, content-type detectors, batch & **watch-folder automation**, templates/recipes, B-roll & emoji, multi-language | Volume + quality without babysitting |
| **P4 — Publish + Analyze (+ optional collab)** | "Close the loop" | Publish/schedule to TikTok/Reels/Shorts/LinkedIn/X, content calendar, performance analytics feeding back into ranking, light multi-seat/agency features | A full create→publish→learn loop |

A visual roadmap/Gantt and screen-flow map are good candidates for Claude Design to produce from §4 and §8.

---

## 5. Feature catalog (detailed)

Grouped by capability area. Each feature notes **what**, **why**, **how it builds on the existing engine**, and its **phase**.

### 5.1 Ingestion & the downloader — `Phase 1` (priority)

The single biggest unlock: today you can only point at a local file. yt-dlp lets users pull source video from **YouTube, Instagram, TikTok, X/Twitter, Vimeo, Reddit, and 1,000+ sites**.

| Feature | Detail | Phase |
|---|---|---|
| **URL import (yt-dlp)** | Paste one or many URLs → fetch best-quality MP4 (configurable: resolution cap, codec, container). Show resolved title, duration, channel, thumbnail before/while downloading. | P1 |
| **Local file import** | Drag-drop or file-picker; the original skill's entry point. | P1 |
| **Format/quality selector** | Map yt-dlp `-f` to friendly choices: "Best", "1080p", "720p", "Audio only". Default cap at 1080p (the pan math assumes ~1920×1080; 4K is downscaled or coordinates doubled per the skill's notes). | P1 |
| **Subtitles & metadata** | Pull existing captions (`--write-subs`/auto-subs) to skip/seed Whisper; embed/keep metadata, chapters, thumbnail. Chapters are great seeds for clip boundaries. | P1/P2 |
| **Authenticated downloads** | Cookie support for member-only/age-restricted content via **browser cookie import** (brave, chrome, chromium, edge, firefox, opera, safari, vivaldi, whale) or a cookies.txt file. Surfaced in Settings → Integrations, used carefully (see security §7.8). | P2 |
| **Playlists & channels** | Expand a playlist/channel URL into a selectable list; batch-queue downloads. | P3 |
| **Livestream / long-VOD handling** | Segmented download; allow clipping a time-range without downloading the whole VOD where the site permits. | P3 |
| **Resumable / concurrent downloads** | Progress %, ETA, pause/resume, retry, concurrency limit; respect the engine's job queue. | P1/P2 |

> **Legal/ethical note for the UI:** show a one-time, dismissible reminder that users are responsible for rights to downloaded content and each platform's ToS. Keep it factual, not preachy. (Spool is a tool; rights are the user's responsibility.)

### 5.2 Library & media management — `Phase 1`

| Feature | Detail | Phase |
|---|---|---|
| **Sources library** | Every imported/downloaded video as a card: thumbnail, title, duration, source (file/URL), date, status (downloading/transcribing/ready), # clips made. | P1 |
| **Projects** | A project groups a source + its transcript + candidates + clips + render outputs. One source can have many clips. | P1 |
| **Clips library** | All generated clips: thumbnail, aspect badge, duration, caption style, platform target, status, virality score (P3). Filter/sort/search. | P1 |
| **Search & filter** | By title, transcript text (full-text search over Whisper output), date, status, platform, score. | P2 |
| **Storage manager** | Disk usage by project; "clean intermediates" (the `/tmp/spool` artifacts), archive, delete. Local-first means disk hygiene matters. | P2 |
| **Tags & collections** | Organize by client/campaign/topic. Essential for the agency persona. | P3 |

### 5.3 Transcription & analysis — `Phase 1`

| Feature | Detail | Phase |
|---|---|---|
| **Whisper transcription** | Default `tiny.en` for speed (per skill); selectable `base`/`small`/`medium`, language auto-detect, word timestamps always on. Show model trade-off (speed vs accuracy) in UI. | P1 |
| **Transcript viewer** | Word-level, clickable to seek; speaker labels (from the speaker track); editable to fix Whisper errors (improves captions). | P1/P2 |
| **Re-transcribe on clip** | The skill re-runs Whisper on the trimmed clip for accurate caption timing — keep this; expose as automatic. | P1 |
| **Scene-cut detection** | `ffmpeg select='gt(scene,0.3)'` to count cuts inside a candidate — used to warn when face-pan won't work and to auto-fallback to center-crop. | P2 |
| **Audio-peak / energy track** | `volumedetect` + RMS over time → laughter/applause/excitement signal for moment-finding and for the timeline's energy lane. | P2 |
| **Filler/pause map** | Mark "uh/um", long gaps — both as funny-moment signals and as candidates for silence-removal (a popular editing feature). | P3 |
| **Silence removal / "tighten"** | Auto-cut dead air and filler from a clip (jump-cut style). High-value, builds on the filler map. | P3 |

### 5.4 Clip discovery & ranking — `Phase 1` → `Phase 3`

| Feature | Detail | Phase |
|---|---|---|
| **Agent moment-finding** | The current LLM scan: punchlines, reversals, awkward pauses, audio peaks → 3–5 candidates `[start, end, why, title]`. Now structured into DB rows with rationale. | P1 |
| **Candidate review UI** | Card per candidate: title, time range, "why it's funny" rationale, mini-preview, transcript excerpt. Accept / reject / adjust in/out / merge. | P1 |
| **Content-type modes** | Beyond "funny": *insightful/educational*, *controversial/hot-take*, *story/emotional*, *how-to/steps*, *Q&A*. Each tunes the moment-finding prompt + signals. | P3 |
| **Ranking / "opportunity" score** | A transparent, multi-signal score (hook strength, self-containedness, emotional arc, audio energy, length fit) — **explained, not a mystery 0–99**. Directly answers Opus Clip's biggest complaint (opaque, unreliable virality score). Show the contributing factors. | P3 |
| **Hook analysis** | Score/flag the first 3 seconds (the make-or-break for retention); suggest a stronger entry point or a text hook overlay. | P3 |
| **Auto-title & description** | Generate platform-tuned titles, descriptions, hashtags per clip from the transcript. | P3 |
| **De-dupe** | Use `audio_align.py` to detect near-duplicate clips/segments across a project or library. | P3 |

> **Differentiation on scoring:** every OSS clone copied Opus Clip's number. We make the score **glass-box** — a stacked bar of named factors the user can reweight ("I care about hooks more than length"). That's a feature reviewers will notice.

### 5.5 Reframing & layout — `Phase 1` → `Phase 2`

| Feature | Detail | Phase |
|---|---|---|
| **Aspect targets** | 9:16 (TikTok/Reels/Shorts), 16:9 (YouTube), 1:1 (feed), plus 4:5 (IG feed) and 9:16-with-safe-zones overlay. | P1 |
| **Face-pan (hard-cut)** | The signature engine: ROI motion → speaker timeline → hard-cut pan. Auto for 2-person 16:9→9:16. | P1 |
| **Split-screen** | Both faces stacked, active speaker on top — the skill's Step 4b. | P1 |
| **Center / smart crop** | Single-talker; later a saliency-aware crop for non-face content. | P1/P3 |
| **Visual ROI editor** | Replace "eyeball boxes in chat" with **draggable boxes on a sample frame**, live preview of the diff/motion, verify overlay (the skill's `drawbox` step made interactive). The biggest manual-mode upgrade. | P2 |
| **Speaker-track editor** | Show the computed speaker timeline as an editable lane; nudge cut points, set min-dwell, flip a segment, smooth. Writes back to the same `segments.json`. | P2 |
| **Reframe presets by content type** | Talking-head (pan), reaction (PiP), gameplay (gameplay-on-top/cam-bottom), screen-rec (slides + presenter), webinar. Broadens beyond the original niche. | P3 |
| **Multi-cam / scene-cut aware** | When scene cuts exist, switch ROI sets per scene or fall back gracefully (today it just warns). | P3/P4 |
| **Background fill** | For letterboxed sources: blurred fill, solid, or gradient instead of crop. | P2 |

### 5.6 Caption & brand studio — `Phase 1` → `Phase 2`

| Feature | Detail | Phase |
|---|---|---|
| **Preset styles** | opus / karaoke / minimal from `build_ass.py`, day one. | P1 |
| **Live caption editor** | WYSIWYG over the preview: font, size, weight, fill, outline, shadow, position, max words/line, active-word highlight color, animation (pop/fade/slide). Writes ASS. | P2 |
| **Style from reference image** | The skill already supports "paste an example to match." Make it a feature: upload a screenshot → agent infers font/size/color/position → generates matching ASS. | P2 |
| **Emoji & keyword emphasis** | Auto-emphasize keywords, optional auto-emoji (Submagic's hook) — toggle, never forced. | P3 |
| **Brand kits** | Saved fonts, colors, caption style, logo/watermark, intro/outro, lower-thirds. Per-project or per-client. The agency unlock. | P2 |
| **Text/hook overlays** | Add a headline hook for the first seconds; lower-thirds with speaker name. | P2/P3 |
| **B-roll & cutaways** | Insert images/short clips over the audio (from a local folder or generated). | P3 |
| **Custom fonts** | Import font files; manage the font library used by ASS rendering. | P2 |

### 5.7 Editor & timeline — `Phase 2`

| Feature | Detail | Phase |
|---|---|---|
| **Clip timeline** | Trim handles, frame-accurate in/out, snap to word/sentence/scene boundaries (fixing Opus Clip's "mid-sentence cut" complaint that Vizard beats them on). | P2 |
| **Transcript-based editing** | Delete a word/sentence in the transcript → cut it from the video (Descript-style). Powerful, builds on word timestamps. | P2/P3 |
| **Lanes** | Video, captions, speaker-track, audio-energy, scene-cuts, B-roll, overlays. | P2 |
| **Preview player** | Scrubbable, with caption + reframe rendered live (proxy quality), safe-zone overlays per platform. | P2 |
| **Undo/redo & versions** | Non-destructive; every render is a version you can compare. | P2 |
| **Multi-clip board** | Work across all candidates from one source at once. | P2 |

### 5.8 Rendering, batch & automation — `Phase 1` → `Phase 3`

| Feature | Detail | Phase |
|---|---|---|
| **Render queue** | Every cut/reframe/caption/export is a job with status, progress %, ETA, logs, cancel/retry. Built on the engine's job model (§7.4). | P1 |
| **Batch render** | Select N candidates → render all with chosen format + style. | P1/P2 |
| **Recipes / templates** | Save a full pipeline ("9:16 + pan + my brand captions + 1080p") and apply to any source in one click or one agent sentence. | P3 |
| **Watch-folder automation** | Point at a folder (or a channel/playlist); new videos auto-ingest → auto-find → auto-render per a recipe → land in an "for review" queue. The "drop a video → clips" magic. | P3 |
| **Scheduled runs** | "Every morning, check this channel and clip new uploads." (Maps to scheduled tasks.) | P3/P4 |
| **Hardware-aware encode** | VideoToolbox on macOS (per skill), NVENC/QSV/VAAPI elsewhere, libx264 fallback. Auto-detect, expose in Settings. | P1/P2 |
| **Export presets** | Per-platform container/codec/bitrate/fps/loudness-normalization (-14 LUFS for social). | P2 |

### 5.9 Publish, schedule & analyze — `Phase 4`

| Feature | Detail | Phase |
|---|---|---|
| **Direct publish** | Post/schedule to TikTok, Reels, YouTube Shorts, LinkedIn, X via their APIs (OAuth, tokens stored locally/OS keychain). | P4 |
| **Content calendar** | Drag clips onto dates/times; per-platform queues; best-time hints. | P4 |
| **Caption/hashtag per platform** | Tailor copy per destination from the auto-generated metadata. | P4 |
| **Performance analytics** | Pull views/retention/likes per posted clip; dashboard. | P4 |
| **Feedback loop** | Feed real performance back into the ranking model so future picks improve — the thing cloud tools *can't* do privately. | P4 |

### 5.10 Settings, models & system — `Phase 1` → `Phase 2`

| Feature | Detail | Phase |
|---|---|---|
| **Dependency doctor** | Detect/install/verify ffmpeg, whisper, yt-dlp, Python, numpy; show versions; one-click fix guidance. Critical for local-first onboarding. | P1 |
| **Model manager** | Choose/download Whisper sizes; pick the moment-finding LLM: local (Ollama/llama.cpp) or hosted via the user's own API key. | P1/P2 |
| **Hardware & performance** | Encoder selection, concurrency, proxy resolution, temp/output paths. | P1/P2 |
| **Integrations** | yt-dlp cookies, publish accounts, MCP server toggle + token. | P2/P4 |
| **Privacy panel** | Explicit switches for any network call; "offline mode". Reinforces the brand. | P1 |

---

## 10. Risks, open questions & decisions needed from you

| # | Topic | Question / risk | Recommendation |
|---|---|---|---|
| 1 | **Name/brand** | Name decided: **Spool**. Keep the upstream project's name out of the product UI/code (credit in README only); confirm trademark/domain availability | Quick trademark/handle check for "Spool" pre-launch. |
| 2 | **Moment-finding model** | The "brain" is an LLM. Local (Ollama) keeps it 100% offline but lower quality; hosted (user's key) is better but breaks pure-local | Default local with a clearly-labeled "use my API key for smarter picks" opt-in. Be explicit in UI. |
| 3 | **Cross-platform parity** | The skill leans on macOS VideoToolbox & `open` | Engine must abstract encoders + "reveal in folder" per OS from day one. |
| 4 | **yt-dlp & ToS** | Downloading from some platforms may violate their ToS; yt-dlp breaks as sites change | Ship the rights/ToS reminder; make yt-dlp easily updatable; never bundle credentials. |
| 5 | **Multi-speaker accuracy** | Even the leaders hit ~40% on multi-speaker; the pan relies on static-camera assumption | Lean into the editor (manual ROI/ track fix) as the differentiator; warn on scene cuts; don't over-promise auto. |
| 6 | **Scope creep** | Publish/analytics (P4) is a different product surface | Keep P4 optional/pluggable; the core is create, not a social scheduler. |
| 7 | **Hosted future** | "Open-source SaaS" could later mean a hosted tier | Architecture supports it (Streamable HTTP MCP, stateless engine), but keep P1–P3 strictly local-first. Decide later. |
| 8 | **Licensing** | Repo is MIT; ffmpeg/whisper/yt-dlp licensing for redistribution | Confirm bundling vs. install-on-first-run (Dependency Doctor) to stay clean. |

**Decisions I need from you to refine further:** product name; default to local or hosted moment-finding LLM; first distribution target (Docker vs. desktop); and whether P4 (publish/analytics) is in your near-term vision or a community/plugin concern.

---

## 11. Appendix

### 11.1 Glossary

- **ROI** — region of interest; the face rectangle used for motion-based speaker detection.
- **ASS** — Advanced SubStation Alpha; the subtitle format Spool burns (styled, word-level).
- **Elicitation** — an MCP server-initiated request asking the client/user to choose something mid-task.
- **Task (MCP)** — a long-running operation tracked via status + progress rather than a blocking call.
- **Recipe** — a saved end-to-end pipeline (aspect + mode + caption style + export preset).
- **Speaker track** — the per-time L/R speaker timeline (`segments.json`) driving the pan/split.

### 11.3 Sources (research)

- Opus Clip / Vizard / Submagic 2026 comparison & virality-score reliability — [Reap: State of AI clipping tools 2026](https://reap.video/reports/state-of-top-ai-video-clipping-tools-2026), [Listicler: Vizard vs Opus Clip](https://listicler.com/comparisons/vizard-vs-opus-clip), [Ssemble: Vizard vs Opus vs Ssemble](https://www.ssemble.com/blog/vizard-vs-opus-clip-vs-ssemble), [Opus Clip — Virality Score](https://help.opus.pro/docs/article/virality-score)
- Open-source alternatives landscape — [SupoClip](https://github.com/FujiwaraChoki/supoclip), [AI-Youtube-Shorts-Generator](https://github.com/samuraigpt/ai-youtube-shorts-generator), [OpenShorts](https://github.com/mutonby/openshorts), [opus-clip-alternative topic](https://github.com/topics/opus-clip-alternative)
- yt-dlp capabilities, sites, cookies — [yt-dlp guide 2026 (RapidSeedbox)](https://www.rapidseedbox.com/blog/yt-dlp-complete-guide), [yt-dlp ArchWiki](https://wiki.archlinux.org/title/Yt-dlp), [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ)
- MCP primitives, elicitation, Tasks, MCP Apps, transports — [MCP Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25), [MCP 2026-07-28 release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/), [MCP Cheat Sheet 2026 (Webfuse)](https://www.webfuse.com/mcp-cheat-sheet)

---

*End of spec. Hand §8 to Claude Design, §6–§7 to Claude Code, start with §9.*







