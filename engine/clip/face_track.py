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


# Each crop param becomes one nested-if expression — one `if()` per kept keyframe. ffmpeg's
# expression parser rejects a crop expression past ~80-100 nested if() lerps ("Missing )" /
# "too many args"), which on a long clip silently produced a 0-byte reframe. Keep each param's
# keyframe count safely under that; reduction is per-param, so a static dimension (w/h) costs
# almost nothing while a panning one (x/y) spends the budget where the motion is.
_MAX_KEYFRAMES = 50


def _reduce_points(points, vi: int, *, tol: float = 1.5):
    """Drop keyframes redundant for param ``vi`` so its nested-if expression stays small.

    Shape-preserving: a point is dropped only when its value lies (within ``tol`` px) on the
    straight line between the last kept point and the next one — lossless for flat/linear runs
    (a per-shot-constant zoom or a steady pan). Endpoints and every hard-cut ``snap`` boundary
    (index-5 flag, and the point right before it) are always kept. If a continuously-curving pan
    still exceeds ``_MAX_KEYFRAMES``, the remaining (non-forced) points are uniformly decimated to
    the budget — a graceful quality floor that guarantees the expression always parses."""
    n = len(points)
    if n <= 2:
        return list(points)
    kept = [0]
    for i in range(1, n - 1):
        if points[i][5] or points[i + 1][5]:      # a hard cut here or next → keep the boundary
            kept.append(i)
            continue
        p, nxt = points[kept[-1]], points[i + 1]
        span = nxt[0] - p[0]
        proj = p[vi] + (nxt[vi] - p[vi]) * ((points[i][0] - p[0]) / span) if span > 1e-9 else p[vi]
        if abs(points[i][vi] - proj) > tol:        # deviates from the line → a real control point
            kept.append(i)
    kept.append(n - 1)

    if len(kept) > _MAX_KEYFRAMES:
        forced = {kept[0], kept[-1]} | {i for i in kept if points[i][5]}
        free = [i for i in kept if i not in forced]
        budget = max(0, _MAX_KEYFRAMES - len(forced))
        if budget < len(free):
            step = len(free) / budget
            free = [free[min(len(free) - 1, int(k * step))] for k in range(budget)]
        kept = sorted(forced.union(free))
    return [points[i] for i in kept]


def _expr(points, vi: int) -> str:
    """Nested-if ffmpeg expression (function of ``t``) over a control-point param: lerp within a
    shot, step at a ``snap`` (the point's index-5 flag). Keyframes are reduced per-param first so
    the expression can't overflow ffmpeg's parser on a long clip (see ``_reduce_points``)."""
    if not points:
        return "0"
    points = _reduce_points(points, vi)
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


# ---- active-speaker selection: video authority + an audio (diarization) tie-break ------------
# A shot may show several faces (a wide two-shot). Mouth motion picks the talker when one face is
# clearly moving more; when it's ambiguous (no clear winner) we fall back — historically to the
# largest/most-prominent face, which in a two-shot can be the LISTENER. Item B lets the audio
# diarization break that tie: the face on the audio-active speaker's screen side. Video stays the
# authority — a clear visual winner is never overridden — so bad diarization can't hurt the pan.

def _score_clusters(shot, w0: int, h0: int):
    """Cluster a shot's face samples into people and score each: mouth motion, median bbox area,
    median normalized center (cy for audience filtering, cx for the audio-side tie-break).

    Returns ``(scored, upper)``; ``upper`` drops likely foreground/audience faces (cy below
    ``_AUDIENCE_CY``), falling back to all clusters if that would empty the list."""
    import statistics
    all_faces = [(t, cx, cy, fw, fh, patch)
                 for (t, fl, _w, _h) in shot for (cx, cy, fw, fh, _sc, patch) in fl]
    if not all_faces:
        return [], []
    clusters = cluster_by_x(all_faces, w0)
    scored = [{
        "members": c,
        "motion": mouth_motion([m[5] for m in sorted(c, key=lambda m: m[0])]),
        "area": statistics.median([m[3] * m[4] for m in c]),
        "cy": statistics.median([m[2] / h0 for m in c]),
        "cx": statistics.median([m[1] / w0 for m in c]),
    } for c in clusters]
    upper = [c for c in scored if c["cy"] <= _AUDIENCE_CY] or scored
    return scored, upper


def _clear_motion_winner(upper):
    """The UNAMBIGUOUS mouth-motion talker among ≥2 clusters (top motion > 1.25× the runner-up and
    > 0), else None. This is the confident visual pick — it always wins and teaches the audio→side
    map, so audio only ever resolves the shots video is unsure about."""
    if len(upper) < 2:
        return None
    by_motion = sorted(upper, key=lambda c: c["motion"], reverse=True)
    if by_motion[0]["motion"] > 1.25 * by_motion[1]["motion"] and by_motion[0]["motion"] > 0:
        return by_motion[0]
    return None


def _pick_cluster_on_side(upper, side):
    """The cluster furthest to ``side`` by normalized center x. None if side invalid / list empty."""
    if not upper or side not in ("left", "right"):
        return None
    return min(upper, key=lambda c: c["cx"]) if side == "left" else max(upper, key=lambda c: c["cx"])


