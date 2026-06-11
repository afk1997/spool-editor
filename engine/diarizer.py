"""Local speaker diarization. No HuggingFace auth, no API keys.

Pipeline: silero-vad → speech chunks → Resemblyzer embeddings →
sklearn AgglomerativeClustering. Realistic accuracy is ~70% on clean
audio (worse than pyannote, but no auth required).

Heavy deps (resemblyzer, silero-vad, scikit-learn, torch) are imported
LAZILY inside the worker functions. The module itself imports cleanly
on a stock Python install, so the rest of the app never blows up just
because diarization isn't available.

Public API
----------
``diarize(audio_path, expected_speakers=None)`` returns a list of
``SpeakerChunk`` objects sorted by start time. Raises ``DiarizationUnavailable``
when the optional dependencies aren't installed.

``available()`` returns True when all heavy deps import successfully.
Use this to short-circuit at the call-site before spending time on a
big audio file.

Feature-flag ``TROVE_DIARIZATION``: when set to "off" / "0" / "false",
``available()`` returns False even if the deps are installed. Default
is "off" because the deps are ~800MB and not bundled with Trove.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass


class DiarizationUnavailable(RuntimeError):
    """Raised when heavy deps aren't installed or the feature flag is off."""


# Process-wide ``VoiceEncoder`` cache. Constructing the encoder loads
# ~50MB of weights and takes a few seconds; per-job instantiation made
# repeated transcribes slow and memory-churny. ``_get_encoder()`` lazily
# builds it once and every later diarize() reuses the same instance.
_ENCODER = None
_ENCODER_LOCK = threading.Lock()


def _get_encoder():
    """Return the cached ``VoiceEncoder``, building it on first use.

    Lazy-imports resemblyzer so the module still loads on a stock
    Python install. Raises ``DiarizationUnavailable`` if the import
    fails.
    """
    global _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            try:
                from resemblyzer import VoiceEncoder
            except Exception as e:
                raise DiarizationUnavailable(
                    f"resemblyzer not installed: {e}") from e
            _ENCODER = VoiceEncoder()
        return _ENCODER


def warm() -> bool:
    """Eagerly load the encoder so the first diarize() call is fast.

    Useful for health checks or a "warm models" admin path so users
    aren't surprised by a multi-second pause on their first transcribe.
    Returns True on success, False when diarization isn't available
    (feature flag off or deps missing).
    """
    if not available():
        return False
    try:
        _get_encoder()
        return True
    except DiarizationUnavailable:
        return False


@dataclass
class SpeakerChunk:
    start: float    # seconds
    end: float      # seconds (exclusive)
    speaker: str    # "Speaker 1", "Speaker 2", ...


def _flag_enabled() -> bool:
    """``TROVE_DIARIZATION`` env var. Defaults to off."""
    raw = (os.environ.get("TROVE_DIARIZATION", "off") or "").strip().lower()
    return raw in {"on", "1", "true", "yes"}


def available() -> bool:
    """True iff the feature flag is on AND all heavy deps import OK."""
    if not _flag_enabled():
        return False
    try:
        import resemblyzer  # noqa: F401
        import sklearn  # noqa: F401
        import torch  # noqa: F401
    except Exception:
        return False
    return True


def vad_available() -> bool:
    """True iff silero-vad word-realignment can run — deps present, IGNORING
    the ``TROVE_DIARIZATION`` flag.

    Word-realignment (``transcriber.realign_words_to_vad``: snapping
    whisper.cpp's post-silence word drift to silero-vad speech regions)
    improves *caption timing* and is independent of whether speaker
    *labelling* (diarization) is enabled. It needs only silero-vad +
    librosa + torch — NOT resemblyzer/scikit-learn (the speaker-embedding
    + clustering stack) — so it can and should run whenever those light
    deps are installed, even with the diarization feature flag off.

    Deliberately does NOT consult ``_flag_enabled()``: caption-timing
    accuracy must not depend on the speaker-label feature flag.
    """
    try:
        import torch  # noqa: F401
        import librosa  # noqa: F401
        from silero_vad import (  # noqa: F401
            load_silero_vad,
            get_speech_timestamps,
        )
    except Exception:
        return False
    return True


