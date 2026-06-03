# Spool — Post-Phase-2 Quality Pass (plan)

> **Scope decided by Kaivan (2026-06-03).** Runs **after** the three fixes that already landed
> this session (frame-accurate cut / caption sync · diarization over-count · reframe crash on long
> clips) and **before/alongside Phase 3**. Same discipline as every shipped slice: **diagnose before
> fixing**, **TDD the engine (RED→GREEN)**, **measure with a harness**, **verify on real media**,
> **commit each verified slice**, keep `docs/PROGRESS.md` updated. Read `docs/PROGRESS.md` +
> `docs/Spool_Engineering-Spec.md` first.

## In scope (this plan)
- **A. Decouple VAD word-realignment from the diarization flag** — caption-timing accuracy.
- **B. Active-speaker = fuse audio (diarization) + video (face-track)** in the auto-pan.
- **C. Diarization accuracy ceiling** — more accurate speaker count / turns / labels.
- **D. Caption craft** — speaker color, line-breaking, keyword emphasis.
- **E. Better moment-finding** — richer (non-text) signals + a feedback re-rank. **No local LLM.**
- **F. Performance & preview fidelity** — fewer / hardware encodes + a real-render editor preview.

## Explicitly deferred — revisit only after Phase 3 if something underperforms
Kaivan: *"rest is okay for now, we'll do these at last after Phase 3 if something doesn't live up to
what it says."*
- Honest **per-stage failure surfacing + Retry** in the studio (today an errored stage can fall back
  to a misleading preview — see the reframe-crash post-mortem in PROGRESS).
- **Disk ↔ store reconciliation** + stop re-downloading a source whose media is already on disk
  (the TTL-sweep currently forces a re-import).
- **Widen the e2e** to a long / multi-speaker clip + a harness-based **quality-regression gate**.

## Explicitly OUT
- **Local / offline moment-finder LLM.** Kaivan: *"too heavy, no one runs LLMs offline."* Keep the
  **codex bridge** (`clip/llm.py`) as the default provider.

## Recommended sequence (dependencies in parens)
1. **A** — quick, isolated win.
2. **C** — foundational; B and D's speaker color both lean on diarization quality.
3. **B** — (needs C) fuse the now-better diarization into the pan.
4. **D** — (speaker color needs C) the rest is independent.
5. **F** — independent; the preview-fidelity half touches the studio.
6. **E** — largest; overlaps the **Phase 3** glass-box ranking, so coordinate with that.

The three measurement harnesses already exist and are the acceptance tooling:
`engine/scripts/{reframe_eval,caption_sync_eval,diarization_eval}.py`. Re-import a real video to
get fresh media (downloads are TTL-swept): zoo `jNQXAC9IVRw` (1 speaker), and a real 2-speaker
interview such as Karpathy×Stephanie Zhan for diarization / active-speaker tests.

---

## A. Decouple VAD word-realignment from the diarization flag
**Goal.** `transcriber.realign_words_to_vad` (snaps whisper's post-silence word drift to silero-vad
speech regions) should run whenever **silero-vad is available**, independent of whether the user
turned on **speaker diarization**. Caption-timing accuracy must not depend on the speaker-label
feature flag.

**Where.** `app.py` `_build_transcribe_target` (~L339–360): realignment currently lives *inside*
`if diarizer.available():` (which requires `TROVE_DIARIZATION=on`). `transcriber.realign_words_to_vad`,
`diarizer._vad_speech_chunks`, `diarizer._flag_enabled` / `available`.

**Approach.** Add a lightweight `diarizer.vad_available()` (silero-vad + librosa import OK, *ignoring*
the `TROVE_DIARIZATION` flag). In the worker, run realignment when `vad_available()` even if
diarization (speaker labelling) is off; keep speaker diarization separately flag-gated. Diarization
is in the core install (locked decision), so the deps are present — only the **flag** gate is removed
for realignment.

**Verify / acceptance.** TDD: a worker-level test that with `TROVE_DIARIZATION=off` but silero-vad
present, `realign_words_to_vad` is still called (and is NOT called when silero-vad is missing).
Measure on real media with internal silences: active-word-vs-audio drift after a pause is reduced
(extend `caption_sync_eval.py` to report post-silence drift, or a small dedicated probe). **Measure
that it helps before keeping it** — if realignment doesn't reduce drift on real clips, don't ship the
change. Full `pytest -q` green (OpenAPI contract test runs on the whole suite).

