"""Caption↔audio sync harness — quantify caption drift (ms) on a known clip.

Run: .venv/bin/python scripts/caption_sync_eval.py <source.mp4> <start> <end> [style]

Captions are emitted by ``clip/captioner.py`` at ``(word_time - clip_start)`` and burned
onto the cut. The word's audio actually sits at ``(word_time - clip_TRUE_start)`` in the
clip, where ``clip_TRUE_start`` is where the cut really begins. So the caption drift is a
CONSTANT::

    drift = (word - clip_start) - (word - clip_true_start) = clip_true_start - clip_start

Negative drift = captions appear BEFORE the audio (the keyframe-preroll bug). ~0 = synced.
We measure ``clip_true_start`` by FFT cross-correlation of the rendered clip's audio against
the source (ground truth by signal, not container metadata) — at the cut AND after the
caption burn, proving the whole chain preserves alignment.

Metrics (lower |drift| is better; target |drift| < ~60 ms ≈ inaudible):
  requested_start / true_start_s   — where the clip was asked to begin vs. where it does
  cut_drift_ms                     — caption drift introduced by the cut
  captioned_drift_ms               — caption drift in the final burned clip (the user-visible one)
  clip_dur_s / requested_dur_s     — the clip must not carry preroll (equal = clean)
  first_words                      — per-word: ASS time vs. true audio time in the clip
"""
import json
import os
import subprocess
import sys
import tempfile

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)
from clip import captioner, cutter  # the REAL production engine

_XCORR = os.path.join(ENGINE, "clip", "backhalf", "audio_xcorr.py")


def _to_pcm(src, out, *, ss=None, t=None):
    argv = ["ffmpeg", "-y", "-v", "error"]
    if ss is not None:
        argv += ["-ss", f"{ss:.3f}"]
    argv += ["-i", src]
    if t is not None:
        argv += ["-t", f"{t:.3f}"]
    argv += ["-ac", "1", "-ar", "8000", "-f", "s16le", out]
    subprocess.run(argv, check=True)
    return out


def _duration(path):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", path], capture_output=True, text=True).stdout.strip() or 0)


def _true_start(clip, src, *, win_lo, win_hi):
    """Source-time where ``clip``'s audio begins, searched within source [win_lo, win_hi]."""
    d = tempfile.mkdtemp(prefix="capsync.")
    try:
        cp = _to_pcm(clip, os.path.join(d, "c.pcm"))
        sp = _to_pcm(src, os.path.join(d, "s.pcm"), ss=win_lo, t=max(0.1, win_hi - win_lo))
        res = subprocess.run([sys.executable, _XCORR, cp, sp, f"{win_lo:.3f}"],
                             capture_output=True, text=True)
        return float(res.stdout.strip())
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _first_words(words_json, start, end, n=4):
    try:
        data = json.load(open(words_json))
    except (OSError, ValueError):
        return []
    out = []
    for w in data.get("words", []):
        s, e = w.get("start"), w.get("end")
        if s is None or e is None or w.get("deleted"):
            continue
        if e <= start or s >= end:
            continue
        out.append({"word": (w.get("w") or "").strip(), "src_time": round(float(s), 3)})
        if len(out) >= n:
            break
    return out


def main():
    src = os.path.abspath(sys.argv[1])
    start, end = float(sys.argv[2]), float(sys.argv[3])
    style = sys.argv[4] if len(sys.argv) > 4 else "opus"
    words_json = os.path.splitext(src)[0] + ".words.json"

    work = tempfile.mkdtemp(prefix="capsync-work.")
    clip = os.path.join(work, "clip.mp4")
    cutter.cut(src, start, end, clip)

    win_lo, win_hi = max(0.0, start - 3.0), end + 3.0
    cut_true = _true_start(clip, src, win_lo=win_lo, win_hi=win_hi)

    result = {
        "source": os.path.basename(src),
        "requested_start": round(start, 3),
        "true_start_s": round(cut_true, 3),
        "cut_drift_ms": round((cut_true - start) * 1000, 1),
        "clip_dur_s": round(_duration(clip), 3),
        "requested_dur_s": round(end - start, 3),
    }

    # Run the real caption burn and re-measure on the final clip (proves the burn preserves it).
    if os.path.exists(words_json):
        try:
            ass = os.path.join(work, "captions.ass")
            captioner.generate(words_json, clip_start=start, clip_end=end, style=style, out_ass_path=ass)
            captioned = os.path.join(work, "captioned.mp4")
            captioner.burn(clip, ass, captioned)
            cap_true = _true_start(captioned, src, win_lo=win_lo, win_hi=win_hi)
            result["captioned_drift_ms"] = round((cap_true - start) * 1000, 1)
        except Exception as e:
            result["captioned_drift_ms"] = f"caption failed: {e}"

        fw = _first_words(words_json, start, end)
        for w in fw:
            w["ass_time_s"] = round(w["src_time"] - start, 3)            # what the caption claims
            w["true_audio_s"] = round(w["src_time"] - cut_true, 3)       # where the audio really is
            w["word_drift_ms"] = round((w["ass_time_s"] - w["true_audio_s"]) * 1000, 1)
        result["first_words"] = fw

    import shutil
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
