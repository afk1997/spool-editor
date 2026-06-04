"""Tests for the tiny shared filesystem/url helpers in util.py."""
from __future__ import annotations

import os

from util import link_or_copy


def test_link_or_copy_hardlinks_on_same_filesystem(tmp_path):
    # Importing a local file into the download dir must NOT duplicate its bytes
    # when both live on the same filesystem (spec §5 P3 watch-folder): a hardlink
    # gives a second name for the same inode — zero extra disk, reads identically.
    src = tmp_path / "orig.mp4"
    src.write_bytes(b"video-bytes")
    dst = tmp_path / "imported.mp4"

    link_or_copy(str(src), str(dst))

    assert dst.read_bytes() == b"video-bytes"
    assert os.stat(src).st_ino == os.stat(dst).st_ino   # same inode = no duplication
    assert os.stat(src).st_nlink >= 2                   # the original now has a second link


def test_link_or_copy_falls_back_to_copy_across_filesystems(tmp_path, monkeypatch):
    # When a hardlink is impossible (e.g. the watched folder is on another volume →
    # EXDEV), it must still land a real, independent copy of the bytes.
    src = tmp_path / "orig.mp4"
    src.write_bytes(b"abc")
    dst = tmp_path / "out.mp4"

    def _no_hardlink(a, b):
        raise OSError(18, "Cross-device link")          # errno.EXDEV

    monkeypatch.setattr(os, "link", _no_hardlink)
    link_or_copy(str(src), str(dst))

    assert dst.read_bytes() == b"abc"
    assert os.stat(src).st_ino != os.stat(dst).st_ino   # an independent copy, not a link


def test_link_or_copy_overwrites_existing_destination(tmp_path, monkeypatch):
    # The copy fallback path must tolerate a pre-existing dst (os.link would raise
    # EEXIST) so a re-import never errors out.
    src = tmp_path / "orig.mp4"
    src.write_bytes(b"new")
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"stale")

    def _no_hardlink(a, b):
        raise OSError(18, "Cross-device link")

    monkeypatch.setattr(os, "link", _no_hardlink)
    link_or_copy(str(src), str(dst))

    assert dst.read_bytes() == b"new"
