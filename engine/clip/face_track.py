"""Per-shot face-tracking reframe — auto-follow AND properly frame the speaker.

The diar⊕ROI pan hard-cuts between two *fixed* ROI boxes; on single-camera footage that cuts
between wide shots and speakers, those boxes miss the speaker and read as centered. This module
watches the actual picture and produces a stabilized, well-framed crop that follows the speaker:

  scene cuts (ffmpeg) → per-frame face detection (OpenCV YuNet) → pick the speaker (upper-frame
  + size + stability) → frame it (adaptive zoom so the face fills a target fraction, eyes on the
  rule-of-thirds) → temporally smooth (dead-zone + EMA, hold through dropped detections, snap at
  cuts) → ffmpeg crop expressions (w/h/x/y vary over time).

Degrades gracefully: no OpenCV / no YuNet model / no faces → ``track`` returns ``[]`` and the
caller falls back to the diar⊕ROI 2-ROI pan. Pure helpers are unit-tested; the cv2/ffmpeg I/O is
verified on real media + the quality harness (scripts/reframe_eval.py).
"""
from __future__ import annotations

import os
import re
import subprocess

_MODEL = os.path.join(os.path.dirname(__file__), "models", "face_detection_yunet_2023mar.onnx")

# Framing constants.
_AUDIENCE_CY = 0.62      # faces whose top sits below this are likely foreground audience
_TARGET_FACE_FRAC = 0.34  # frame the face to ~this fraction of the crop height (close/medium)
_Y_THIRD = 0.40           # put the face center at this fraction of the crop height (eyes upper-third)
_MIN_ZOOM_FRAC = 0.6      # never crop tighter than this fraction of full height (cap upscale/blur;
                          # don't chase tiny faces in wide shots — keep them wider instead)


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return os.path.exists(_MODEL)
    except Exception:
        return False


# ---- pure logic (unit-tested) ---------------------------------------

def pick_face(faces, frame_w: int, frame_h: int, prev_cx: float | None = None):
    """Choose the speaker from ``faces`` ``[(cx,cy,w,h,score)]`` (pixels) → that face, or ``None``.
    Prefer faces in the upper part of the frame (foreground audience sits low and can be larger);
    score by area weighted toward the previous pick's x (stability — don't hop between people)."""
    if not faces:
        return None
    upper = [f for f in faces if f[1] / frame_h <= _AUDIENCE_CY] or list(faces)

    def score(f):
        area = f[2] * f[3]
        prox = 1.0 if prev_cx is None else (1.0 - min(1.0, abs(f[0] / frame_w - prev_cx)))
        return area * (0.55 + 0.45 * prox)

    return max(upper, key=score)


def frame_rect(fcx: float, fcy: float, fh: float, src_w: int, src_h: int, out_w: int, out_h: int):
    """Adaptive-zoom + rule-of-thirds crop ``(x, y, w, h)`` (source px) around a face center
    ``(fcx, fcy)`` with face height ``fh``: zoom so the face ≈ target fraction (clamped so it never
    zooms out past full height or in past the blur floor), face centered horizontally and on the
    upper-third line, clamped inside the frame, holding the ``out_w:out_h`` aspect."""
    crop_h = fh / _TARGET_FACE_FRAC
    crop_h = max(src_h * _MIN_ZOOM_FRAC, min(float(src_h), crop_h))
    crop_w = crop_h * out_w / out_h
    if crop_w > src_w:                       # don't exceed the source width; re-derive height
        crop_w = float(src_w)
        crop_h = crop_w * out_h / out_w
    x = max(0.0, min(src_w - crop_w, fcx - crop_w / 2))
    y = max(0.0, min(src_h - crop_h, fcy - crop_h * _Y_THIRD))
    return (round(x, 1), round(y, 1), round(crop_w, 1), round(crop_h, 1))


def smooth_track(points, *, deadzone: float = 0.012, alpha: float = 0.35):
    """EMA-smooth the crop params of ``[(t,x,y,w,h,snap)]`` for a steady shot: a dead-zone on x
    (ignore sub-``deadzone*w`` wobble → no jitter), low-pass on all params, and a hard reset at a
    ``snap`` (scene cut). Returns the smoothed points."""
    out = []
    sx = sy = sw = sh = None
    for (t, x, y, w, h, snap) in points:
        if sx is None or snap:
            sx, sy, sw, sh = x, y, w, h
        else:
            if abs(x - sx) > deadzone * sw:
                sx = sx + alpha * (x - sx)
            sy = sy + alpha * (y - sy)
            sw = sw + alpha * (w - sw)
            sh = sh + alpha * (h - sh)
        out.append((t, round(sx, 1), round(sy, 1), round(sw, 1), round(sh, 1), snap))
    return out


