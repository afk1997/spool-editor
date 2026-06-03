"""Diarization accuracy harness — speaker count + turns vs ground truth.

Run: .venv/bin/python scripts/diarization_eval.py <source.mp4|.wav> [truth_count]

Runs the REAL ``diarizer.diarize`` (resemblyzer + silero-vad + clustering) on a clip and
reports the detected speaker count and turn structure, checked against a known ground
truth. The companion of reframe_eval.py / caption_sync_eval.py — measure, don't vibe.
Forces ``TROVE_DIARIZATION=on`` (this tool always wants diarization).
"""
import json
import os
import subprocess
import sys
import tempfile

os.environ["TROVE_DIARIZATION"] = "on"
ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)
import diarizer  # noqa: E402


def main():
    src = os.path.abspath(sys.argv[1])
    truth = int(sys.argv[2]) if len(sys.argv) > 2 else None

    if src.lower().endswith(".wav"):
        wav, tmp = src, None
    else:
        tmp = tempfile.mkdtemp(prefix="diareval.")
        wav = os.path.join(tmp, "audio.wav")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                        "-ac", "1", "-ar", "16000", wav], check=True)

    if not diarizer.available():
        print("diarization unavailable — install resemblyzer/silero-vad/sklearn/torch")
        return

    chunks = diarizer.diarize(audio_path=wav)
    speakers = sorted({c.speaker for c in chunks})
    out = {
        "source": os.path.basename(src),
        "speaker_count": len(speakers),
        "turns": len(chunks),
        "speakers": speakers,
    }
    if truth is not None:
        out["ground_truth"] = truth
        out["correct"] = len(speakers) == truth
    out["turn_list"] = [{"start": round(c.start, 2), "end": round(c.end, 2),
                         "speaker": c.speaker} for c in chunks[:40]]
    if tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