**Risk.** Adds a silero-vad pass to every transcribe even with diarization off (silero-vad is light;
acceptable). None if deps absent (no-op).

## B. Active-speaker = fuse audio (diarization) + video (face-track)
**Goal.** When several faces are visible (wide two-shot) or a face pick is ambiguous, use the audio
diarization turns (who is talking, when) to bias which face the auto-pan follows. Fixes the two
weak cases seen on the Karpathy clip: the wide two-shot (frame the talker, not the listener) and a
speaker who is briefly off-camera.

**Where.** `clip/face_track.py` (`track`, `pick_face`, `cluster_by_x`, `mouth_motion`,
active-speaker selection); `clip/reframe.py` (`_face_pan_vf`); `clip_runner._do_reframe` (the
`face_timeline` branch — today it does NOT pass diarization into face-track, even though
`diarization_from_words` + the diar⊕ROI fusion `reframe._fuse_diar_roi` already exist and are unused
in the auto-pan path).

**Approach.** Thread the source's diarization turns into `face_track.track`. Per shot, when
mouth-motion doesn't yield a clear winner, prefer the face whose screen-side matches the
audio-active speaker for that time window (map diar speaker → left/right via accumulated overlap, as
`_fuse_diar_roi` already does). Keep video-only behaviour as the fallback when diarization is
absent/off. Single-face shots unchanged.

**Verify / acceptance.** Extend the framing check: a harness that, across a back-and-forth clip,
reports **agreement between the framed face's side and the diarization speaker's side** through each
switch (target: high agreement, no oscillation at boundaries). Visual spot-check (frames at each
turn). `reframe_eval.py` metrics must not regress (face_present, jitter, center_dx). Pure-logic parts
TDD'd in `tests/test_face_track.py`.

**Risk.** Bad diarization could mislead the pan → do **C first**, and keep video as the confident
fallback (only let audio break ties, not override a strong visual pick).

## C. Diarization accuracy ceiling
**Goal.** Lift speaker **count**, **turn boundaries**, and **labels** above resemblyzer's ~70%.
Feeds B (active-speaker) and D (speaker-colored captions).

**Where.** `diarizer.py`: `_get_encoder` (resemblyzer `VoiceEncoder`), `_continuous_embeddings`,
`_cluster_partials`, `_auto_k_partials` (now centroid-distance gated — this session), `_smooth_labels`,
`MIN_CENTROID_DIST`. `transcriber.apply_speakers` (word→speaker attribution).