def diarize(*, audio_path: str,
            expected_speakers: int | None = None) -> list[SpeakerChunk]:
    """Run VAD + continuous-window embedding + clustering on a 16k mono WAV.

    Pipeline (the v2 chunk-per-VAD-region approach lumped multi-speaker
    audio into a single embedding and produced near-random labels for any
    chunk that contained more than one speaker):

      1. silero-vad picks out speech regions.
      2. Within each region, slide overlapping 1.6 s windows and embed
         each one separately (Resemblyzer's ``embed_utterance(return_partials=True)``).
      3. Cluster the partials with Ward + Euclidean — empirically far less
         skewed than cosine + average on partial embeddings (where one
         dominant speaker would otherwise eat 90% of the labels).
      4. Median-filter the labels so a single anomalous window doesn't
         create a one-second phantom speaker.
      5. Merge consecutive same-label partials into ``SpeakerChunk`` runs.

    Args:
        audio_path: path to a 16kHz mono WAV.
        expected_speakers: if set (1..6), force k. Otherwise auto-detect.

    Returns:
        Speaker chunks sorted by start time. Empty when no speech was
        detected or there wasn't enough audio for reliable clustering.

    Raises:
        DiarizationUnavailable: when deps aren't installed or flag is off.
    """
    if not _flag_enabled():
        raise DiarizationUnavailable(
            "TROVE_DIARIZATION is off (set TROVE_DIARIZATION=on to enable)"
        )
    wav = _load_wav_16k(audio_path)
    speech = _vad_speech_chunks(wav)
    if not speech:
        return []
    times, embeddings = _continuous_embeddings(wav, speech)
    if len(embeddings) < 2:
        # One or zero embeddings — nothing to cluster. Fall back to a
        # single SpeakerChunk covering the whole speech span.
        if not times:
            return []
        return [SpeakerChunk(start=float(times[0][0]),
                             end=float(times[-1][1]),
                             speaker="Speaker 1")]

    if expected_speakers is None:
        k = _auto_k_partials(embeddings)
    else:
        k = max(1, min(6, int(expected_speakers)))

    if k <= 1:
        return [SpeakerChunk(start=float(times[0][0]),
                             end=float(times[-1][1]),
                             speaker="Speaker 1")]

    labels = _cluster_partials(embeddings, k)
    # window=5 (~1.25s) lets short turns (≥1s) survive while still
    # filtering out single-window flips. window=9 absorbed real
    # 1-2s utterances like "Hi." and "Mary?" between long turns.
    labels = _smooth_labels(labels, window=5)

    runs: list[SpeakerChunk] = []
    cur_s, cur_e, cur_l = times[0][0], times[0][1], labels[0]
    for (s, e), lbl in zip(times[1:], labels[1:]):
        if lbl == cur_l:
            cur_e = max(cur_e, e)
        else:
            runs.append(SpeakerChunk(
                start=float(cur_s), end=float(cur_e),
                speaker=f"Speaker {int(cur_l) + 1}",
            ))
            cur_s, cur_e, cur_l = s, e, lbl
    runs.append(SpeakerChunk(
        start=float(cur_s), end=float(cur_e),
        speaker=f"Speaker {int(cur_l) + 1}",
    ))
    return runs


# ----------------------------------------------------------------------
# Internal helpers (each lazy-imports its heavy dep so the module loads
# on a stock Python install).
# ----------------------------------------------------------------------

def _load_wav_16k(audio_path: str):
    """Decode once: 16 kHz mono float32. Both silero-vad and Resemblyzer operate at
    16 kHz, so one decode serves VAD + embedding (it was decoded up to 3x per job)."""
    try:
        import librosa
    except Exception as e:
        raise DiarizationUnavailable(f"librosa not installed: {e}") from e
    wav, _sr = librosa.load(audio_path, sr=16000, mono=True)
    return wav


def _vad_speech_chunks(wav) -> list[dict]:
    """silero-vad → list of {"start": s, "end": s} dicts.

    Accepts a 16 kHz mono float32 numpy array (pre-decoded by
    ``_load_wav_16k``). silero-vad's bundled ``read_audio`` calls
    torchaudio for I/O, which on torchaudio ≥2.9 requires the optional
    ``torchcodec`` package and breaks with a confusing message when it's
    missing. We sidestep all of that by receiving an already-loaded
    array and feeding silero-vad a pre-built tensor.
    """
    try:
        import torch
        from silero_vad import load_silero_vad, get_speech_timestamps
    except Exception as e:
        raise DiarizationUnavailable(f"silero-vad not installed: {e}") from e
    model = load_silero_vad()
    import numpy as np
    wav_tensor = torch.from_numpy(np.asarray(wav, dtype=np.float32))
    timestamps = get_speech_timestamps(wav_tensor, model, sampling_rate=16000)
    return [{"start": t["start"] / 16000.0, "end": t["end"] / 16000.0}
            for t in timestamps]