def select_talker(upper, *, want_side: str | None = None):
    """Pick the active-speaker cluster among ``upper``.

    Video is the authority: a CLEAR mouth-motion winner always wins, so a bad audio guess can never
    override a confident visual pick. Only when motion is ambiguous does the audio-active speaker's
    side (``want_side``, from diarization) break the tie. With no audio signal (``want_side`` None)
    this is byte-identical to the prior behavior — the largest/most-prominent face. A single cluster
    (single-camera shot) is returned unchanged."""
    if not upper:
        return None
    if len(upper) < 2:
        return upper[0]
    clear = _clear_motion_winner(upper)
    if clear is not None:
        return clear                                     # confident visual winner — audio can't override
    if want_side is not None:                            # ambiguous → let audio break the tie
        on_side = _pick_cluster_on_side(upper, want_side)
        if on_side is not None:
            return on_side
    return max(upper, key=lambda c: c["area"])           # prior fallback: most prominent face


def rebase_diarization(turns, clip_start: float, duration: float):
    """Re-base SOURCE-relative audio turns to the cut clip's timeline (the clip starts at 0),
    clipping to [0, duration] and dropping turns outside the window.

    The transcript's speaker turns are in source time, but ``track`` measures everything on the cut
    clip (clip-relative). Without this shift, turns from a clip cut deep into a source never overlap
    the clip's shots and the audio tie-break silently never fires."""
    out = []
    for t in (turns or []):
        s = max(0.0, float(t["start"]) - clip_start)
        e = min(float(duration), float(t["end"]) - clip_start)
        if e > s:
            out.append({"start": s, "end": e, "speaker": t["speaker"]})
    return out


def _audio_side_for_window(diarization, side_map, start: float, end: float):
    """Screen side ('left'/'right') of the diar speaker most active over [start, end], via
    ``side_map`` (speaker → side). None when no turn overlaps or that speaker has no mapped side."""
    tally: dict = {}
    for t in (diarization or []):
        ov = min(end, float(t["end"])) - max(start, float(t["start"]))
        if ov > 0:
            tally[t["speaker"]] = tally.get(t["speaker"], 0.0) + ov
    if not tally:
        return None
    return side_map.get(max(tally, key=tally.get))


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
          *, cancel_check=None, diarization=None):
    """Stabilized crop timeline ``[(t,x,y,w,h,snap)]`` (source px) following the speaker's face
    across the clip. The zoom is **constant per shot** (the shot's median face size → one crop size,
    so it doesn't pulse with per-frame detection noise) and the crop pans (x/y) to follow the face
    within the shot, snapping at each scene cut. ``[]`` when OpenCV/YuNet is unavailable, the decode
    fails, or no face is found — the caller then uses the diar⊕ROI 2-ROI pan.

    ``diarization`` (optional audio turns ``[{start, end, speaker}]``) only breaks the tie in
    ambiguous multi-face shots: shots with a CLEAR mouth-motion winner teach a speaker→screen-side
    map, and the audio-active speaker's side then picks the talker where motion is unclear (a wide
    two-shot, or a speaker briefly off-mic-visually). Video stays the authority — a confident visual
    pick is never overridden — so bad diarization can't degrade the pan. ``None`` → behavior is
    byte-identical to the video-only tracker."""
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

    # Group sampled detections into shots (one window per scene-cut segment) once; both the
    # side-map pass and the emission pass iterate it.
    shots = []
    for bi in range(len(bounds) - 1):
        s, e = bounds[bi], bounds[bi + 1]
        sh = [d for d in dets if s <= d[0] < e]
        if sh:
            shots.append((s, e, sh))

    # Pass 1 (only with diarization): learn the speaker→screen-side map from shots that have a
    # CLEAR visual winner. Those confident picks are the *where*; the audio turns are the *who*.
    # Audio is then used ONLY to resolve ambiguous shots in pass 2 — never to override a clear
    # visual pick — so a wrong diarization label can't move a confidently-framed shot.
    side_map = {}
    if diarization:
        video_segs = []
        for (s, e, sh) in shots:
            w0, h0 = sh[0][2], sh[0][3]
            _scored, upper = _score_clusters(sh, w0, h0)
            winner = _clear_motion_winner(upper)
            if winner is not None:
                video_segs.append({"start": s, "end": e,
                                   "speaker": "left" if winner["cx"] < 0.5 else "right"})
        if video_segs:
            from clip import reframe  # local import avoids a module-load cycle
            side_map = reframe.diar_speaker_sides(video_segs, diarization)

    points = []
    found_any = False
    for (s, e, shot) in shots:
        w0, h0 = shot[0][2], shot[0][3]
        # Cluster the shot's faces into people, then follow the one TALKING (clear mouth-motion
        # winner). When motion is ambiguous, the audio-active speaker's side breaks the tie (item B);
        # with no diarization, the most prominent face — single-face shots behave as before.
        _scored, upper = _score_clusters(shot, w0, h0)
        if not upper:
            continue
        want_side = (_audio_side_for_window(diarization, side_map, s, e)
                     if (diarization and side_map) else None)
        talker = select_talker(upper, want_side=want_side)
        if talker is None:
            continue
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