def _expr(points, vi: int) -> str:
    """Nested-if ffmpeg expression (function of ``t``) over a control-point param: lerp within a
    shot, step at a ``snap`` (the point's index-5 flag)."""
    if not points:
        return "0"
    if len(points) == 1:
        return f"{points[0][vi]:.1f}"
    expr = f"{points[-1][vi]:.1f}"
    for i in range(len(points) - 2, -1, -1):
        t0, t1 = points[i][0], points[i + 1][0]
        v0, v1, snap1 = points[i][vi], points[i + 1][vi], points[i + 1][5]
        if snap1 or (t1 - t0) < 1e-3:
            seg = f"{v0:.1f}"
        else:
            seg = f"({v0:.1f}+({v1 - v0:.1f})*(t-{t0:.3f})/{t1 - t0:.3f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr


def crop_exprs(points):
    """``(w_expr, h_expr, x_expr, y_expr)`` for an ffmpeg crop, from ``[(t,x,y,w,h,snap)]``."""
    return _expr(points, 3), _expr(points, 4), _expr(points, 1), _expr(points, 2)


def cluster_by_x(faces, frame_w: int, tol: float = 0.12):
    """Group per-frame face samples ``(t, cx, cy, w, h, patch)`` into *people* by x-proximity
    (within ``tol`` of the frame width), so a shot's faces become one cluster per person across
    time. Used to pick the active speaker among several people."""
    clusters = []  # {"cx": running mean (normalized), "members": [...]}
    for f in sorted(faces, key=lambda f: f[0]):
        cxn = f[1] / frame_w
        best = next((c for c in clusters if abs(c["cx"] - cxn) <= tol), None)
        if best is None:
            clusters.append({"cx": cxn, "members": [f]})
        else:
            best["members"].append(f)
            best["cx"] = sum(m[1] for m in best["members"]) / len(best["members"]) / frame_w
    return [c["members"] for c in clusters]


def mouth_motion(patches) -> float:
    """A person's lip activity = mean absolute frame-to-frame difference of their mouth patches
    (each a flat list of gray values). Higher ⇒ more talking. <2 frames ⇒ 0."""
    if len(patches) < 2:
        return 0.0
    total, n = 0.0, 0
    for a, b in zip(patches, patches[1:]):
        if a and len(a) == len(b):
            total += sum(abs(x - y) for x, y in zip(a, b)) / len(a)
            n += 1
    return total / n if n else 0.0


# ---- I/O (verified on real media) -----------------------------------

def _mouth_patch(cv2, gray, fx: float, fy: float, fw: float, fh: float):
    """A tiny 16×8 grayscale patch of a face's mouth region (lower-middle of the bbox), flattened
    — its frame-to-frame change is the active-speaker (lip-motion) signal."""
    H, W = gray.shape[:2]
    x0, x1 = int(max(0, fx + 0.28 * fw)), int(min(W, fx + 0.72 * fw))
    y0, y1 = int(max(0, fy + 0.58 * fh)), int(min(H, fy + 0.90 * fh))
    if x1 <= x0 or y1 <= y0:
        return []
    return cv2.resize(gray[y0:y1, x0:x1], (16, 8)).flatten().astype(int).tolist()


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


