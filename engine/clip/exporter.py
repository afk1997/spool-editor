"""Export: final mux + platform/loudness presets + brand kit (spec §5 P1 / §4 ``render.export``).

The last hop: take a reframed, captioned clip and produce the delivered ``.mp4`` with a
per-platform container/codec/bitrate/fps and loudness normalization (-14 LUFS for social),
using the hardware-aware encoder trove probes (VideoToolbox/NVENC/QSV/VAAPI/x264).
Every render is a versioned file the user owns (spec §3 on-disk layout).

Phase 1: platform presets. Phase 2: brand kits (logo/intro/outro/lower-thirds).
"""
from __future__ import annotations


def export(
    clip_path: str,
    *,
    preset: str = "tiktok",
    brand_kit_id: str | None = None,
    fast: bool = True,
    out_path: str,
) -> str:
    """Mux ``clip_path`` to the delivered file for ``preset`` (tiktok/reels/shorts/
    linkedin/x/youtube). ``fast`` selects the speed-vs-quality encoder preset.
    ``brand_kit_id`` overlays saved branding (P2). Returns ``out_path``."""
    raise NotImplementedError("Phase 1 — export + platform presets (spec §4 render.export)")
