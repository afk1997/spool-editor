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

from process_ownership import run_service_process

from . import _ffmpeg
from . import exporter

# Vendored back-half (kept verbatim, MIT — THIRD_PARTY_LICENSES.md); invoked as CLI tools.
_ROI_MOTION_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "roi_motion.py")
_PAN_EXPR_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "pan_expr.py")

MOTION_TIMEOUT = 600

# ROI shape: {"x": int, "y": int, "w": int, "h": int} — a face rectangle on the frame.
# SpeakerTrack shape: {"segments": [{"start","end","speaker":"left"|"right"}],
#                      "roiL": ROI, "roiR": ROI, "source": "fused"|"roi"}


def probe_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    out = run_service_process(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        popen=subprocess.Popen,
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
    smoothing: int | None = None,
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
    _measure_roi_motion_pair(clip_path, roi_left, roi_right, left_txt, right_txt,
                             cancel_check=cancel_check, register_proc=register_proc)

    video_segments = _roi_motion_segments(left_txt, right_txt, min_dwell, smoothing)

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


def _measure_roi_motion_pair(clip_path, roi_l, roi_r, out_l, out_r, *,
                             cancel_check=None, register_proc=None):
    """One decode for BOTH ROIs: split the input, crop/diff/measure each branch, and let
    ffmpeg write the two metadata files. Decoding dominates a reframe's cost and the old
    per-ROI helper decoded the whole clip twice."""
    def _branch(roi, out_txt, tag):
        return (f"[{tag}]crop={int(roi['w'])}:{int(roi['h'])}:{int(roi['x'])}:{int(roi['y'])},"
                f"tblend=all_mode=difference,signalstats,"
                f"metadata=print:file={out_txt}[{tag}o]")
    fc = f"[0:v]split=2[l][r];{_branch(roi_l, out_l, 'l')};{_branch(roi_r, out_r, 'r')}"
    _ffmpeg.run(
        ["ffmpeg", "-y", "-i", clip_path, "-an", "-filter_complex", fc,
         "-map", "[lo]", "-f", "null", "-",
         "-map", "[ro]", "-f", "null", "-"],
        cancel_check=cancel_check, register_proc=register_proc,
        timeout=MOTION_TIMEOUT, label="ffmpeg roi-motion",
    )


def _roi_motion_segments(left_txt: str, right_txt: str, min_dwell: float,
                         smoothing: int | None = None) -> list[dict]:
    """Run the vendored roi_motion CLI on the two motion files → L/R segments.
    ``smoothing`` (optional) tunes the builder's averaging window (its 4th CLI arg)."""
    argv = [sys.executable, _ROI_MOTION_SCRIPT, left_txt, right_txt, str(min_dwell)]
    if smoothing is not None:
        argv.append(str(int(smoothing)))
    out = run_service_process(
        argv,
        popen=subprocess.Popen,
        capture_output=True,
        text=True,
        timeout=120,
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


def _diar_turns(diarization: list[dict]) -> list[dict]:
    """Sorted, validated audio turns ``[{start, end, speaker}]`` (drops zero/negative spans)."""
    return sorted(
        ({"start": float(t["start"]), "end": float(t["end"]), "speaker": t["speaker"]}
         for t in diarization if t.get("end", 0) > t.get("start", 0)),
        key=lambda t: t["start"],
    )


def diar_speaker_sides(video_segments: list[dict], diarization: list[dict]) -> dict[str, str]:
    """Map each diar speaker label to the screen side ('left'/'right') its turns most overlap in
    the video timeline, keeping distinct speakers on opposite sides.

    The shared core of two fusions: the diar⊕ROI 2-ROI timeline (``_fuse_diar_roi``) and the
    auto-pan face-track active-speaker tie-break (``clip/face_track.track``). ``{}`` when there are
    no turns. ``video_segments`` carry the *where* (which side has motion/a clear talker, when);
    the audio turns carry the robust *who/when*.
    """
    turns = _diar_turns(diarization)
    if not turns:
        return {}

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
    return side_map


def _fuse_diar_roi(video_segments: list[dict], diarization: list[dict]) -> list[dict]:
    """Fuse audio turns with the video ROI timeline.

    Audio turns are the robust *when/who*; the ROI motion supplies *where* (L/R). We map
    each audio speaker label to the side its turns most overlap in the video timeline
    (``diar_speaker_sides``), then emit a segment per audio turn with that side, collapsing
    consecutive same-side.
    """
    turns = _diar_turns(diarization)
    if not turns:
        return _collapse(video_segments)
    side_map = diar_speaker_sides(video_segments, diarization)
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


# Target output dimensions per aspect (1080-wide family — the pan math assumes ~1080p).
_ASPECTS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
_MODES = ("pan", "split", "center")
RENDER_TIMEOUT = 1800


def render(
    clip_path: str,
    track: dict,
    *,
    aspect: str = "9:16",
    mode: str = "pan",
    crop_margin: float = 0.0,
    out_path: str,
    face_timeline=None,
    preview: bool = False,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Render the reframed clip. ``mode`` ∈ {pan, split, center}; ``aspect`` ∈
    {9:16, 16:9, 1:1, 4:5}. ``pan`` follows the speaker: with a ``face_timeline`` (per-shot face
    tracking) the crop-x follows the detected face; otherwise it hard-cuts between the two ROIs
    via the vendored ``pan_expr`` from ``track``. ``split`` stacks both ROIs; ``center`` is a
    centered crop. Returns ``out_path``.

    ``preview`` renders a fast, low-res (640-tall) throwaway so the editor can show the REAL reframe
    at the chosen aspect/mode (what-you-see = what-you-get) instead of a CSS crop approximation —
    ultrafast/low-quality since it's never delivered."""
    if aspect not in _ASPECTS:
        raise ValueError(f"unknown aspect {aspect!r}; expected one of {list(_ASPECTS)}")
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {list(_MODES)}")

    out_w, out_h = _ASPECTS[aspect]
    src_w, src_h = probe_dimensions(clip_path)
    base = ["ffmpeg", "-y", "-i", clip_path]
    # Final intermediate pass → hardware encoder + visually-lossless quality (was an implicit
    # ~CRF 23). A preview is throwaway, so encode it tiny + ultrafast instead.
    venc = (["-c:v", "libx264", "-crf", "30", "-preset", "ultrafast"]
            if preview else exporter.intermediate_encode_flags())
    pre = ",scale=-2:640" if preview else ""   # downscale the finished crop for a fast preview

    if mode == "split":
        sf = _split_filter(track, out_w, out_h)
        if preview:
            sf += ";[vout]scale=-2:640[vpre]"
        argv = base + [
            "-filter_complex", sf,
            "-map", "[vpre]" if preview else "[vout]", "-map", "0:a?", *venc, "-c:a", "copy", out_path,
        ]
    else:
        if mode == "pan" and face_timeline:
            vf = _face_pan_vf(face_timeline, src_w, src_h, out_w, out_h)
        elif mode == "pan":
            vf = _pan_vf(track, src_w, src_h, out_w, out_h, crop_margin)
        else:
            vf = _center_vf(src_w, src_h, out_w, out_h)
        argv = base + ["-vf", vf + pre, *venc, "-c:a", "copy", out_path]

    _ffmpeg.run(
        argv,
        cancel_check=cancel_check, register_proc=register_proc,
        timeout=timeout if timeout is not None else RENDER_TIMEOUT,
        cleanup_path=out_path, label=f"ffmpeg reframe {mode}",
    )
    return out_path


def _pan_vf(track: dict, src_w: int, src_h: int, out_w: int, out_h: int, crop_margin: float = 0.0) -> str:
    """A vertical strip (target aspect) that hard-cuts its x to the active speaker.
    ``crop_margin`` (0–0.5) tightens the crop around the speaker (zoom in), keeping aspect;
    crop_margin=0 is the full-height strip (byte-identical to before)."""
    segments = track.get("segments") or []
    m = max(0.0, min(0.5, float(crop_margin)))
    strip_h = max(1, round(src_h * (1.0 - m)))
    strip_w = min(src_w, round(strip_h * out_w / out_h))
    y0 = (src_h - strip_h) // 2

    def left_edge(roi: dict) -> int:
        cx = roi["x"] + roi["w"] / 2
        return int(max(0, min(src_w - strip_w, round(cx - strip_w / 2))))

    if not segments:
        # No speaker timeline → nothing to pan to; fall back to a centered crop.
        return _center_vf(src_w, src_h, out_w, out_h)

    left_x = left_edge(track["roiL"])
    right_x = left_edge(track["roiR"])
    expr = _run_pan_expr(segments, left_x, right_x)
    return f"crop={strip_w}:{strip_h}:x='{expr}':y={y0},scale={out_w}:{out_h}"


def aspect_dims(aspect: str) -> tuple[int, int]:
    """Target (width, height) for an aspect key — used by the face-tracking reframe."""
    return _ASPECTS[aspect]


def _face_pan_vf(face_timeline, src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    """Crop that follows the speaker's face with adaptive zoom + rule-of-thirds framing: w/h/x/y
    all vary over time (per-shot framing + stabilization from face_track), then scale to aspect."""
    from clip import face_track
    w_e, h_e, x_e, y_e = face_track.crop_exprs(face_timeline)
    return f"crop=w='{w_e}':h='{h_e}':x='{x_e}':y='{y_e}',scale={out_w}:{out_h},setsar=1"


def _center_vf(src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    """Centered crop to the target aspect, then scale."""
    crop_w = min(src_w, round(src_h * out_w / out_h))
    crop_h = min(src_h, round(crop_w * out_h / out_w))
    x = (src_w - crop_w) // 2
    y = (src_h - crop_h) // 2
    return f"crop={crop_w}:{crop_h}:{x}:{y},scale={out_w}:{out_h}"


def _split_filter(track: dict, out_w: int, out_h: int) -> str:
    """Both ROIs stacked — left speaker on top, right on the bottom (spec Step 4b)."""
    half = out_h // 2
    L, R = track["roiL"], track["roiR"]
    return (
        f"[0:v]crop={int(L['w'])}:{int(L['h'])}:{int(L['x'])}:{int(L['y'])},scale={out_w}:{half}[top];"
        f"[0:v]crop={int(R['w'])}:{int(R['h'])}:{int(R['x'])}:{int(R['y'])},scale={out_w}:{half}[bot];"
        f"[top][bot]vstack=inputs=2[vout]"
    )


def _run_pan_expr(segments: list[dict], left_x: int, right_x: int) -> str:
    """Vendored pan_expr CLI: segments + the two strip x-coords → ffmpeg crop-x expr."""
    fd, seg_path = tempfile.mkstemp(prefix="spool-pan-segs.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(segments, f)
        out = run_service_process(
            [sys.executable, _PAN_EXPR_SCRIPT, seg_path, str(left_x), str(right_x)],
            popen=subprocess.Popen,
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            raise RuntimeError(f"pan_expr failed (rc={out.returncode}): {out.stderr.strip()[-300:]}")
        return out.stdout.strip()
    finally:
        try:
            os.unlink(seg_path)
        except OSError:
            pass
