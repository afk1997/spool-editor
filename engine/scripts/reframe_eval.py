"""Reframe quality harness — score a reframed .mp4 by detecting faces in the OUTPUT.

Run: .venv/bin/python scripts/reframe_eval.py <reframed.mp4> [step_seconds]

Metrics (higher face_present is better; for the rest, steadier/closer-to-target is better):
  face_present%   — fraction of sampled frames with a detected face (the speaker is in frame)
  face_h_frac     — mean face-height / frame-height (target ~0.30–0.42 = a proper close/medium)
  size_std        — std of face_h_frac (low = consistent zoom, not pumping)
  center_dx       — mean |face_cx − 0.5| (low = horizontally centered)
  y_pos           — mean face_cy (target ~0.34–0.42 = eyes on the upper third)
  jitter          — mean |Δ face_cx| between consecutive detected frames (low = stable, no shake)
"""
import json
import os
import subprocess
import sys
import tempfile

import cv2

MODEL = os.path.join(os.path.dirname(__file__), "..", "clip", "models", "face_detection_yunet_2023mar.onnx")


def main():
    path = sys.argv[1]
    step = float(sys.argv[2]) if len(sys.argv) > 2 else 0.4
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nk=1:nw=1", path], capture_output=True, text=True).stdout.strip() or 0)
    tmp = tempfile.mkdtemp(prefix="reval.")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vf", f"fps=1/{step}",
                    os.path.join(tmp, "f%05d.png")], check=False)
    frames = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
    det = None
    present = 0
    hf, cxs, cys = [], [], []
    cx_seq = []
    for fn in frames:
        img = cv2.imread(os.path.join(tmp, fn))
        if img is None:
            continue
        h, w = img.shape[:2]
        if det is None:
            det = cv2.FaceDetectorYN.create(MODEL, "", (w, h), score_threshold=0.6)
            det.setInputSize((w, h))
        _, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            cx_seq.append(None)
            continue
        f = max(faces, key=lambda f: f[2] * f[3])  # largest in the OUTPUT = the framed speaker
        present += 1
        hf.append(float(f[3]) / h)
        cx = (float(f[0]) + f[2] / 2) / w
        cxs.append(abs(cx - 0.5))
        cys.append((float(f[1]) + f[3] / 2) / h)
        cx_seq.append(cx)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    def mean(xs):
        return float(sum(xs) / len(xs)) if xs else 0.0

    def std(xs):
        m = mean(xs)
        return float((sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5) if xs else 0.0

    jit = [abs(a - b) for a, b in zip(cx_seq, cx_seq[1:]) if a is not None and b is not None]
    n = len([1 for _ in frames])
    print(json.dumps({
        "clip": os.path.basename(path), "dur": round(dur, 1), "frames": n,
        "face_present_pct": round(100 * present / n, 1) if n else 0,
        "face_h_frac": round(mean(hf), 3), "size_std": round(std(hf), 3),
        "center_dx": round(mean(cxs), 3), "y_pos": round(mean(cys), 3),
        "jitter": round(mean(jit), 4),
    }, indent=2))


if __name__ == "__main__":
    main()
