"""Glass-box ranking harness — A/B signal-aware vs text-only ranking on REAL media.

Run (from engine/):
  .venv/bin/python scripts/rank_eval.py [--source PATH] [--words PATH] [--mode MODE]
                                        [--count N] [--sample N]

What it proves (the Phase-3 acceptance, spec §5 / §4 discover.rank / §6 glass-box rule):
  1. EXPLAINABLE — every candidate's score decomposes into the five named factors
     (hook / self_contained / arc / energy / length_fit); the table prints the breakdown.
  2. SIGNAL-AWARE — the REAL non-text signals (audio energy + scene density, measured by
     ffmpeg on the real .mp4) change the ranking vs a text-only baseline. We rank the same
     candidates twice — once with the full features, once with audio+scene stripped — and
     report which moments the non-text signals promote.
  3. REWEIGHTABLE — re-ranking with an energy-heavy weight vector reorders the list.

Candidates come from the real moment-finder (codex bridge) by default; ``--sample N`` builds
N deterministic ~18 s transcript windows instead (no LLM — reproducible without codex).
"""
import argparse
import copy
import os
import sys

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)

import transcript_io                       # noqa: E402
from clip import moments, signals          # noqa: E402

DL = os.path.join(ENGINE, "downloads")
INTERVIEW = os.path.join(DL, "032df8e8e5.mp4")   # Karpathy × Zhan, 2 speakers, ~30 min


def _sample_windows(words_path, n, target=18.0):
    """N deterministic ~``target``-second windows built from consecutive transcript segments."""
    data = transcript_io.load(words_path)
    segs = [s for s in (data.get("segments") or []) if s.get("start") is not None]
    out, i = [], 0
    while i < len(segs) and len(out) < n:
        start = float(segs[i]["start"])
        j = i
        while j < len(segs) and float(segs[j]["end"]) - start < target:
            j += 1
        end = float(segs[min(j, len(segs) - 1)]["end"])
        out.append({"start": round(start, 2), "end": round(end, 2), "title": f"win@{int(start)}s",
                    "rationale": "", "mode": "sample", "signals": []})
        i = j + 1
    return out


def _label(c):
    return f'{c["start"]:7.1f}–{c["end"]:<7.1f} {(c.get("title") or "")[:34]:34}'


def _order(ranked):
    return [c["_id"] for c in ranked]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=INTERVIEW)
    ap.add_argument("--words", default=None)
    ap.add_argument("--mode", default="insightful")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--sample", type=int, default=0, help="skip the LLM; build N transcript windows")
    args = ap.parse_args()

    words_path = args.words or os.path.splitext(args.source)[0] + ".words.json"
    if not os.path.exists(words_path):
        sys.exit(f"no transcript at {words_path}")

    if args.sample:
        cands = _sample_windows(words_path, args.sample)
        print(f"candidates: {len(cands)} sampled ~18s windows (no LLM)")
    else:
        print(f"find_moments(mode={args.mode}, count={args.count}) via the codex bridge …")
        cands = moments.find_moments(words_path, mode=args.mode, count=args.count)
        print(f"candidates: {len(cands)} from the moment-finder")
    if not cands:
        sys.exit("no candidates")

    # Attach the REAL glass-box signals (real ffmpeg audio energy + scene density on the .mp4).
    words = (transcript_io.load(words_path).get("words")) or None
    signals.annotate(cands, words=words, media_path=args.source)
    for i, c in enumerate(cands):
        c["_id"] = i

    # --- A/B: signal-aware (full features) vs text-only (audio+scene stripped) ---------------
    text_only = copy.deepcopy(cands)
    for c in text_only:
        c.get("features", {}).pop("audio", None)
        c.get("features", {}).pop("scene_density", None)

    full_ranked = moments.rank(copy.deepcopy(cands))
    text_ranked = moments.rank(text_only)
    energy_ranked = moments.rank(copy.deepcopy(cands), weights={"energy": 1.0})

    print("\n=== SIGNAL-AWARE ranking (default weights) — every score = Σ named factors ===")
    print(f'{"#":>2}  {"window":<16}{"title":34} {"score":>6}   '
          f'{"hook":>5} {"self":>5} {"arc":>5} {"enrgy":>5} {"len":>5}   audio/scene')
    for rank_i, c in enumerate(full_ranked, 1):
        f = c["factors"]
        feats = c.get("features", {})
        au = feats.get("audio") or {}
        a = f'dyn={au.get("dynamic_db","–")} max={au.get("max_db","–")}' if au else "no-audio"
        sc = feats.get("scene_density")
        print(f'{rank_i:>2}  {_label(c)} {c["score"]:>6}   '
              f'{f["hook"]:>5} {f["self_contained"]:>5} {f["arc"]:>5} {f["energy"]:>5} {f["length_fit"]:>5}   '
              f'{a}{f" scene={sc}" if sc is not None else ""}')

    # How much did the NON-TEXT signals reorder the list?
    full_o, text_o = _order(full_ranked), _order(text_ranked)
    moved = sum(1 for a, b in zip(full_o, text_o) if a != b)
    by_id = {c["_id"]: c for c in cands}
    promoted = [cid for cid in full_o if full_o.index(cid) < text_o.index(cid)]

    print(f"\n=== A/B vs TEXT-ONLY baseline ===")
    print(f"text-only order : {text_o}")
    print(f"signal-aware    : {full_o}")
    print(f"positions changed by adding audio+scene signals: {moved}/{len(cands)}")
    for cid in promoted:
        c = by_id[cid]
        au = (c.get("features", {}).get("audio") or {})
        print(f"  ↑ promoted {text_o.index(cid)+1}→{full_o.index(cid)+1}: "
              f'{_label(c)}  dynamic_db={au.get("dynamic_db","–")} '
              f'scene={c.get("features", {}).get("scene_density","–")}')

    print(f"\n=== REWEIGHT (energy=1.0) reorders ===")
    print(f"default order : {full_o}")
    print(f"energy-first  : {_order(energy_ranked)}")

    explainable = all(
        abs(c["score"] - round(100.0 * sum(c["factors"][k] * c["weights"][k] for k in c["factors"]), 1)) < 0.05
        for c in full_ranked)
    print(f"\nVERDICT: explainable={explainable} (every score == 100·Σ factor·weight)  ·  "
          f"non-text signals reordered {moved}/{len(cands)} positions  ·  "
          f"reweight {'reordered' if _order(energy_ranked) != full_o else 'kept order'}")


if __name__ == "__main__":
    main()