def _embed_chunks(audio_path: str, chunks: list[dict]):
    """Resemblyzer voice encoder → (kept_chunks, embeddings).

    Skips chunks shorter than 0.5s (too short for a stable embedding).
    Returns the surviving chunks alongside their embeddings so the
    caller can pair labels with the correct time intervals — a naive
    `chunks[:len(embeddings)]` slice would silently misalign whenever
    a non-trailing short chunk is dropped.

    NOTE on audio loading: resemblyzer's stock ``preprocess_wav`` calls
    ``trim_long_silences``, which strips silent regions and shrinks the
    returned array. Chunk timestamps reference the ORIGINAL audio
    timeline, so any later chunk past a silent gap would index past
    the trimmed wav's end and get silently dropped. We instead mirror
    preprocess_wav's load + resample + volume-normalize steps and
    skip the silence trim.
    """
    try:
        from resemblyzer.audio import (
            normalize_volume,
            audio_norm_target_dBFS,
            sampling_rate as _RES_SR,
        )
        import librosa
        import numpy as np
    except Exception as e:
        raise DiarizationUnavailable(f"resemblyzer not installed: {e}") from e
    encoder = _get_encoder()
    wav, source_sr = librosa.load(audio_path, sr=None)
    if source_sr != _RES_SR:
        wav = librosa.resample(wav, orig_sr=source_sr, target_sr=_RES_SR)
    wav = normalize_volume(wav, audio_norm_target_dBFS, increase_only=True)
    sr = _RES_SR
    kept: list[dict] = []
    embeddings = []
    for c in chunks:
        s = int(c["start"] * sr)
        e = int(c["end"] * sr)
        seg = wav[s:e]
        if len(seg) < int(sr * 0.5):
            continue
        embeddings.append(encoder.embed_utterance(seg))
        kept.append(c)
    if not embeddings:
        return kept, np.zeros((0, 256))
    return kept, np.array(embeddings)


def _cluster(embeddings, k: int):
    """Agglomerative cosine clustering. Returns 0..k-1 label per row."""
    try:
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise DiarizationUnavailable(f"scikit-learn not installed: {e}") from e
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1 or k <= 1:
        return [0] * n
    k = min(k, n)
    clf = AgglomerativeClustering(
        n_clusters=k,
        metric="cosine",
        linkage="average",
    )
    return list(clf.fit_predict(embeddings))


# Two cluster centroids closer than this in cosine distance are treated as the SAME
# speaker. Within-speaker cosine distance on Resemblyzer embeddings is typically 0.05-0.25;
# between-speaker is typically 0.40-0.70. Tuned for Resemblyzer + clean speech; lower (0.20)
# tolerates more within-speaker variation but lets a borderline second speaker slip through,
# higher (0.30) is stricter. Both auto-K paths share it. Verified on real clips: "Me at the
# zoo" (1 narrator) tops out at 0.243 cosine between its two tidiest sub-clusters → k=1; the
# Karpathy×Zhan interview (2 speakers) sits at 0.302 → k=2.
MIN_CENTROID_DIST = 0.25


def _min_centroid_cosine_distance(embeddings, labels) -> float:
    """Smallest cosine distance between any pair of cluster centroids (0.0 = identical
    direction, 1.0 = orthogonal). The tightest pair is the most likely same-speaker split,
    so this is the discriminator for "did clustering split one speaker or find a new one?"."""
    import numpy as np
    labels = np.asarray(labels)
    centroids = [embeddings[labels == c].mean(axis=0)
                 for c in sorted(set(labels.tolist())) if (labels == c).any()]
    if len(centroids) < 2:
        return float("inf")
    centroids = np.asarray(centroids)
    normed = centroids / np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-9)
    cos_sim = normed @ normed.T
    np.fill_diagonal(cos_sim, -np.inf)          # ignore self-similarity
    return float(1.0 - cos_sim.max())           # 1 − (max off-diagonal similarity)


