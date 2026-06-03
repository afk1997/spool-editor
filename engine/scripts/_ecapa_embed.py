"""ECAPA-TDNN embedding path for evaluating diarization candidate (1).

Mirrors ``diarizer._continuous_embeddings`` (same 1.6 s / 250 ms-step partial
windows, same ``(times, embeddings)`` contract) but embeds each window with
SpeechBrain's ECAPA-TDNN speaker encoder instead of Resemblyzer. Local,
no-auth (the ``speechbrain/spkrec-ecapa-voxceleb`` model downloads from the HF
hub without a token for inference).

This lives under scripts/ on purpose: it is EVALUATION code, monkeypatched into
``diarizer`` by the benchmark. Production diarizer.py is untouched until/unless
ECAPA measurably beats Resemblyzer on scripts/diarization_bench.py.
"""
import os

_ECAPA = None


def _get_ecapa():
    global _ECAPA
    if _ECAPA is None:
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError as e:  # eval-only dep — not in requirements.txt
            raise ImportError(
                "ECAPA evaluation needs speechbrain: `uv pip install "
                "--python .venv/bin/python speechbrain`. It was evaluated and "
                "NOT adopted (no accuracy win vs resemblyzer); see the item-C "
                "notes in docs/PROGRESS.md.") from e
        savedir = os.path.join(os.path.expanduser("~"), ".cache", "spool_ecapa")
        _ECAPA = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb", savedir=savedir,
            run_opts={"device": "cpu"})
    return _ECAPA


def continuous_embeddings_ecapa(audio_path, speech_regions):
    """Slide 1.6 s / 250 ms-step windows over each speech region; ECAPA-embed
    each. Returns ``(times, embeddings)`` — identical contract to
    ``diarizer._continuous_embeddings`` so the clustering stack is unchanged."""
    import librosa
    import numpy as np
    import torch

    clf = _get_ecapa()
    wav, sr0 = librosa.load(audio_path, sr=16000, mono=True)
    sr = 16000
    PARTIAL = 1.6
    STEP = 0.25

    win_n = int(sr * PARTIAL)              # fixed window length (samples)
    times, segs = [], []
    for region in speech_regions:
        rs, re = float(region["start"]), float(region["end"])
        if re - rs < PARTIAL:
            continue
        t = rs
        while t + PARTIAL <= re + 1e-6:
            s = int(t * sr)
            seg = wav[s:s + win_n]         # exact length so windows stack cleanly
            if len(seg) == win_n:
                segs.append(seg)
                times.append((t, t + PARTIAL))
            t += STEP
    if not segs:
        return [], np.zeros((0, 192))
    # Encode in small mini-batches. A single giant encode_batch over hundreds
    # of 1.6 s windows blows memory up (>1GB) and stalls on CPU; 32-window
    # batches keep it at the ~15 ms/window the probe measured.
    embs = []
    BATCH = 8   # ECAPA's CPU pooling hits a cliff above ~8 (10ms vs 480ms/window)
    for i in range(0, len(segs), BATCH):
        chunk = np.stack(segs[i:i + BATCH])
        with torch.no_grad():
            e = clf.encode_batch(torch.from_numpy(chunk).float())  # [b, 1, 192]
        embs.append(e.squeeze(1).cpu().numpy())
    return times, np.concatenate(embs, axis=0)