def track(clip_path: str, duration: float, src_w: int, src_h: int, out_w: int, out_h: int,
          *, cancel_check=None):
    """Stabilized crop timeline ``[(t,x,y,w,h,snap)]`` (source px) following the speaker's face
    across the clip. The zoom is **constant per shot** (the shot's median face size → one crop size,
    so it doesn't pulse with per-frame detection noise) and the crop pans (x/y) to follow the face
    within the shot, snapping at each scene cut. ``[]`` when OpenCV/YuNet is unavailable, the decode
    fails, or no face is found — the caller then uses the diar⊕ROI 2-ROI pan."""
    if not available():
        return []
    import glob
    import shutil
    import statistics
    import tempfile
    import cv2

    duration = float(duration or 0)
    if duration <= 0.5:
        return []
    step = 0.4 if duration <= 90 else 0.8
    n = min(400, max(2, int(duration / step) + 1))
    bounds = [0.0] + sorted(scene_cuts(clip_path, duration)) + [duration]

    tmp = tempfile.mkdtemp(prefix="spool-ft.")
    dets = []  # (t, faces, w, h) per sampled frame
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", clip_path, "-vf", f"fps=1/{step}",
             "-frames:v", str(n), os.path.join(tmp, "f%05d.png")],
            check=False, timeout=300,
        )
        frames = sorted(glob.glob(os.path.join(tmp, "f*.png")))
        det = None
        for i, fp in enumerate(frames):
            if cancel_check and cancel_check():
                break
            img = cv2.imread(fp)
            if img is None:
                continue
            h, w = img.shape[:2]
            if det is None:
                det = cv2.FaceDetectorYN.create(_MODEL, "", (w, h), score_threshold=0.6)
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            fl = []  # (cx, cy, w, h, score, mouth_patch)
            for f in (faces if faces is not None else []):
                fx, fy, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                fl.append((fx + fw / 2, fy + fh / 2, fw, fh, float(f[-1]),
                           _mouth_patch(cv2, gray, fx, fy, fw, fh)))
            dets.append((round((i + 0.5) * step, 3), fl, w, h))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not dets:
        return []

    points = []
    found_any = False
    for bi in range(len(bounds) - 1):
        s, e = bounds[bi], bounds[bi + 1]
        shot = [d for d in dets if s <= d[0] < e]
        if not shot:
            continue
        w0, h0 = shot[0][2], shot[0][3]
        # All face samples in the shot → cluster into people, then follow the one TALKING (most
        # mouth motion) when it's a clear winner; else the most prominent upper face. One person per
        # shot → that person (= the camera's subject), so single-face shots behave as before.
        all_faces = [(t, cx, cy, fw, fh, patch)
                     for (t, fl, _w, _h) in shot for (cx, cy, fw, fh, _sc, patch) in fl]
        if not all_faces:
            continue
        clusters = cluster_by_x(all_faces, w0)
        scored = [{
            "members": c,
            "motion": mouth_motion([m[5] for m in sorted(c, key=lambda m: m[0])]),
            "area": statistics.median([m[3] * m[4] for m in c]),
            "cy": statistics.median([m[2] / h0 for m in c]),
        } for c in clusters]
        upper = [c for c in scored if c["cy"] <= _AUDIENCE_CY] or scored
        if len(upper) >= 2:
            by_motion = sorted(upper, key=lambda c: c["motion"], reverse=True)
            talker = (by_motion[0] if by_motion[0]["motion"] > 1.25 * by_motion[1]["motion"] and by_motion[0]["motion"] > 0
                      else max(upper, key=lambda c: c["area"]))
        else:
            talker = upper[0]
        members = {round(m[0], 3): m for m in talker["members"]}  # talker face per sampled time
        face_hs = [m[4] for m in talker["members"]]
        if not face_hs:
            continue
        found_any = True
        _, _, crop_w, crop_h = frame_rect(w0 / 2, h0 / 2, statistics.median(face_hs), w0, h0, out_w, out_h)
        emitted = False
        last_x = last_y = None
        for (t, _fl, _w, _h) in shot:
            m = members.get(round(t, 3))
            if m is None:
                if last_x is None:
                    continue  # leading frames before the talker appears
                x, y = last_x, last_y
            else:
                x = max(0.0, min(w0 - crop_w, m[1] - crop_w / 2))
                y = max(0.0, min(h0 - crop_h, m[2] - crop_h * _Y_THIRD))
                last_x, last_y = x, y
            snap = (not emitted) and bool(points)  # snap at the cut into this shot
            points.append((round(t, 3), round(x, 1), round(y, 1), crop_w, crop_h, snap))
            emitted = True

    if not points or not found_any:
        return []
    if points[0][0] > 0.05:  # cover from t=0 so the expression never extrapolates before frame 1
        f = points[0]
        points.insert(0, (0.0, f[1], f[2], f[3], f[4], False))
    return smooth_track(points)