def _auto_k(embeddings, max_k: int = 4) -> int:
    """Choose k between 1 and max_k by inter-cluster centroid distance.

    Within-speaker cosine distance on Resemblyzer embeddings is typically
    0.05-0.25; between-speaker distance is typically 0.40-0.70. So we can
    discriminate "real different speakers" from "one speaker, varied
    delivery" by requiring every pair of cluster centroids to be at least
    ``MIN_CENTROID_DIST`` apart in cosine distance.

    Walks k upward from 2; stops at the first k whose tightest pair of
    centroids is closer than the threshold (those are the same speaker
    that the clusterer split into two halves). This is much more reliable
    than a within-cluster-distance ratio heuristic, which can't tell a
    real second speaker (~50% drop in within-cluster dist) from a
    well-fitting same-speaker split (~30-50% drop).

    < 4 chunks → k=1 (not enough data).
    """
    try:
        import numpy as np
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise DiarizationUnavailable(f"scikit-learn not installed: {e}") from e
    n = len(embeddings)
    if n < 4:
        return 1
    upper = min(max_k, n)

    best_k = 1
    for k in range(2, upper + 1):
        clf = AgglomerativeClustering(
            n_clusters=k, metric="cosine", linkage="average",
        )
        labels = np.asarray(clf.fit_predict(embeddings))
        centroids = []
        for c in sorted(set(labels.tolist())):
            members = embeddings[labels == c]
            if len(members) == 0:
                continue
            centroids.append(members.mean(axis=0))
        centroids = np.asarray(centroids)
        # Normalize to unit length so dot product = cosine similarity
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        normed = centroids / np.maximum(norms, 1e-9)
        cos_sim = normed @ normed.T
        pairwise_dist = 1.0 - cos_sim
        # Ignore self-distances on the diagonal
        np.fill_diagonal(pairwise_dist, np.inf)
        min_inter = float(pairwise_dist.min())
        if min_inter < MIN_CENTROID_DIST:
            # Tightest pair of clusters is too close — they represent the
            # same speaker. The previous k (or k=1) is the answer.
            break
        best_k = k
    return best_k


def _continuous_embeddings(wav, speech_regions: list[dict]):
    """Slide overlapping 1.6 s windows across each speech region and embed
    each one. Returns ``(times, embeddings)`` where ``times[i]`` is the
    ``(start, end)`` interval of the i-th partial (in original-audio seconds)
    and ``embeddings[i]`` is its 256-d Resemblyzer embedding.

    Accepts a 16 kHz mono float32 numpy array (pre-decoded by
    ``_load_wav_16k``). This is the v3 path: instead of one embedding
    per VAD region (which smears multi-speaker audio into a single
    point), we compute many finer-grained embeddings so a speaker change
    inside one VAD region is still resolvable downstream.
    """
    try:
        from resemblyzer.audio import (
            normalize_volume,
            audio_norm_target_dBFS,
            sampling_rate as _RES_SR,
        )
        import numpy as np
    except Exception as e:
        raise DiarizationUnavailable(f"resemblyzer not installed: {e}") from e
    encoder = _get_encoder()
    wav = np.asarray(wav, dtype=np.float32)
    if _RES_SR != 16000:   # resemblyzer's constant is 16000 today; resample defensively
        import librosa
        wav = librosa.resample(wav, orig_sr=16000, target_sr=_RES_SR)
    wav = normalize_volume(wav, audio_norm_target_dBFS, increase_only=True)
    sr = _RES_SR

    # 1.6 s is the partial-utterance length baked into Resemblyzer's
    # encoder. A region shorter than that can't produce overlapping
    # partials; we skip it (the speaker is still labeled by neighboring
    # partials via apply_speakers' forward/backward fill).
    PARTIAL_DURATION = 1.6
    PARTIALS_PER_SEC = 4  # rate=4 → ~250ms steps between partials

    times: list[tuple[float, float]] = []
    embeddings = []
    for region in speech_regions:
        rs = float(region["start"])
        re = float(region["end"])
        if re - rs < PARTIAL_DURATION:
            continue
        s = int(rs * sr)
        e = int(re * sr)
        seg = wav[s:e]
        if len(seg) < int(sr * PARTIAL_DURATION):
            continue
        try:
            _, partials, splits = encoder.embed_utterance(
                seg, return_partials=True, rate=PARTIALS_PER_SEC,
            )
        except Exception:
            # Fall back to a single embedding for this region.
            embeddings.append(encoder.embed_utterance(seg))
            times.append((rs, re))
            continue
        for p_idx, slc in enumerate(splits):
            p_start = rs + slc.start / sr
            p_end = rs + slc.stop / sr
            embeddings.append(partials[p_idx])
            times.append((p_start, p_end))

    if not embeddings:
        return [], np.zeros((0, 256))
    return times, np.array(embeddings)


