"""Diarization accuracy BENCHMARK — speaker count + turn boundaries + label
accuracy vs ground truth, across a labelled multi-clip set (item C acceptance).

``diarization_eval.py`` checks speaker COUNT on a single clip. This benchmark
adds the two things C must improve — TURN BOUNDARIES and per-frame LABEL
accuracy — and scores them across several clips, so "more accurate" is a number,
not a vibe. It runs the REAL ``diarizer.diarize`` and is the gate for adopting
any change (a candidate must measurably beat the baseline here to ship).

Ground truth without manual labelling
-------------------------------------
Two kinds of clips, both with KNOWN truth:

  * REAL clips with a known speaker COUNT (zoo monologue = 1, the interview = 2).
  * SYNTHETIC clips built by concatenating REAL single-speaker segments from two
    genuinely different voices, so the turn boundaries + per-segment speaker are
    EXACT by construction. Voice A = the zoo narrator (Jawed); voice B = the
    interview's longest single-speaker runs (171-457s / 604-816s are single
    285s/212s segments — confidently one person). No prior label is trusted for
    SCORING; we only cut from a run we're sure is one speaker.

Frame-level accuracy = 1 - DER(no overlap): bin GT + prediction at 100 ms over
speech frames, try every label permutation, take the best match.

Run:  .venv/bin/python scripts/diarization_bench.py [--json]
Env:  forces TROVE_DIARIZATION=on.
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile

os.environ["TROVE_DIARIZATION"] = "on"
ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)
import diarizer  # noqa: E402

DL = os.path.join(ENGINE, "downloads")
ZOO = os.path.join(DL, "8d3be2fe43.mp4")          # 1 speaker (Jawed), ~19s
INTERVIEW = os.path.join(DL, "032df8e8e5.mp4")    # 2 speakers, ~30min

# Synthetic manifests: each is a list of (source, src_start, src_end, true_spk).
# Boundaries in the BUILT clip are the cumulative segment lengths (exact).
# Voice B segments are taken from the interview's confidently-single-speaker
# long runs (171-457 / 604-816).
SYNTHETIC = {
    # Two long turns, one boundary — easy separability check.
    "synth_2turn": [
        (ZOO, 1.0, 16.0, "A"),
        (INTERVIEW, 180.0, 205.0, "B"),
    ],
    # Alternating turns — turn-boundary precision under switching.
    "synth_4turn": [
        (ZOO, 1.0, 11.0, "A"),
        (INTERVIEW, 185.0, 195.0, "B"),
        (ZOO, 11.0, 18.0, "A"),
        (INTERVIEW, 620.0, 632.0, "B"),
    ],
    # Short turns (~2s) — stresses short-turn handling / label smoothing.
    "synth_shortturns": [
        (ZOO, 1.0, 4.0, "A"),
        (INTERVIEW, 180.0, 183.0, "B"),
        (ZOO, 6.0, 9.0, "A"),
        (INTERVIEW, 200.0, 203.0, "B"),
        (ZOO, 12.0, 15.0, "A"),
        (INTERVIEW, 620.0, 623.0, "B"),
    ],
}


def _seg_wav(src, ss, t, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ss:.3f}", "-i", src,
                    "-t", f"{t:.3f}", "-ac", "1", "-ar", "16000", out], check=True)


def _build_synthetic(manifest, workdir):
    """Concat real single-speaker segments → (wav_path, gt_turns).
    gt_turns = [(start, end, true_spk)] in the BUILT clip's timeline."""
    parts, gt, cursor = [], [], 0.0
    for i, (src, a, b, spk) in enumerate(manifest):
        p = os.path.join(workdir, f"seg{i}.wav")
        _seg_wav(src, a, b - a, p)
        dur = _duration(p)
        parts.append(p)
        gt.append((cursor, cursor + dur, spk))
        cursor += dur
    listf = os.path.join(workdir, "list.txt")
    with open(listf, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    out = os.path.join(workdir, "synth.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", listf, "-ac", "1", "-ar", "16000", out], check=True)
    return out, gt


def _duration(path):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", path], capture_output=True, text=True).stdout.strip() or 0)


def _spk_at(turns, t):
    for s, e, spk in turns:
        if s <= t < e:
            return spk
    return None


def _frame_accuracy(gt_turns, pred_chunks, hop=0.1):
    """Best-permutation frame accuracy over GT speech frames. Returns
    (accuracy, n_frames). Predicted labels are matched to GT labels by the
    permutation that maximizes agreement (diarization labels are arbitrary)."""
    total = max(e for _, e, _ in gt_turns)
    n = int(total / hop)
    gt, pred = [], []
    for i in range(n):
        t = (i + 0.5) * hop
        g = _spk_at(gt_turns, t)
        if g is None:
            continue
        gt.append(g)
        pred.append(_spk_at([(c.start, c.end, c.speaker) for c in pred_chunks], t))
    if not gt:
        return None, 0
    gt_labels = sorted(set(gt))
    pred_labels = sorted(set(x for x in pred if x is not None))
    best = 0
    # Map each GT label to a predicted label (try all injections pred->gt).
    for perm in itertools.permutations(pred_labels, min(len(pred_labels), len(gt_labels))):
        mapping = dict(zip(perm, gt_labels))  # pred_label -> gt_label
        acc = sum(1 for g, p in zip(gt, pred) if mapping.get(p) == g)
        best = max(best, acc)
    return best / len(gt), len(gt)


