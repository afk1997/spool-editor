"""Tests for clip.exporter — platform/loudness/hardware-encoder mux.

ffmpeg (clip._ffmpeg.run) and GPU detection (machine._detect_gpu) are mocked; we
assert the encode argv contract.
"""
from __future__ import annotations

import machine
import pytest

from clip import exporter


@pytest.fixture()
def captured(monkeypatch):
    box = {}
    monkeypatch.setattr(exporter._ffmpeg, "run", lambda argv, **kw: box.update(argv=argv))
    return box


def _val(argv, flag):
    return argv[argv.index(flag) + 1]


def test_export_builds_platform_argv(captured, tmp_path):
    out = str(tmp_path / "final.mp4")
    result = exporter.export("clip.mp4", preset="tiktok", out_path=out, encoder="libx264")
    assert result == out
    argv = captured["argv"]
    assert _val(argv, "-c:v") == "libx264"
    assert _val(argv, "-b:v") == "6M"           # tiktok video bitrate
    assert _val(argv, "-r") == "30"
    assert _val(argv, "-preset") == "veryfast"  # fast=True
    assert _val(argv, "-af").startswith("loudnorm=I=-14")  # -14 LUFS social loudness
    assert _val(argv, "-c:a") == "aac" and _val(argv, "-b:a") == "128k"
    assert _val(argv, "-movflags") == "+faststart"
    assert argv[-1] == out


def test_export_quality_mode_uses_slow_preset(captured, tmp_path):
    exporter.export("clip.mp4", preset="tiktok", fast=False, out_path=str(tmp_path / "o.mp4"), encoder="libx264")
    assert _val(captured["argv"], "-preset") == "slow"


def test_export_youtube_has_higher_bitrate(captured, tmp_path):
    exporter.export("clip.mp4", preset="youtube", out_path=str(tmp_path / "o.mp4"), encoder="libx264")
    assert _val(captured["argv"], "-b:v") == "12M"
    assert _val(captured["argv"], "-b:a") == "192k"


def test_export_uses_videotoolbox_on_metal(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(machine, "_detect_gpu", lambda: "metal")
    exporter.export("clip.mp4", preset="reels", out_path=str(tmp_path / "o.mp4"))
    argv = captured["argv"]
    assert _val(argv, "-c:v") == "h264_videotoolbox"
    assert "-preset" not in argv  # videotoolbox: bitrate alone, no x264 preset


@pytest.mark.parametrize("gpu,enc", [
    ("metal", "h264_videotoolbox"),
    ("cuda", "h264_nvenc"),
    ("cpu", "libx264"),
    ("anything-else", "libx264"),
])
def test_pick_encoder(gpu, enc):
    assert exporter.pick_encoder(gpu) == enc


def test_export_rejects_unknown_preset(tmp_path):
    with pytest.raises(ValueError, match="unknown preset"):
        exporter.export("clip.mp4", preset="myspace", out_path=str(tmp_path / "o.mp4"))


# ---- intermediate encode flags (item F): hardware + visually-lossless for reframe/caption ----

def test_intermediate_flags_libx264_is_sharp_and_fast():
    """The software fallback: CRF 18 (sharper than the old implicit ~23) at veryfast."""
    f = exporter.intermediate_encode_flags("libx264")
    assert f[:2] == ["-c:v", "libx264"]
    assert _val(f, "-crf") == "18"
    assert _val(f, "-preset") == "veryfast"


def test_intermediate_flags_use_hardware_encoders():
    vt = exporter.intermediate_encode_flags("h264_videotoolbox")
    assert _val(vt, "-c:v") == "h264_videotoolbox" and "-crf" not in vt
    nv = exporter.intermediate_encode_flags("h264_nvenc")
    assert _val(nv, "-c:v") == "h264_nvenc"


def test_intermediate_flags_default_follow_pick_encoder(monkeypatch):
    monkeypatch.setattr(machine, "_detect_gpu", lambda: "metal")
    assert _val(exporter.intermediate_encode_flags(), "-c:v") == "h264_videotoolbox"
    monkeypatch.setattr(machine, "_detect_gpu", lambda: "cpu")
    assert _val(exporter.intermediate_encode_flags(), "-c:v") == "libx264"


def test_export_forces_yuv420p(captured, tmp_path):
    exporter.export(str(tmp_path / "in.mp4"), preset="tiktok", out_path=str(tmp_path / "out.mp4"))
    assert _val(captured["argv"], "-pix_fmt") == "yuv420p"


def test_intermediate_flags_force_yuv420p():
    for enc in ("h264_videotoolbox", "h264_nvenc", "libx264"):
        flags = exporter.intermediate_encode_flags(enc)
        assert flags[flags.index("-pix_fmt") + 1] == "yuv420p", enc
