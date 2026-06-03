"""Per-shot face tracking for the reframe *pan* — auto-follow the real speaker.

The diar⊕ROI pan hard-cuts between two *fixed* ROI boxes; on single-camera footage that cuts
between wide shots and speakers, those boxes miss the speaker and the crop reads as centered.
This module instead watches the actual picture: detect scene cuts, find the dominant (on-stage,
not foreground-audience) face per sampled frame, and emit a smoothed crop-center timeline that
LERPs within a shot and SNAPS at each cut — so the pan follows the speaker through cuts.

It degrades gracefully: if OpenCV isn't importable or no faces are found, ``face_timeline``
returns ``[]`` and the caller falls back to the diar⊕ROI 2-ROI pan. The pure helpers
(``dominant_face_cx``, ``crop_x_expr``) are unit-tested; the cv2/ffmpeg I/O is verified on real
media.
"""
from __future__ import annotations

import os
import re
import subprocess

# Faces whose center sits below this fraction of the frame are treated as foreground audience
# (back-of-head shots) rather than the on-stage speaker, unless nothing else is found.
_AUDIENCE_CY = 0.62


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


# ---- pure logic (unit-tested) ---------------------------------------

def dominant_face_cx(faces, frame_w: int, frame_h: int):
    """The speaker's face → normalized x-center (0–1), or ``None``. ``faces`` is ``[(x,y,w,h)]``.
    Prefer faces in the upper part of the frame (the speaker; foreground audience faces sit low and
    can be larger); among those take the biggest. If none qualify, the largest overall."""
    if not faces:
        return None
    upper = [f for f in faces if (f[1] + f[3] / 2) / frame_h <= _AUDIENCE_CY]
    x, _y, w, _h = max(upper or faces, key=lambda f: f[2] * f[3])
    return (x + w / 2) / frame_w


def _smooth(vals, win: int = 3):
    if len(vals) < 3:
        return list(vals)
    half = win // 2
    return [sum(vals[max(0, i - half):min(len(vals), i + half + 1)])
            / (min(len(vals), i + half + 1) - max(0, i - half)) for i in range(len(vals))]


def crop_x_expr(points, src_w: int, cw: int) -> str:
    """An ffmpeg crop-x expression (function of ``t``) that follows the face-center control points
    ``[(t, cx, snap)]`` (cx normalized 0–1). Within a shot it linearly interpolates between points;
    a point with ``snap=True`` (the start of a new shot) HOLDS the previous x until that time then
    jumps (no pan across a cut). Always clamped to ``[0, src_w-cw]``."""
    lim = float(max(0, src_w - cw))

    def xv(cx: float) -> float:
        return max(0.0, min(lim, cx * src_w - cw / 2))

    if not points:
        return f"{xv(0.5):.1f}"
    if len(points) == 1:
        return f"{xv(points[0][1]):.1f}"
    expr = f"{xv(points[-1][1]):.1f}"  # after the last point, hold
    for i in range(len(points) - 2, -1, -1):
        t0, cx0, _ = points[i]
        t1, cx1, snap1 = points[i + 1]
        x0, x1 = xv(cx0), xv(cx1)
        if snap1 or (t1 - t0) < 1e-3:
            seg = f"{x0:.1f}"  # snap: hold x0 until the cut at t1
        else:
            seg = f"({x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{t1 - t0:.3f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr


# ---- I/O (verified on real media) -----------------------------------

def scene_cuts(clip_path: str, duration: float, threshold: float = 0.3):
    """Cut timestamps (seconds, strictly inside the clip) via ffmpeg scene detection."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", clip_path, "-filter:v",
             f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180,
        )
    except Exception:
        return []
    cuts = []
    for m in re.finditer(r"pts_time:([0-9.]+)", out.stderr):
        try:
            t = float(m.group(1))
        except ValueError:
            continue
        if 0.25 < t < (float(duration) - 0.25):
            cuts.append(round(t, 3))
    return sorted(set(cuts))


def face_timeline(clip_path: str, duration: float, *, cancel_check=None):
    """Smoothed crop-center control points ``[(t, cx, snap)]`` tracking the speaker's face across
    the clip (lerp within a shot, snap at each scene cut). ``[]`` if OpenCV is unavailable, the
    decode fails, or no face is ever found — the caller then uses the diar⊕ROI pan."""
    if not available():
        return []
    import glob
    import shutil
    import tempfile
    import cv2

    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if casc.empty():
        return []
    duration = float(duration or 0)
    if duration <= 0.5:
        return []
    step = 0.6 if duration <= 120 else 1.2
    n = min(240, max(2, int(duration / step) + 1))

    tmp = tempfile.mkdtemp(prefix="spool-ft.")
    raw = []           # (t, cx)
    found_any = False
    last = 0.5
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", clip_path, "-vf", f"fps=1/{step}",
             "-frames:v", str(n), os.path.join(tmp, "f%05d.png")],
            check=False, timeout=240,
        )
        frames = sorted(glob.glob(os.path.join(tmp, "f*.png")))
        for i, fp in enumerate(frames):
            if cancel_check and cancel_check():
                break
            img = cv2.imread(fp)
            if img is None:
                continue
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                          minSize=(int(w * 0.05), int(w * 0.05)))
            cx = dominant_face_cx([tuple(int(v) for v in f) for f in faces], w, h)
            if cx is not None:
                last = cx
                found_any = True
            raw.append((round((i + 0.5) * step, 3), last))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not raw or not found_any:
        return []
    cuts = scene_cuts(clip_path, duration)
    sm = _smooth([c for _, c in raw], 3)
    pts = []
    for k, (t, _) in enumerate(raw):
        prev_t = raw[k - 1][0] if k > 0 else 0.0
        snap = any(prev_t < cut <= t for cut in cuts)
        pts.append((t, round(sm[k], 4), bool(snap)))
    # collapse near-identical consecutive non-snap points so the ffmpeg expression stays short
    out = []
    for p in pts:
        if out and not p[2] and abs(p[1] - out[-1][1]) < 0.012:
            continue
        out.append(p)
    return out
