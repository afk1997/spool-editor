"""Captions: styled ASS from word timestamps, then burn-in (spec §5 P1 / §4 ``caption.*``).

No re-transcribe: ``generate`` slices trove's ``words.json`` to the clip's
``[clip_start, clip_end]`` window (re-based to t=0) and feeds the vendored ASS
generator (``clip.backhalf.ass_captions``). Because the transcript is editable
upstream, recognition errors are fixed *before* captions are burned (spec §1.3).
``burn`` rasterizes the ASS into the video via ffmpeg's subtitles filter.

Caption timing needs no re-transcribe — we slice the existing words, never re-run whisper.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import _ffmpeg
from . import exporter

# The vendored back-half script (kept verbatim, MIT — see THIRD_PARTY_LICENSES.md);
# it's a CLI tool, so we invoke it as one.
_ASS_SCRIPT = os.path.join(os.path.dirname(__file__), "backhalf", "ass_captions.py")
_VALID_STYLES = ("opus", "karaoke", "minimal")
BURN_TIMEOUT = 3600
_PLAY_H = 1920  # matches ass_captions PlayResY — for the position→MarginV mapping

# Distinct, high-contrast-on-dark ASS colors (&H00BBGGRR&) assigned to speakers in
# first-appearance order when speaker-coloring is on. [0] is white so a single speaker /
# the dominant speaker reads as the normal caption (and stays byte-identical).
_SPEAKER_PALETTE = ["&H00FFFFFF&", "&H003CC9FF&", "&H00FFE14D&", "&H005CE05C&", "&H00C9A6FF&"]


def _hex_to_ass(hexcolor: str) -> str:
    """'#RRGGBB' (or '#RGB') → ASS '&H00BBGGRR&'."""
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&".upper()


def _ass_overrides(overrides: dict) -> dict:
    """Map S8 fine-styling (UI units) → ass_captions preset keys."""
    ov: dict = {}
    if overrides.get("size") is not None:
        ov["size"] = int(overrides["size"])
    if overrides.get("outline") is not None:
        ov["outline"] = int(overrides["outline"])
    if overrides.get("words") is not None:
        ov["chunk"] = max(1, int(overrides["words"]))
    if overrides.get("font"):
        ov["font"] = str(overrides["font"])
    if overrides.get("weight") is not None:
        ov["bold"] = 1 if int(overrides["weight"]) >= 600 else 0
    if "allcaps" in overrides:
        ov["allcaps"] = bool(overrides["allcaps"])
    if overrides.get("fill"):
        ov["primary"] = _hex_to_ass(str(overrides["fill"]))
    if "highlight" in overrides:
        hv = overrides["highlight"]
        ov["highlight"] = _hex_to_ass(str(hv)) if hv else None
    if overrides.get("position") is not None:
        ov["marginv"] = max(0, min(_PLAY_H, round(float(overrides["position"]) / 100 * _PLAY_H)))
    return ov


def _ass_time(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    """Neutralize chars that would break an ASS override block / Dialogue line."""
    return text.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def generate(
    words_json_path: str,
    *,
    clip_start: float,
    clip_end: float,
    style: str = "opus",
    overrides: dict | None = None,
    watermark: str | None = None,
    lower_third: str | None = None,
    color_speakers: bool = False,
    emphasis: bool = False,
    balance_lines: bool = False,
    out_ass_path: str,
) -> str:
    """Slice ``words.json`` to ``[clip_start, clip_end]`` (re-based to 0) and write a
    styled ASS file (opus / karaoke / minimal) via the vendored generator.

    Caption-craft options (all additive — defaults reproduce today's ASS byte-for-byte):
      ``color_speakers`` — tint each word by its diarization speaker (a palette assigned in
        first-appearance order); only active when ≥2 speakers appear in the window.
      ``emphasis`` — scale up salient words (auto: source ALL-CAPS / acronyms).
      ``balance_lines`` — rebalance chunks so the last line isn't a 1-word orphan.

    Returns ``out_ass_path``. Raises ``ValueError`` for a bad style, an inverted
    window, or a window containing no words.
    """
    if style not in _VALID_STYLES:
        raise ValueError(f"unknown caption style {style!r}; expected one of {list(_VALID_STYLES)}")
    if clip_end <= clip_start:
        raise ValueError(f"clip_end ({clip_end}) must be greater than clip_start ({clip_start})")

    with open(words_json_path) as f:
        data = json.load(f)

    # The serialized words.json carries the diarization speaker on SEGMENTS, not on the flat
    # word list — so resolve each word's speaker from its containing segment (source-time
    # lookup), falling back to a per-word `speaker` if a future transcript persists one.
    segs = [s for s in (data.get("segments") or [])
            if s.get("start") is not None and s.get("end") is not None and s.get("speaker")]

    def _speaker_at(mid: float):
        for s in segs:
            if float(s["start"]) <= mid < float(s["end"]):
                return s["speaker"]
        return None

    sliced = []
    for w in data.get("words", []):
        if w.get("deleted"):
            continue
        start, end = w.get("start"), w.get("end")
        if start is None or end is None:
            continue
        # keep any word overlapping the window
        if end <= clip_start or start >= clip_end:
            continue
        # Transcript text is untrusted (whisper output of arbitrary downloaded media):
        # neutralize ASS-structural chars HERE, before the vendored generator wraps the
        # word in its own {\...} override tags (which must stay intact).
        text = _ass_escape((w.get("w") or ""))
        if not text:
            continue
        sliced.append({
            "start": round(max(0.0, start - clip_start), 3),
            "end": round(max(0.0, min(end, clip_end) - clip_start), 3),
            "word": text,
            # speaker for speaker-colored captions (None when undiarized): segment lookup in
            # SOURCE time (before re-basing), with a flat-word fallback.
            "speaker": w.get("speaker") or _speaker_at((start + end) / 2),
        })
    if not sliced:
        raise ValueError(f"no words in clip window [{clip_start}, {clip_end}]")

    # ass_captions reads data["segments"][].words[].{start,end,word} and re-chunks
    # internally, so one segment holding the sliced words is all it needs.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="spool-cap-words.", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump({"segments": [{"words": sliced}]}, f)
        argv = [sys.executable, _ASS_SCRIPT, tmp_path, out_ass_path, style]
        ass_ov = _ass_overrides(overrides) if overrides else {}
        # Caption-craft options → ass_captions overrides (each off by default → output unchanged).
        if color_speakers:
            seen: list = []
            for s in sliced:
                sp = s.get("speaker")
                if sp is not None and sp not in seen:
                    seen.append(sp)
            if len(seen) >= 2:   # one speaker → leave the captions exactly as they were
                ass_ov["speaker_colors"] = {
                    sp: _SPEAKER_PALETTE[i % len(_SPEAKER_PALETTE)] for i, sp in enumerate(seen)}
        if emphasis:
            ass_ov["emphasis"] = "auto"
        if balance_lines:
            ass_ov["balance"] = True
        if ass_ov:
            argv.append(json.dumps(ass_ov))
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ass generation failed (rc={proc.returncode}): {proc.stderr.strip()[-300:]}"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Brand-kit overlays: pin a watermark (top-right) + lower-third (top-center) as static
    # ASS lines spanning the whole clip — burned by the same libass pass as the captions.
    extras = []
    end_tc = _ass_time(max(0.0, clip_end - clip_start))
    if watermark:
        extras.append(f"Dialogue: 0,0:00:00.00,{end_tc},Default,,0,0,0,,{{\\an9\\fs44\\alpha&H50&}}{_ass_escape(watermark)}")
    if lower_third:
        extras.append(f"Dialogue: 0,0:00:00.00,{end_tc},Default,,0,0,0,,{{\\an8\\fs54}}{_ass_escape(lower_third)}")
    if extras:
        with open(out_ass_path, "a", encoding="utf-8") as f:
            f.write("\n".join(extras) + "\n")
    return out_ass_path


def burn(
    clip_path: str,
    ass_path: str,
    out_path: str,
    *,
    cancel_check=None,
    register_proc=None,
    timeout: int | None = None,
) -> str:
    """Burn ``ass_path`` into ``clip_path`` (ffmpeg ``subtitles`` filter); return ``out_path``.

    Re-encodes video (captions are rasterized into the frames); audio is stream-copied.
    """
    argv = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vf", f"subtitles={_escape_filter_path(ass_path)}",
        # Intermediate pass → hardware encoder + visually-lossless quality (was an implicit ~CRF 23).
        *exporter.intermediate_encode_flags(),
        "-c:a", "copy",
        out_path,
    ]
    _ffmpeg.run(
        argv,
        cancel_check=cancel_check,
        register_proc=register_proc,
        timeout=timeout if timeout is not None else BURN_TIMEOUT,
        cleanup_path=out_path,
        label="ffmpeg caption burn",
    )
    return out_path


def _escape_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph (the ``subtitles`` source).

    Backslash, colon and single-quote are special to the filter parser.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
