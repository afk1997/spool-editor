"""VAD word-realignment harness — does silero-vad realignment reduce
post-silence caption drift on REAL media? (item A acceptance probe)

Background
----------
whisper.cpp (no DTW) localizes words by cross-attention probability, which
drifts EARLY after a silence: the first word after a pause is emitted
~0.3-0.5 s before its real audio onset, so the active-word highlight "races
ahead" of playback. ``transcriber.realign_words_to_vad`` snaps those isolated
post-silence words forward to silero-vad's speech-region boundaries.

This session decoupled realignment from the ``TROVE_DIARIZATION`` flag (it now
runs whenever silero-vad is installed). Per the plan we must MEASURE that it
actually helps on real clips before keeping the change.

Honest measurement (no circularity)
------------------------------------
silero-vad is the *corrector*, so we do NOT use it as the ruler. For every word
that realignment MOVED, we compute the real acoustic onset INDEPENDENTLY with a
plain RMS-energy gate (silence→speech threshold crossing) and report::

    drift = word.start - acoustic_onset      (negative = caption ahead of audio)

then compare |drift| for raw whisper vs. realigned, on exactly the moved words.
If realigned |drift| < raw |drift|, realignment helps → keep it. If not → revert
(the plan's explicit kill-switch).

Run:
  .venv/bin/python scripts/realign_eval.py <source.(mp4|wav)> [start] [end]

Uses the REAL production code path — transcriber.extract_audio /
run_transcribe, diarizer._vad_speech_chunks, transcriber.realign_words_to_vad.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)

import transcriber          # the REAL production transcriber
import diarizer             # the REAL production VAD
import models_store


def _extract_wav(src, dst, *, ss=None, t=None):
    """16 kHz mono WAV (optionally a [ss, ss+t] segment)."""
    argv = ["ffmpeg", "-y", "-v", "error"]
    if ss is not None:
        argv += ["-ss", f"{ss:.3f}"]
    argv += ["-i", src]
    if t is not None:
        argv += ["-t", f"{t:.3f}"]
    argv += ["-ac", "1", "-ar", "16000", dst]
    subprocess.run(argv, check=True)
    return dst


def _load_mono16k(wav_path):
    import librosa
    y, _ = librosa.load(wav_path, sr=16000, mono=True)
    return y, 16000


def _frame_rms(y, sr, t0, t1, *, frame_ms=20, hop_ms=10):
    """RMS envelope over [t0, t1] → (times, rms) at 10 ms hop. Empty if too short."""
    import numpy as np
    i0, i1 = max(0, int(t0 * sr)), min(len(y), int(t1 * sr))
    seg = y[i0:i1]
    frame, hop = int(frame_ms / 1000 * sr), int(hop_ms / 1000 * sr)
    if frame <= 0 or hop <= 0 or len(seg) < frame:
        return np.zeros(0), np.zeros(0)
    rms, times = [], []
    for j in range(0, len(seg) - frame, hop):
        rms.append(float(np.sqrt(np.mean(seg[j:j + frame] ** 2))))
        times.append(t0 + (j + frame / 2) / sr)
    return np.asarray(times), np.asarray(rms)


def _acoustic_onset(y, sr, lo, hi, *, rel_thresh=0.2, sustain_ms=50):
    """Independent silence→speech onset inside [lo, hi] seconds via an RMS gate.

    Returns ``(onset_time, is_clean)`` where ``is_clean`` is True only when the
    100 ms BEFORE the onset is near-silent and the 100 ms after is clearly
    louder (a genuine silence→speech transition — the case the post-silence
    early-word bug actually occurs in). For continuous speech (no real pause),
    ``is_clean`` is False and the onset is unreliable. Uses NO neural VAD.
    Returns ``(None, False)`` when the window has no detectable onset.
    """
    import numpy as np
    times, rms = _frame_rms(y, sr, lo, hi)
    if len(rms) < 3:
        return None, False
    floor = float(np.percentile(rms, 10))
    peak = float(rms.max())
    if peak - floor < 1e-4:            # essentially flat — no onset to find
        return None, False
    thr = floor + rel_thresh * (peak - floor)
    sustain = max(1, int(sustain_ms / 10))
    for k in range(len(rms) - sustain):
        if rms[k] >= thr and bool((rms[k:k + sustain] >= thr).all()):
            onset = float(times[k])
            # Validate a real silence→speech jump around the onset.
            _, pre = _frame_rms(y, sr, onset - 0.15, onset - 0.02)
            _, post = _frame_rms(y, sr, onset + 0.02, onset + 0.15)
            is_clean = (
                len(pre) and len(post)
                and float(post.mean()) > 3.0 * float(pre.mean()) + 1e-5
            )
            return onset, bool(is_clean)
    return None, False


def main():
    src = os.path.abspath(sys.argv[1])
    ss = float(sys.argv[2]) if len(sys.argv) > 2 else None
    end = float(sys.argv[3]) if len(sys.argv) > 3 else None
    t = (end - ss) if (ss is not None and end is not None) else None

    model_path = str(models_store.get_active_path() or "")
    if not model_path or not os.path.exists(model_path):
        print(json.dumps({"error": f"no active whisper model ({model_path!r})"}))
        return

    work = tempfile.mkdtemp(prefix="realign-eval.")
    wav = os.path.join(work, "audio.wav")
    if src.lower().endswith(".wav") and ss is None:
        import shutil
        shutil.copy(src, wav)
    else:
        _extract_wav(src, wav, ss=ss, t=t)

    # 1) REAL whisper → raw words (the timeline as captions would show it today
    #    when realignment does NOT run).
    result = transcriber.run_transcribe(audio_path=wav, model_path=model_path)
    if result.error:
        print(json.dumps({"error": f"transcribe failed: {result.error}"}))
        return
    raw_words = copy.deepcopy(result.words)

    # 2) REAL silero-vad regions + REAL realignment (mutates result.words).
    vad = diarizer._vad_speech_chunks(wav)
    transcriber.realign_words_to_vad(result, vad)
    new_words = result.words

    # 3) For every MOVED word, measure raw vs realigned against an independent
    #    acoustic onset. The post-silence early-word bug only occurs at a real
    #    silence→speech transition (``is_clean``); mid-stream continuous-speech
    #    words have no well-defined onset, so we report them separately and base
    #    the verdict on the clean (post-silence) subset the feature targets.
    y, sr = _load_mono16k(wav)
    moved = []
    for raw, new in zip(raw_words, new_words):
        rs, ns = float(raw["start"]), float(new["start"])
        if abs(rs - ns) < 1e-3:
            continue                       # realignment left it alone
        lo, hi = min(rs, ns) - 0.30, max(rs, ns) + 0.30
        onset, clean = _acoustic_onset(y, sr, max(0.0, lo), hi)
        if onset is None:
            continue                       # no confident onset → can't measure
        rd, nd = rs - onset, ns - onset
        moved.append({
            "word": (raw.get("w") or "").strip(),
            "raw_start": round(rs, 3), "realigned_start": round(ns, 3),
            "acoustic_onset": round(onset, 3), "post_silence": clean,
            "raw_drift_ms": round(rd * 1000, 1),
            "realigned_drift_ms": round(nd * 1000, 1),
        })

    def _stats(rows):
        if not rows:
            return {"n": 0}
        raw_abs = [abs(r["raw_drift_ms"]) for r in rows]
        new_abs = [abs(r["realigned_drift_ms"]) for r in rows]
        rsig = [r["raw_drift_ms"] for r in rows]
        nsig = [r["realigned_drift_ms"] for r in rows]
        mean = lambda xs: round(sum(xs) / len(xs), 1)
        return {
            "n": len(rows),
            "mean_abs_drift_ms": {"raw": mean(raw_abs), "realigned": mean(new_abs)},
            "mean_signed_drift_ms": {"raw": mean(rsig), "realigned": mean(nsig)},
            "improvement_ms": round(mean(raw_abs) - mean(new_abs), 1),
        }

    clean_rows = [r for r in moved if r["post_silence"]]
    clean = _stats(clean_rows)
    verdict = (
        "no_post_silence_moves" if clean["n"] == 0
        else "helps" if clean["improvement_ms"] > 0
        else "no_improvement")
    out = {
        "source": os.path.basename(src),
        "window": [ss, end] if ss is not None else "full",
        "total_words": len(raw_words),
        "vad_regions": len(vad),
        "words_moved_by_realign": sum(
            1 for raw, new in zip(raw_words, new_words)
            if abs(float(raw["start"]) - float(new["start"])) >= 1e-3),
        "post_silence_moved": clean,            # the verdict scope
        "all_moved_words": _stats(moved),        # incl. mid-stream (ruler noisy here)
        "verdict": verdict,
        "moved_words": moved[:25],
    }
    print(json.dumps(out, indent=2))
    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