def _smooth_labels(labels, window: int = 9):
    """Median-filter cluster labels to suppress single-window flips.

    A speaker change that lasts only one or two partials (~0.5 s) is
    almost certainly noise — Resemblyzer momentarily latched onto a
    breath, a laugh, or a short interjection. Replacing each label
    with the mode of its ``window``-wide neighborhood makes the runs
    more readable without losing real turn-taking that lasts ≥1 s.
    """
    import numpy as np
    arr = np.asarray(labels)
    if len(arr) == 0:
        return arr
    out = np.array(arr, copy=True)
    half = window // 2
    for i in range(len(arr)):
        lo = max(0, i - half)
        hi = min(len(arr), i + half + 1)
        vals, counts = np.unique(arr[lo:hi], return_counts=True)
        out[i] = vals[int(counts.argmax())]
    return out


def _cluster_partials(embeddings, k: int):
    """Cluster partial embeddings with Ward + Euclidean.

    Cosine + average linkage (used by ``_cluster``) is appropriate for
    a small number of long-utterance embeddings, but on hundreds of
    partials it produces near-singleton clusters: one big bucket and
    a sliver. Ward + Euclidean produces balanced clusters that map
    closely to actual speaker share-of-voice.
    """
    try:
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise DiarizationUnavailable(f"scikit-learn not installed: {e}") from e
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1 or k <= 1:
        return [0] * n
    k = min(k, n)
    clf = AgglomerativeClustering(
        n_clusters=k,
        metric="euclidean",
        linkage="ward",
    )
    return list(clf.fit_predict(embeddings))


def _auto_k_partials(embeddings, max_k: int = 4) -> int:
    """Choose k for partial embeddings by inter-speaker centroid distance.

    A silhouette score only measures how *tidy* clusters are — it can't tell one speaker's
    phonetic sub-clusters (tidy, but close together) from two real speakers (tidy, and far
    apart). On a monologue it over-counts: "Me at the zoo" (one narrator) split into two
    clean clusters only 0.243 cosine apart at silhouette 0.216 → the old gate reported 2.

    Instead, gate on the SAME within/between-speaker boundary the long-utterance path uses
    (``_auto_k`` / ``MIN_CENTROID_DIST``): two cluster centroids closer than the threshold
    are the same speaker. Walk k upward (Ward on partials → balanced clusters) and accept a
    k only while every centroid pair stays ≥ the threshold AND no cluster is a <5% sliver;
    stop at the first k that fails, since higher k only splits finer (closer, smaller).

    Verified on real clips: zoo (1 speaker) → k=1, Karpathy interview (2) → k=2.
    < 10 partials → k=1 (not enough data to trust a split).
    """
    try:
        import numpy as np
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise DiarizationUnavailable(f"scikit-learn not installed: {e}") from e
    n = len(embeddings)
    if n < 10:
        return 1
    upper = min(max_k, n - 1)
    best_k = 1
    for k in range(2, upper + 1):
        labels = AgglomerativeClustering(
            n_clusters=k, metric="euclidean", linkage="ward",
        ).fit_predict(embeddings)
        _, counts = np.unique(labels, return_counts=True)
        if counts.min() < 0.05 * n:
            break  # a <5% sliver isn't a distinct speaker; higher k only slivers more
        if _min_centroid_cosine_distance(embeddings, labels) < MIN_CENTROID_DIST:
            break  # tightest centroid pair is within-speaker close — k-1 was the answer
        best_k = k
    return best_k


def _within_cluster_dist(embeddings, labels) -> float:
    """Mean cosine distance from each point to its cluster centroid."""
    import numpy as np
    labels = np.asarray(labels)
    total = 0.0
    n = 0
    for c in set(labels.tolist()):
        members = embeddings[labels == c]
        if len(members) == 0:
            continue
        centroid = members.mean(axis=0)
        cn = np.linalg.norm(centroid) or 1e-9
        for m in members:
            mn = np.linalg.norm(m) or 1e-9
            cos_sim = float((m @ centroid) / (mn * cn))
            total += 1.0 - cos_sim
            n += 1
    return total / n if n else 0.0
