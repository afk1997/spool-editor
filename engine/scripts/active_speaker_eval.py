"""Active-speaker fusion harness — does audio diarization improve the auto-pan's
speaker tracking, without regressing framing? (item B acceptance)

Runs the REAL per-shot face tracker (clip/face_track.track) on a cut clip BOTH ways —
video-only and with the audio diarization fused in — and reports:

  * shots / multi-face shots          — how much of the clip even exercises the tie-break
  * timeline divergence               — how many sampled times the two pans differ (B is active)
  * speaker-side consistency          — for each diar speaker, the fraction of its talking time the
                                         pan stayed on that speaker's majority side (higher = the
                                         pan tracks turns cleanly; distinct speakers on opposite
                                         sides = the two-shot is being framed on the talker)
  * reframe_eval on both renders       — face_present / center_dx / jitter MUST NOT regress

Run: .venv/bin/python scripts/active_speaker_eval.py <source.mp4> <start> <end> [--render]

Uses the production path (cutter.cut, face_track.track, reframe.render) + the source's
.words.json for diarization turns. Without --render it skips the (slow) encode + reframe_eval
and reports the timeline analysis only.
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)
from clip import cutter, face_track, reframe  # the REAL engine


def _turns_from_words(words_json, clip_start, dur):
    try:
        data = json.load(open(words_json))
    except (OSError, ValueError):
        return []
    raw = [{"start": s["start"], "end": s["end"], "speaker": s.get("speaker")}
           for s in data.get("segments", [])
           if s.get("speaker") and s.get("start") is not None and s.get("end") is not None]
    return face_track.rebase_diarization(raw, clip_start, dur)


def _pan_side(point, src_w):
    """left/right of the crop center for a timeline point (t, x, y, w, h, snap)."""
    cx = (point[1] + point[3] / 2) / src_w
    return "left" if cx < 0.5 else "right"


def _speaker_side_consistency(timeline, diar, src_w):
    """For each diar speaker, the purity of the pan's side during its turns (max share of one side).
    Returns (mean_purity, per_speaker_majority_side). Higher purity + distinct sides = the pan
    tracks each speaker to a stable, separate side."""
    by_spk = {}
    for (t, x, y, w, h, _snap) in timeline:
        spk = next((d["speaker"] for d in diar if d["start"] <= t < d["end"]), None)
        if spk is None:
            continue
        by_spk.setdefault(spk, []).append(_pan_side((t, x, y, w, h, _snap), src_w))
    purities, sides = [], {}
    for spk, s in by_spk.items():
        if not s:
            continue
        left = s.count("left")
        maj = "left" if left >= len(s) - left else "right"
        purities.append(max(left, len(s) - left) / len(s))
        sides[spk] = maj
    return (round(statistics.mean(purities), 3) if purities else None), sides


def _reframe_eval(path):
    out = subprocess.run([sys.executable, os.path.join(ENGINE, "scripts", "reframe_eval.py"), path],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"error": out.stderr.strip()[-200:]}


def main():
    src = os.path.abspath(sys.argv[1])
    start, end = float(sys.argv[2]), float(sys.argv[3])
    do_render = "--render" in sys.argv
    dur = end - start
    words_json = os.path.splitext(src)[0] + ".words.json"

    work = tempfile.mkdtemp(prefix="aseval.")
    clip = os.path.join(work, "clip.mp4")
    cutter.cut(src, start, end, clip)
    src_w, src_h = reframe.probe_dimensions(clip)
    out_w, out_h = reframe.aspect_dims("9:16")

    diar = _turns_from_words(words_json, start, dur)
    tl_video = face_track.track(clip, dur, src_w, src_h, out_w, out_h, diarization=None)
    tl_audio = face_track.track(clip, dur, src_w, src_h, out_w, out_h, diarization=diar)

    # timeline divergence (align by nearest time)
    diverged = 0
    if tl_video and tl_audio:
        va = {round(p[0], 1): p for p in tl_audio}
        for p in tl_video:
            q = va.get(round(p[0], 1))
            if q and abs((p[1] + p[3] / 2) - (q[1] + q[3] / 2)) > src_w * 0.03:
                diverged += 1

    v_pur, v_sides = _speaker_side_consistency(tl_video, diar, src_w)
    a_pur, a_sides = _speaker_side_consistency(tl_audio, diar, src_w)

    result = {
        "source": os.path.basename(src), "window": [start, end],
        "diar_turns": len(diar), "speakers": sorted({d["speaker"] for d in diar}),
        "timeline_points": {"video_only": len(tl_video), "with_audio": len(tl_audio)},
        "diverged_points": diverged,
        "speaker_side_consistency": {
            "video_only": {"mean_purity": v_pur, "sides": v_sides},
            "with_audio": {"mean_purity": a_pur, "sides": a_sides},
        },
        "distinct_sides": {
            "video_only": len(set(v_sides.values())) if v_sides else 0,
            "with_audio": len(set(a_sides.values())) if a_sides else 0,
        },
    }

    if do_render and tl_video and tl_audio:
        track_v = {"segments": [], "roiL": {}, "roiR": {}, "source": "roi"}
        rv, ra = os.path.join(work, "v.mp4"), os.path.join(work, "a.mp4")
        reframe.render(clip, track_v, aspect="9:16", mode="pan", out_path=rv, face_timeline=tl_video)
        reframe.render(clip, track_v, aspect="9:16", mode="pan", out_path=ra, face_timeline=tl_audio)
        result["reframe_eval"] = {"video_only": _reframe_eval(rv), "with_audio": _reframe_eval(ra)}

    print(json.dumps(result, indent=2))
    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
