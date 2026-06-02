"""Reframe: ROI detection, the diar⊕ROI speaker timeline, and pan/split/center render.

Spool's signature upgrade (spec §1.3, "the diarization⊕ROI win"). The upstream back-half
pans on *video* motion alone and fails on still or off-mic speakers; Spool **fuses**
trove's *audio* diarization (who's speaking, when) with the back-half's *video* ROI
motion (where each face is), then drives the hard-cut pan.

    detect_faces(frame) ─┐
                          ├─▶ speaker_track (diar ⊕ roi_motion) ─▶ render (pan/split/center)
    diarization turns  ──┘

This module is the *analysis* half (detect_faces + speaker_track); render() lands next.
There is no face-detection model — the camera is static within a clip, so two eyeballed
rectangles + cheap ffmpeg motion-differencing are enough. Tiny dependency surface = a feature.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import _ffmpeg

# Vendored back-half (kept verbatim, MIT — THIRD_PARTY_LICENSES.md); invoked as CLI tools.
_ROI_MOTION_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "roi_motion.py")
_PAN_EXPR_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "pan_expr.py")

MOTION_TIMEOUT = 600

# ROI shape: {"x": int, "y": int, "w": int, "h": int} — a face rectangle on the frame.
# SpeakerTrack shape: {"segments": [{"start","end","speaker":"left"|"right"}],
#                      "roiL": ROI, "roiR": ROI, "source": "fused"|"roi"}


def probe_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-200:]}")
    try:
        w, h = out.stdout.strip().split("\n")[0].split("x")
        return int(w), int(h)
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"could not parse ffprobe dimensions {out.stdout!r}") from e


def detect_faces(clip_path: str, *, at_seconds: float = 1.0, frame_path: str | None = None) -> dict:
    """Seed ROIs on a sample frame. There's no face model — for a 2-person talking-head
    we extract a frame (for the agent/editor to confirm boxes on) and return sensible
    default left/right half-frame rectangles.

    Returns ``{"width", "height", "frame_path", "rois": {"left": ROI, "right": ROI}}``.
    """
    width, height = probe_dimensions(clip_path)
    if frame_path is None:
        fd, frame_path = tempfile.mkstemp(prefix="spool-frame.", suffix=".jpg")
        os.close(fd)
    _ffmpeg.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, at_seconds):.3f}", "-i", clip_path,
         "-frames:v", "1", "-q:v", "2", frame_path],
        timeout=60, label="ffmpeg sample-frame",
    )
    half = width // 2
    return {
        "width": width,
        "height": height,
        "frame_path": frame_path,
        "rois": {
            "left": {"x": 0, "y": 0, "w": half, "h": height},
            "right": {"x": half, "y": 0, "w": width - half, "h": height},
        },
    }


def speaker_track(
    clip_path: str,
    *,
    roi_left: dict,
    roi_right: dict,
    diarization: list[dict] | None = None,
    min_dwell: float = 1.0,
    work_dir: str | None = None,
    cancel_check=None,
    register_proc=None,
) -> dict:
    """Build the fused **diar⊕ROI** speaker timeline for a clip.

    Measures per-ROI motion energy (ffmpeg ``crop`` + ``tblend=difference`` +
    ``signalstats``) → vendored ``roi_motion`` → a *video* L/R timeline. When
    ``diarization`` (audio turns ``[{start, end, speaker}]``) is given, reconciles it
    with the video motion so still/off-mic speakers resolve correctly (``source="fused"``);
    otherwise returns the video timeline (``source="roi"``).
    """
    tmp = work_dir or tempfile.mkdtemp(prefix="spool-track.")
    left_txt = os.path.join(tmp, "motion_left.txt")
    right_txt = os.path.join(tmp, "motion_right.txt")
    _measure_roi_motion(clip_path, roi_left, left_txt, cancel_check=cancel_check, register_proc=register_proc)
    _measure_roi_motion(clip_path, roi_right, right_txt, cancel_check=cancel_check, register_proc=register_proc)

    video_segments = _roi_motion_segments(left_txt, right_txt, min_dwell)

    if diarization:
        segments = _fuse_diar_roi(video_segments, diarization)
        source = "fused"
    else:
        segments = _collapse(video_segments)
        source = "roi"

    return {"segments": segments, "roiL": roi_left, "roiR": roi_right, "source": source}


# ----- internals ------------------------------------------------------------

def _measure_roi_motion(clip_path, roi, out_txt, *, cancel_check=None, register_proc=None):
    """ffmpeg: isolate the ROI, frame-difference it, write per-frame luma-avg (motion)
    in the ``frame:… / lavfi.signalstats.YAVG=…`` form roi_motion parses."""
    vf = (
        f"crop={int(roi['w'])}:{int(roi['h'])}:{int(roi['x'])}:{int(roi['y'])},"
        f"tblend=all_mode=difference,signalstats,"
        f"metadata=print:file={out_txt}"
    )
    _ffmpeg.run(
        ["ffmpeg", "-y", "-i", clip_path, "-an", "-vf", vf, "-f", "null", "-"],
        cancel_check=cancel_check, register_proc=register_proc,
        timeout=MOTION_TIMEOUT, label="ffmpeg roi-motion",
    )


def _roi_motion_segments(left_txt: str, right_txt: str, min_dwell: float) -> list[dict]:
    """Run the vendored roi_motion CLI on the two motion files → L/R segments."""
    out = subprocess.run(
        [sys.executable, _ROI_MOTION_SCRIPT, left_txt, right_txt, str(min_dwell)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"roi_motion failed (rc={out.returncode}): {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout)


def _video_side_for(video_segments: list[dict], start: float, end: float) -> str | None:
    """Which side ('left'/'right') the video motion favors over [start, end] (by overlap)."""
    tally = {"left": 0.0, "right": 0.0}
    for seg in video_segments:
        overlap = min(end, seg["end"]) - max(start, seg["start"])
        if overlap > 0:
            tally[seg["speaker"]] = tally.get(seg["speaker"], 0.0) + overlap
    if tally["left"] == 0.0 and tally["right"] == 0.0:
        return None
    return "left" if tally["left"] >= tally["right"] else "right"


def _fuse_diar_roi(video_segments: list[dict], diarization: list[dict]) -> list[dict]:
    """Fuse audio turns with the video ROI timeline.

    Audio turns are the robust *when/who*; the ROI motion supplies *where* (L/R). We map
    each audio speaker label to the side its turns most overlap in the video timeline,
    then emit a segment per audio turn with that side, collapsing consecutive same-side.
    """
    turns = sorted(
        ({"start": float(t["start"]), "end": float(t["end"]), "speaker": t["speaker"]}
         for t in diarization if t.get("end", 0) > t.get("start", 0)),
        key=lambda t: t["start"],
    )
    if not turns:
        return _collapse(video_segments)

    # 1. speaker label -> accumulated video side preference.
    pref: dict[str, dict[str, float]] = {}
    for t in turns:
        side = _video_side_for(video_segments, t["start"], t["end"])
        if side is None:
            continue
        pref.setdefault(t["speaker"], {"left": 0.0, "right": 0.0})
        pref[t["speaker"]][side] += t["end"] - t["start"]

    # 2. resolve each speaker to a side; keep distinct speakers on opposite sides.
    side_map: dict[str, str] = {}
    taken: set[str] = set()
    # strongest preference first so the most-confident speaker claims its side.
    ordered = sorted(pref.items(), key=lambda kv: -max(kv[1].values()))
    for speaker, sides in ordered:
        want = "left" if sides["left"] >= sides["right"] else "right"
        if want in taken:
            want = "right" if want == "left" else "left"
        side_map[speaker] = want
        taken.add(want)
    # speakers with no video signal: fill remaining side, else default left.
    for t in turns:
        if t["speaker"] not in side_map:
            free = ({"left", "right"} - taken)
            side_map[t["speaker"]] = free.pop() if free else "left"
            taken.add(side_map[t["speaker"]])

    # 3. emit a segment per audio turn with its mapped side, then collapse.
    fused = [{"start": t["start"], "end": t["end"], "speaker": side_map[t["speaker"]]} for t in turns]
    return _collapse(fused)


def _collapse(segments: list[dict]) -> list[dict]:
    """Merge consecutive same-side segments (and copy, so inputs aren't mutated)."""
    out: list[dict] = []
    for seg in segments:
        s = {"start": float(seg["start"]), "end": float(seg["end"]), "speaker": seg["speaker"]}
        if out and out[-1]["speaker"] == s["speaker"]:
            out[-1]["end"] = s["end"]
        else:
            out.append(s)
    return out


def render(clip_path: str, track: dict, *, aspect: str = "9:16", mode: str = "pan", out_path: str) -> str:
    """Render the reframed clip (pan/split/center; 9:16/16:9/1:1/4:5). Lands in the
    next commit — drives the crop-x expression from ``pan_expr`` for ``mode='pan'``."""
    raise NotImplementedError("Phase 1 — pan/split/center render (next commit; spec §4 reframe.render)")
