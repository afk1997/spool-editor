"""Export: final mux + platform/loudness preset + hardware encoder (spec §5 P1 / §4 ``render.export``).

The last hop: take a reframed, captioned clip and produce the delivered ``.mp4`` for a
target platform — codec + bitrate + fps + social loudness normalization (-14 LUFS),
encoded on the best available hardware encoder (VideoToolbox / NVENC / x264), with a
fast-vs-quality switch. Every render is a versioned file the user owns (spec §3).

Brand kits (logo/intro/outro/lower-thirds) are Phase 2 — ``brand_kit_id`` is accepted
for forward-compat but not yet applied.
"""
from __future__ import annotations

import machine

from . import _ffmpeg

EXPORT_TIMEOUT = 1800

# Per-platform encode profile. Dimensions/aspect are already set by reframe; export
# only conforms codec/bitrate/fps/loudness.
_PRESETS = {
    "tiktok":   {"fps": 30, "v_bitrate": "6M", "a_bitrate": "128k"},
    "reels":    {"fps": 30, "v_bitrate": "6M", "a_bitrate": "128k"},
    "shorts":   {"fps": 30, "v_bitrate": "8M", "a_bitrate": "128k"},
    "youtube":  {"fps": 30, "v_bitrate": "12M", "a_bitrate": "192k"},
    "linkedin": {"fps": 30, "v_bitrate": "5M", "a_bitrate": "128k"},
    "x":        {"fps": 30, "v_bitrate": "5M", "a_bitrate": "128k"},
}

# Social loudness target (single-pass; good enough for P1, two-pass is a P2 refinement).
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"

# GPU tier (from machine._detect_gpu) → h264 encoder.
_ENCODERS = {"metal": "h264_videotoolbox", "cuda": "h264_nvenc", "cpu": "libx264"}


def pick_encoder(gpu: str | None = None) -> str:
    """Best available h264 encoder for this machine (VideoToolbox on Apple Silicon,
    NVENC on NVIDIA, else the libx264 software fallback)."""
    return _ENCODERS.get(gpu or machine._detect_gpu(), "libx264")


def intermediate_encode_flags(encoder: str | None = None) -> list[str]:
    """ffmpeg video-codec flags for INTERMEDIATE passes (reframe, caption-burn).

    These feed another encode downstream (the final export), so they must be visually lossless
    (no visible generational loss) but need not be archival — and should be FAST. Previously
    these passes set no ``-c:v`` at all, so ffmpeg fell back to libx264 at its implicit ~CRF 23 /
    medium; this routes them to the best available encoder (``pick_encoder``) at a high quality
    target. libx264 falls back to CRF 18 / veryfast (sharper AND faster than the old default);
    the export step still re-encodes to the platform bitrate.
    """
    enc = encoder or pick_encoder()
    if enc == "h264_videotoolbox":
        # q:v 75 ≈ the libx264 crf18 fallback in size/quality (measured ~15MB vs 14.5MB on a 30s
        # 1080×1920 clip) — visually lossless, so the intermediate doesn't bottleneck the final
        # export; speed is q-independent on hardware (~same wall-time at any q:v).
        return ["-c:v", "h264_videotoolbox", "-q:v", "75"]
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "18", "-preset", "p4"]
    if enc == "libx264":
        return ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast"]
    return ["-c:v", enc]   # unknown encoder: set the codec, let ffmpeg default the rest


def export(
    clip_path: str,
    *,
    preset: str = "tiktok",
    brand_kit_id: str | None = None,
    fast: bool = True,
    out_path: str,
    encoder: str | None = None,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Encode ``clip_path`` to the delivered file for ``preset``
    (tiktok/reels/shorts/linkedin/x/youtube). ``fast`` trades quality for speed.
    Returns ``out_path``."""
    if preset not in _PRESETS:
        raise ValueError(f"unknown preset {preset!r}; expected one of {list(_PRESETS)}")
    p = _PRESETS[preset]
    enc = encoder or pick_encoder()

    argv = [
        "ffmpeg", "-y", "-i", clip_path,
        "-c:v", enc, "-b:v", p["v_bitrate"], "-r", str(p["fps"]),
        *_speed_flags(enc, fast),
        "-af", _LOUDNORM, "-c:a", "aac", "-b:a", p["a_bitrate"],
        "-movflags", "+faststart",          # web-streamable (moov atom up front)
        out_path,
    ]
    _ffmpeg.run(
        argv,
        cancel_check=cancel_check, register_proc=register_proc,
        timeout=timeout if timeout is not None else EXPORT_TIMEOUT,
        cleanup_path=out_path, label=f"ffmpeg export {preset}",
    )
    return out_path


def _speed_flags(encoder: str, fast: bool) -> list[str]:
    """Encoder-specific speed/quality knob. VideoToolbox is hardware-fast already."""
    if encoder == "libx264":
        return ["-preset", "veryfast" if fast else "slow"]
    if encoder == "h264_nvenc":
        return ["-preset", "p4" if fast else "p7"]
    return []  # h264_videotoolbox / unknown — bitrate alone
