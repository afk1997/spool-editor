"""Clip-job orchestration: thin work-closure builders for ClipJobManager.

This is to the clip engine what ``runner.py`` / ``transcriber.py`` are to downloads /
transcription — the orchestration layer that ``create_app`` wires the manager's jobs
through. It owns the on-disk clip tree, the per-clip ``meta.json`` "Clip record", the
diar⊕ROI input prep (audio turns pulled from the transcript), artifact chaining, the
one-shot pipeline, and the cancel/progress hooks — while the actual media work stays in
the already-tested ``clip/`` modules (cutter/reframe/captioner/exporter/moments).

On-disk layout (under the download dir, extending trove's flat ``{source_id}.*``)::

    clips/{clip_id}/
        meta.json            # the Clip record: {clip_id, source_id, start, end}
        clip.mp4             # cut (lossless trim of the source window)
        frame.jpg            # detect_faces sample frame
        track.json           # the diar⊕ROI speaker track
        reframed.mp4         # pan/split/center render
        captions.ass         # styled captions sliced to the window
        captioned.mp4        # captions burned in
        renders/{render_id}.mp4   # final platform export(s)

Keeping the deps (download dir + managers) injected makes the whole layer unit-testable
with the engine functions mocked.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import transcript_io
from clip import captioner, cutter, exporter, moments, reframe


def _scale_roi(roi: dict, width: int, height: int) -> dict:
    """Fractional ROI (0–1, resolution-independent — what the studio sends) → source pixels."""
    return {
        "x": int(round(float(roi.get("x", 0.0)) * width)),
        "y": int(round(float(roi.get("y", 0.0)) * height)),
        "w": int(round(float(roi.get("w", 0.0)) * width)),
        "h": int(round(float(roi.get("h", 0.0)) * height)),
    }


def _clean_segments(segs) -> list[dict]:
    """Sanitize an edited speaker track from S7 → ``[{start, end, speaker:'left'|'right'}]``."""
    out = []
    for s in segs or []:
        if not isinstance(s, dict) or s.get("speaker") not in ("left", "right"):
            continue
        try:
            st, en = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if en > st:
            out.append({"start": st, "end": en, "speaker": s["speaker"]})
    return out


class ClipRunner:
    def __init__(self, *, download_dir, job_manager, clip_manager):
        self.download_dir = Path(download_dir)
        self.job_manager = job_manager
        self.clip_manager = clip_manager

    # ----- layout / records ---------------------------------------------

    def clip_dir(self, clip_id: str) -> Path:
        return self.download_dir / "clips" / clip_id

    def source_paths(self, source_id: str) -> tuple[str | None, str | None]:
        """(media_path, words_json_path) for a source (= media job id)."""
        job = self.job_manager.get(source_id)
        fp = getattr(job, "file_path", None) if job else None
        if not fp:
            return None, None
        return fp, os.path.splitext(fp)[0] + ".words.json"

    def write_clip_meta(self, clip_id: str, *, source_id: str, start: float, end: float) -> dict:
        d = self.clip_dir(clip_id)
        d.mkdir(parents=True, exist_ok=True)
        meta = {"clip_id": clip_id, "source_id": source_id,
                "start": float(start), "end": float(end), "created_at": time.time()}
        (d / "meta.json").write_text(json.dumps(meta))
        return meta

    def load_clip_meta(self, clip_id: str) -> dict | None:
        p = self.clip_dir(clip_id) / "meta.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def diarization_from_words(self, words_path: str | None) -> list[dict]:
        """Audio turns ``[{start, end, speaker}]`` from a transcript's speaker'd segments —
        the *who/when* half the reframe fuses with the video ROI motion."""
        if not words_path or not os.path.exists(words_path):
            return []
        try:
            data = transcript_io.load(words_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        turns = []
        for seg in data.get("segments") or []:
            sp, s, e = seg.get("speaker"), seg.get("start"), seg.get("end")
            if sp is None or s is None or e is None:
                continue
            turns.append({"start": float(s), "end": float(e), "speaker": sp})
        return turns

    # ----- hooks / helpers ----------------------------------------------

    @staticmethod
    def _hooks(job) -> dict:
        """Cancel + live-process hooks handed to each ffmpeg step so the manager's
        cancel() can kill the running subprocess."""
        return {
            "cancel_check": lambda: job._cancel_flag,
            "register_proc": lambda p: setattr(job, "process_handle", p),
        }

    @staticmethod
    def _cancellable(job, body) -> None:
        """Run ``body``; treat a cancelled ffmpeg as a clean abort so the job keeps the
        CANCELLED status the manager already set (never surfaces as an error)."""
        try:
            body()
        except RuntimeError as e:
            if str(e) == "cancelled" or job._cancel_flag:
                return
            raise

    def _stage_input(self, clip_dir: Path, prefer: tuple[str, ...]) -> str:
        """The latest video artifact to feed the next step — first existing of ``prefer``,
        else the last (the engine raises a clear error if it's genuinely missing)."""
        for name in prefer:
            if (clip_dir / name).exists():
                return str(clip_dir / name)
        return str(clip_dir / prefer[-1])

    # ----- per-stage engine work (no staging; reused by targets + pipeline) ----

    def _do_cut(self, job, *, source_id: str, clip_id: str, params: dict) -> str:
        media_path, _ = self.source_paths(source_id)
        if not media_path:
            raise ValueError(f"source {source_id!r} has no downloaded media to clip")
        start, end = float(params["start"]), float(params["end"])
        job.clip_id = clip_id
        self.write_clip_meta(clip_id, source_id=source_id, start=start, end=end)
        out = str(self.clip_dir(clip_id) / "clip.mp4")
        cutter.cut(media_path, start, end, out, **self._hooks(job))
        return out

    def _do_reframe(self, job, *, clip_id: str, params: dict) -> dict:
        d = self.clip_dir(clip_id)
        meta = self.load_clip_meta(clip_id) or {}
        if meta.get("source_id"):
            job.source_id = meta["source_id"]
        clip_path = str(d / "clip.mp4")

        rois = params.get("rois")
        if rois:
            # the studio sends fractional ROIs (0–1); the engine crops in source pixels.
            w, h = reframe.probe_dimensions(clip_path)
            roi_l, roi_r = _scale_roi(rois["left"], w, h), _scale_roi(rois["right"], w, h)
        else:
            faces = reframe.detect_faces(clip_path, frame_path=str(d / "frame.jpg"))
            roi_l, roi_r = faces["rois"]["left"], faces["rois"]["right"]

        edited = _clean_segments(params.get("segments"))
        if edited:
            # a hand-edited speaker track (drag/flip in S7) renders verbatim — no diar⊕ROI.
            track = {"segments": edited, "roiL": roi_l, "roiR": roi_r, "source": "manual"}
        else:
            words_path = self.source_paths(meta["source_id"])[1] if meta.get("source_id") else None
            diar = self.diarization_from_words(words_path)
            track = reframe.speaker_track(
                clip_path, roi_left=roi_l, roi_right=roi_r, diarization=diar,
                min_dwell=float(params.get("min_dwell", 1.0)), smoothing=params.get("smoothing"),
                work_dir=str(d), **self._hooks(job),
            )
        (d / "track.json").write_text(json.dumps(track))
        out = str(d / "reframed.mp4")
        reframe.render(clip_path, track, aspect=params.get("aspect", "9:16"),
                       mode=params.get("mode", "pan"), crop_margin=float(params.get("crop_margin", 0.0)),
                       out_path=out, **self._hooks(job))
        return {"reframed_path": out, "track": track}

    def _do_caption(self, job, *, clip_id: str, params: dict) -> dict:
        d = self.clip_dir(clip_id)
        meta = self.load_clip_meta(clip_id) or {}
        if meta.get("source_id"):
            job.source_id = meta["source_id"]
        words_path = self.source_paths(meta["source_id"])[1] if meta.get("source_id") else None
        if not words_path:
            raise ValueError(f"clip {clip_id!r} has no source transcript to caption from")
        ass = str(d / "captions.ass")
        captioner.generate(words_path, clip_start=float(meta["start"]), clip_end=float(meta["end"]),
                           style=params.get("style", "opus"), out_ass_path=ass)
        video_in = self._stage_input(d, ("reframed.mp4", "clip.mp4"))
        out = str(d / "captioned.mp4")
        captioner.burn(video_in, ass, out, **self._hooks(job))
        return {"ass_path": ass, "captioned_path": out}

    def _do_export(self, job, *, clip_id: str, render_id: str, params: dict) -> str:
        d = self.clip_dir(clip_id)
        meta = self.load_clip_meta(clip_id) or {}
        if meta.get("source_id"):
            job.source_id = meta["source_id"]
        video_in = self._stage_input(d, ("captioned.mp4", "reframed.mp4", "clip.mp4"))
        renders = d / "renders"
        renders.mkdir(parents=True, exist_ok=True)
        out = str(renders / f"{render_id}.mp4")
        exporter.export(video_in, preset=params.get("preset", "tiktok"),
                        fast=bool(params.get("fast", True)), out_path=out, **self._hooks(job))
        return out

    # ----- work-closure builders (one per ClipJob kind) -----------------

    def find_moments_target(self, *, source_id: str, params: dict):
        def _work(job):
            _, words_path = self.source_paths(source_id)
            cands = moments.find_moments(
                words_path,
                mode=params.get("mode", "funny"),
                count=int(params.get("count", 5)),
                transcript_window=params.get("window"),
                source_id=source_id,
            )
            job.result = {"candidates": cands, "count": len(cands),
                          "mode": params.get("mode", "funny")}
        return _work

    def cut_target(self, *, source_id: str, clip_id: str, params: dict):
        def _work(job):
            def body():
                out = self._do_cut(job, source_id=source_id, clip_id=clip_id, params=params)
                job.result = {"clip_id": clip_id, "clip_path": out,
                              "start": float(params["start"]), "end": float(params["end"])}
            self._cancellable(job, body)
        return _work

    def reframe_target(self, *, clip_id: str, params: dict):
        def _work(job):
            def body():
                r = self._do_reframe(job, clip_id=clip_id, params=params)
                track = r["track"]
                job.result = {"clip_id": clip_id, "reframed_path": r["reframed_path"],
                              "aspect": params.get("aspect", "9:16"),
                              "mode": params.get("mode", "pan"),
                              "source": track.get("source"), "segments": track.get("segments", [])}
            self._cancellable(job, body)
        return _work

    def caption_target(self, *, clip_id: str, params: dict):
        def _work(job):
            def body():
                r = self._do_caption(job, clip_id=clip_id, params=params)
                job.result = {"clip_id": clip_id, "ass_path": r["ass_path"],
                              "captioned_path": r["captioned_path"],
                              "style": params.get("style", "opus")}
            self._cancellable(job, body)
        return _work

    def export_target(self, *, clip_id: str, render_id: str, params: dict):
        def _work(job):
            def body():
                out = self._do_export(job, clip_id=clip_id, render_id=render_id, params=params)
                job.result = {"clip_id": clip_id, "render_id": render_id, "output_path": out,
                              "preset": params.get("preset", "tiktok")}
            self._cancellable(job, body)
        return _work

    def pipeline_target(self, *, source_id: str, clip_id: str, render_id: str, params: dict):
        """One-shot ingest→cut→reframe→caption→export, staged for progress (spec §4
        ``render.pipeline``). The API caller supplies all params up front; the MCP layer
        adds elicitation pauses on top of this same chain (next task)."""
        def _work(job):
            def body():
                cm = self.clip_manager
                cm.update_progress(job.id, 5, stage="cut")
                self._do_cut(job, source_id=source_id, clip_id=clip_id, params=params)
                cm.update_progress(job.id, 30, stage="reframe")
                self._do_reframe(job, clip_id=clip_id, params=params)
                cm.update_progress(job.id, 65, stage="caption")
                self._do_caption(job, clip_id=clip_id, params=params)
                cm.update_progress(job.id, 90, stage="export")
                out = self._do_export(job, clip_id=clip_id, render_id=render_id, params=params)
                job.result = {"clip_id": clip_id, "render_id": render_id, "output_path": out,
                              "aspect": params.get("aspect", "9:16"),
                              "preset": params.get("preset", "tiktok"),
                              "style": params.get("style", "opus")}
            self._cancellable(job, body)
        return _work
