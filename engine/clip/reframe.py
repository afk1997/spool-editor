"""Reframe: ROI detection, the diar⊕ROI speaker timeline, and pan/split/center render.

This is Spool's signature upgrade (spec §1.3 "the diarization⊕ROI win"). The upstream
back-half pans purely on *video* motion and fails on still or off-mic speakers; Spool
**fuses** trove's *audio* diarization (who is speaking, when) with the back-half's
*video* ROI motion (where each face is) to build a far more robust speaker timeline,
then drives the hard-cut pan.

    detect_faces(frame) ─┐
                          ├─▶ speaker_track (diar ⊕ roi_motion) ─▶ render (pan/split/center)
    diarization turns  ──┘

Phase 1: basic detect/track/render. Phase 2: the visual ROI + speaker-track editor.
"""
from __future__ import annotations

from .backhalf import roi_motion, pan_expr  # noqa: F401  (wrapped in the impl)

# ROI shape: {"x": int, "y": int, "w": int, "h": int}   (a face rectangle on a sample frame)
# SpeakerTrack shape (mirrors packages/types): {clipId, segments:[{start,end,speaker}],
#   roiL, roiR, source: "diar"|"roi"|"fused"}


def detect_faces(frame_path: str, *, max_faces: int = 2) -> list[dict]:
    """Propose ROI rectangles on a single sample frame.

    The camera is static within a clip, so one frame is enough. Phase 1 seeds these
    (and the agent/user confirm via elicitation / the ROI editor); there is no
    heavyweight face-detection model — that tiny dependency surface is a feature.
    """
    raise NotImplementedError("Phase 1 — ROI seeding (spec §4 reframe.detect_faces)")


def speaker_track(
    clip_path: str,
    *,
    roi_left: dict,
    roi_right: dict,
    diarization: list[dict] | None = None,
    min_dwell: float = 1.0,
) -> dict:
    """Build the fused **diar⊕ROI** speaker timeline for a clip.

    Measures per-ROI motion energy (``backhalf.roi_motion``) and, when available,
    reconciles it with audio diarization turns so still/off-mic speakers resolve
    correctly. Returns a ``SpeakerTrack`` (``source="fused"`` when both signals were
    used, else ``"roi"``). This same structure is what the P2 editor edits.
    """
    raise NotImplementedError("Phase 1 — diar⊕ROI fusion (spec §1.3, §4 reframe.speaker_track)")


def render(
    clip_path: str,
    track: dict,
    *,
    aspect: str = "9:16",
    mode: str = "pan",
    out_path: str,
) -> str:
    """Render the reframed clip. ``mode`` ∈ {pan, split, center}; ``aspect`` ∈
    {9:16, 16:9, 1:1, 4:5}. ``pan`` builds the crop-x expression via
    ``backhalf.pan_expr`` from ``track``. Returns ``out_path``."""
    raise NotImplementedError("Phase 1 — pan/split/center render (spec §4 reframe.render)")