**Approach (evaluate, don't assume).** Candidates, benchmarked on `diarization_eval.py` against
ground truth before adopting: (1) **stronger local, no-auth embedder** — SpeechBrain **ECAPA-TDNN**
(downloadable model, no HF auth needed for inference) in place of / alongside resemblyzer's encoder;
(2) **overlap / short-turn handling** (current `_smooth_labels` window can swallow real ≤1s turns);
(3) better **turn boundaries** by snapping cluster changes to silero-vad / word gaps. Keep it
**local + no-auth** (locked). Build a small **labelled multi-clip benchmark** (≥3 clips with known
speaker counts + rough turns) so accuracy is a number, not a vibe.

**Verify / acceptance.** `diarization_eval.py` on the benchmark: speaker-count correct on all;
turn-boundary error (e.g., mean boundary offset, or frame-level DER if feasible) improves vs the
current pipeline. Don't swap the embedder unless it **measurably beats** resemblyzer on the
benchmark. Engine suite green.

**Risk.** Model download weight / first-run latency; dep surface (torch already present). Determinism
for tests (embeddings are deterministic per input — keep it that way).

## D. Caption craft
**Goal.** Better-looking, more readable burned captions: **speaker-colored** words (per diarization
label), **smarter line-breaking** (balance lines, avoid 1-word orphans), and **keyword emphasis**
(scale/bold salient words).

**Where.** `clip/captioner.py` (`generate`, `_ass_overrides`), `clip/backhalf/ass_captions.py`
(the chunking + per-word color logic), word `speaker` field already in `words.json` segments.

**Approach.** Speaker color: map each word's `speaker` → a palette color in the ASS primary/highlight
(reuse the brand-kit color plumbing). Line-breaking: balance chunk widths / avoid orphan last lines in
the chunker. Keyword emphasis: optional flag to up-scale a detected keyword (simple heuristic first;
LLM-tagged later if cheap via the existing bridge). All **additive** — defaults reproduce today's
output byte-for-byte when the new options are off.

**Verify / acceptance.** `caption_sync_eval.py`: **timing unchanged** (still 0 ms drift). Unit tests on
`ass_captions` for the new ASS output (speaker color present, no orphan lines, emphasis applied).
Visual check on a real 2-speaker clip (each speaker a distinct color). Output unchanged when options
off (regression guard).

**Risk.** Speaker color is only as good as diarization → gate behind good labels (do **C** first).

## E. Better moment-finding (no local LLM)
**Goal.** Find clip-worthy moments more reliably than transcript-text-alone. **Keep the codex bridge**
as the LLM; add **non-text signals** and a **feedback re-rank**.

**Where.** `clip/moments.py` (`find_moments`), `clip/llm.py` (codex provider — unchanged),
`clip/face_track.scene_cuts` (already exists), `clip/backhalf/` (audio tools), `clip_runner`
(records renders/exports), and the **Phase-3 glass-box ranking** surface (coordinate — this overlaps).

**Approach.** (1) **Signal extraction**: audio energy / laughter peaks (ffmpeg loudness / onset),
scene-change density (`scene_cuts`), question/answer + sentiment cues from the transcript — expose
them as named, glass-box features. (2) **Fuse** signals into candidate scoring (transparent weights —
this is the Phase-3 glass-box score with real inputs). (3) **Feedback loop**: log which candidates the
user actually **renders / exports / publishes** and re-weight future ranking. No model leaves local
except the transcript text already sent to codex.

**Verify / acceptance.** Signal extractors unit-tested (energy/scene/QA on synthetic + a real clip).
Ranking is **explainable** (every score traces to named signals — the spec §6 glass-box rule). A/B on
one known video: signal-aware ranking surfaces moments a human agrees are stronger than text-only.
Because "good moment" has no hard ground truth, **log the signals and the chosen weights** (no silent
magic).

**Risk.** Scope overlaps Phase 3 — decide whether this lands as part of Phase 3's ranking work to
avoid double-building. Don't over-fit weights to one video.

## F. Performance & preview fidelity
**Goal.** (1) Cut the per-clip ffmpeg work — a clip is re-encoded **~4×** today (cut → reframe →
caption-burn → export), and the **reframe + caption passes use libx264 defaults (~CRF 23)**.
(2) Make the editor preview match the real output.

**Where.** `clip/cutter.py`, `clip/reframe.py` (`render` — no `-c:v`/quality set), `clip/captioner.py`
(`burn` — no `-c:v`/quality set), `clip/exporter.py` (`pick_encoder` → VideoToolbox/NVENC/x264).
Studio editor preview: `apps/studio/src/app/clips/[id]/page.tsx` (+ `/reframe`) — currently a **CSS
crop approximation** that diverges from the real reframe (root of the "audience vs real crop"
confusion).

**Approach.** Encodes: use `pick_encoder` (hardware) for the intermediate reframe/caption passes (not
just export); raise/standardize intermediate quality (explicit CRF, or visually-lossless). Fewer
passes: combine **reframe + caption** into one filtergraph (`crop,scale,subtitles=…`) so there's one
encode instead of two — fewer generations = sharper + faster. Preview: render a **fast low-res preview
of the actual reframe** (e.g., a quick 360p reframe pass) and play that instead of the CSS crop, so
what-you-see = what-you-get.

**Verify / acceptance.** Benchmark encode wall-time before/after (faster). `caption_sync_eval.py`
(sync preserved) + `reframe_eval.py` (framing preserved) on the merged pass. Visual: preview frame ==
rendered frame at the same timestamp. Engine + e2e green.

**Risk.** Hardware-encoder availability varies (keep the x264 fallback + deterministic tests on
software). Merging reframe+caption must keep the separate `reframed.mp4` artifact the editor/preview
relies on (or update the consumers).

---

## Definition of done (whole pass)
Each item: engine TDD (RED→GREEN), measured on real media with the relevant harness, demo/visual
spot-check, full `pytest -q` + studio `typecheck/lint/test/build` + Playwright `e2e` green, committed
as its own verified slice, `PROGRESS.md` updated. Deferred items (above) stay deferred unless a
shipped item underperforms in manual testing.