def _boundary_error(gt_turns, pred_chunks):
    """Mean |offset| (s) from each GT speaker-change to the nearest predicted
    change. Only interior boundaries (speaker actually changes)."""
    gt_bounds = [gt_turns[i][0] for i in range(1, len(gt_turns))
                 if gt_turns[i][2] != gt_turns[i - 1][2]]
    if not gt_bounds:
        return None
    pred_bounds = [pred_chunks[i].start for i in range(1, len(pred_chunks))
                   if pred_chunks[i].speaker != pred_chunks[i - 1].speaker]
    if not pred_bounds:
        return None
    errs = [min(abs(b - pb) for pb in pred_bounds) for b in gt_bounds]
    return sum(errs) / len(errs)


def run(synth_only=False):
    if not diarizer.available():
        print("diarization unavailable"); return {}
    results = {"real": {}, "synthetic": {}}

    # Real count clips (skipped in --synth-only: the 120s interview window is
    # the slow part and the synthetic clips are where label/turn accuracy is
    # actually scored).
    real_clips = [] if synth_only else [("zoo", ZOO, 1), ("interview", INTERVIEW, 2)]
    for name, src, truth in real_clips:
        if not os.path.exists(src):
            results["real"][name] = {"skipped": "missing source"}; continue
        work = tempfile.mkdtemp(prefix="diarbench.")
        wav = os.path.join(work, "a.wav")
        # interview is long; benchmark a representative 4-min window for speed.
        if name == "interview":
            _seg_wav(src, 120.0, 240.0, wav)
        else:
            _seg_wav(src, 0.0, _duration(src), wav)
        chunks = diarizer.diarize(audio_path=wav)
        cnt = len({c.speaker for c in chunks})
        results["real"][name] = {"count": cnt, "truth": truth,
                                 "count_correct": cnt == truth, "turns": len(chunks)}
        import shutil; shutil.rmtree(work, ignore_errors=True)

    # Synthetic turn-boundary + label clips.
    for name, manifest in SYNTHETIC.items():
        if not all(os.path.exists(m[0]) for m in manifest):
            results["synthetic"][name] = {"skipped": "missing source"}; continue
        work = tempfile.mkdtemp(prefix="diarbench.")
        wav, gt = _build_synthetic(manifest, work)
        chunks = diarizer.diarize(audio_path=wav)
        cnt = len({c.speaker for c in chunks})
        truth_cnt = len(set(s for _, _, s in gt))
        acc, nfr = _frame_accuracy(gt, chunks)
        berr = _boundary_error(gt, chunks)
        results["synthetic"][name] = {
            "count": cnt, "truth": truth_cnt, "count_correct": cnt == truth_cnt,
            "turns": len(chunks), "gt_turns": len(gt),
            "frame_accuracy": round(acc, 3) if acc is not None else None,
            "boundary_err_s": round(berr, 3) if berr is not None else None,
        }
        import shutil; shutil.rmtree(work, ignore_errors=True)

    # Aggregate scores.
    real_cnt_ok = sum(1 for v in results["real"].values() if v.get("count_correct"))
    synth = [v for v in results["synthetic"].values() if "frame_accuracy" in v]
    accs = [v["frame_accuracy"] for v in synth if v["frame_accuracy"] is not None]
    berrs = [v["boundary_err_s"] for v in synth if v["boundary_err_s"] is not None]
    results["summary"] = {
        "real_count_correct": f"{real_cnt_ok}/{len(results['real'])}",
        "synth_count_correct": f"{sum(1 for v in synth if v['count_correct'])}/{len(synth)}",
        "mean_frame_accuracy": round(sum(accs) / len(accs), 3) if accs else None,
        "mean_boundary_err_s": round(sum(berrs) / len(berrs), 3) if berrs else None,
    }
    return results


def _use_ecapa():
    """Monkeypatch diarizer to embed with ECAPA-TDNN (candidate 1)."""
    import _ecapa_embed
    diarizer._continuous_embeddings = _ecapa_embed.continuous_embeddings_ecapa
    # ECAPA embeddings are 192-d; the single-embedding fallback in diarize()
    # constructs a 256-d zero array only when there are <2 embeddings, which
    # never reaches clustering — leave it.


if __name__ == "__main__":
    if "--encoder" in sys.argv:
        enc = sys.argv[sys.argv.index("--encoder") + 1]
        if enc == "ecapa":
            _use_ecapa()
            print("# encoder: ECAPA-TDNN (speechbrain)", file=sys.stderr)
    out = run(synth_only="--synth-only" in sys.argv)
    print(json.dumps(out, indent=2))
