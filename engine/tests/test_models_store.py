import hashlib
import os
import threading
import pytest
from pathlib import Path
import models_store
from network_policy import NetworkPolicy, NetworkPolicyError


@pytest.fixture()
def tmp_models_dir(tmp_path, monkeypatch):
    """Re-point models_store at an empty temp dir for each test."""
    monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def network_policy():
    return NetworkPolicy()


def test_list_installed_empty(tmp_models_dir):
    assert models_store.list_installed() == []


def test_list_installed_returns_only_ggml_bins(tmp_models_dir):
    (tmp_models_dir / "ggml-tiny.bin").write_bytes(b"x")
    (tmp_models_dir / "ggml-base.bin").write_bytes(b"x")
    (tmp_models_dir / "junk.txt").write_bytes(b"x")
    assert sorted(models_store.list_installed()) == ["ggml-base.bin", "ggml-tiny.bin"]


def test_get_active_returns_none_when_no_active_file(tmp_models_dir):
    assert models_store.get_active() is None


def test_set_and_get_active(tmp_models_dir):
    (tmp_models_dir / "ggml-base.bin").write_bytes(b"x")
    models_store.set_active("ggml-base.bin")
    assert models_store.get_active() == "ggml-base.bin"
    assert (tmp_models_dir / "ACTIVE").read_text().strip() == "ggml-base.bin"


def test_set_active_refuses_uninstalled_model(tmp_models_dir):
    with pytest.raises(FileNotFoundError):
        models_store.set_active("ggml-medium.bin")


def test_get_active_path(tmp_models_dir):
    (tmp_models_dir / "ggml-base.bin").write_bytes(b"x")
    models_store.set_active("ggml-base.bin")
    p = models_store.get_active_path()
    assert p is not None
    assert p.name == "ggml-base.bin"


def test_remove_deletes_file_and_clears_active_if_active(tmp_models_dir):
    (tmp_models_dir / "ggml-base.bin").write_bytes(b"x")
    models_store.set_active("ggml-base.bin")
    models_store.remove("ggml-base.bin")
    assert not (tmp_models_dir / "ggml-base.bin").exists()
    assert models_store.get_active() is None
    assert not (tmp_models_dir / "ACTIVE").exists()


def test_remove_keeps_active_if_removing_other(tmp_models_dir):
    (tmp_models_dir / "ggml-base.bin").write_bytes(b"x")
    (tmp_models_dir / "ggml-tiny.bin").write_bytes(b"x")
    models_store.set_active("ggml-base.bin")
    models_store.remove("ggml-tiny.bin")
    assert models_store.get_active() == "ggml-base.bin"


def test_known_models_metadata():
    """KNOWN_MODELS exposes a dict with size, hf_url, sha256 per model."""
    for name in ("ggml-tiny.bin", "ggml-base.bin", "ggml-small.bin", "ggml-medium.bin"):
        assert name in models_store.KNOWN_MODELS
        meta = models_store.KNOWN_MODELS[name]
        assert "size_bytes" in meta
        assert "hf_url" in meta
        assert meta["hf_url"].startswith("https://huggingface.co/ggerganov/whisper.cpp")
        assert len(meta["sha256"]) == 64
        assert all(c in "0123456789abcdef" for c in meta["sha256"].lower())


def _fake_response(payload: bytes, total_size: int | None = None):
    """Build a fake urlopen response with .read(n) and .headers."""
    class FakeResp:
        def __init__(self, data, total):
            self._buf = data
            self._idx = 0
            self.headers = {"Content-Length": str(total)} if total is not None else {}

        def read(self, n=-1):
            if n < 0 or n > len(self._buf) - self._idx:
                chunk = self._buf[self._idx:]
                self._idx = len(self._buf)
            else:
                chunk = self._buf[self._idx:self._idx + n]
                self._idx += n
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return FakeResp(payload, total_size if total_size is not None else len(payload))


def test_download_writes_atomic_with_progress(tmp_models_dir, monkeypatch, network_policy):
    """download() saves the file with atomic rename and emits progress."""
    payload = b"FAKEMODELDATA" * 1000
    monkeypatch.setattr(
        models_store, "urlopen",
        lambda url, timeout=None: _fake_response(payload, len(payload))
    )

    progress_events = []

    def cb(received: int, total: int):
        progress_events.append((received, total))

    target = "ggml-tiny.bin"
    # Skip SHA verification for this test by passing verify=False
    models_store.download(
        target, network_policy=network_policy, progress_cb=cb, verify=False
    )

    final = tmp_models_dir / target
    assert final.exists()
    assert final.read_bytes() == payload
    # No leftover .part files
    assert not (tmp_models_dir / (target + ".part")).exists()
    # Progress was emitted at least once and final progress equals total
    assert progress_events
    assert progress_events[-1] == (len(payload), len(payload))


def test_download_verifies_sha256(tmp_models_dir, monkeypatch, network_policy):
    """download() rejects the file if SHA-256 doesn't match KNOWN_MODELS metadata."""
    payload = b"WRONGDATA" * 100
    monkeypatch.setattr(
        models_store, "urlopen",
        lambda url, timeout=None: _fake_response(payload, len(payload))
    )

    # The KNOWN_MODELS sha256 won't match this random payload
    with pytest.raises(ValueError, match="sha-?256"):
        models_store.download(
            "ggml-tiny.bin", network_policy=network_policy, verify=True
        )

    # No file should be left behind
    assert not (tmp_models_dir / "ggml-tiny.bin").exists()
    assert not (tmp_models_dir / "ggml-tiny.bin.part").exists()


def test_download_writes_sha256_sidecar(tmp_models_dir, monkeypatch, network_policy):
    payload = b"SOMECONTENT" * 500
    monkeypatch.setattr(
        models_store, "urlopen",
        lambda url, timeout=None: _fake_response(payload, len(payload))
    )
    models_store.download(
        "ggml-tiny.bin", network_policy=network_policy, verify=False
    )
    sha = hashlib.sha256(payload).hexdigest()
    assert (tmp_models_dir / "ggml-tiny.bin.sha256").read_text().strip() == sha


def test_download_unknown_model_raises(network_policy):
    with pytest.raises(ValueError, match="unknown model"):
        models_store.download("ggml-foo.bin", network_policy=network_policy)


def test_download_offline_performs_no_network_or_model_mutation(
    tmp_models_dir, monkeypatch
):
    existing = "ggml-tiny.bin"
    target = "ggml-base.bin"
    (tmp_models_dir / existing).write_bytes(b"already-installed")
    models_store.set_active(existing)
    before = {path.name: path.read_bytes() for path in tmp_models_dir.iterdir()}
    calls = []

    def forbidden_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("offline model install reached urlopen")

    monkeypatch.setattr(models_store, "urlopen", forbidden_urlopen)
    policy = NetworkPolicy(offline=True)

    with pytest.raises(NetworkPolicyError) as denied:
        models_store.download(target, network_policy=policy, verify=False)

    assert denied.value.code == "offline_network_disabled"
    assert calls == []
    assert {path.name: path.read_bytes() for path in tmp_models_dir.iterdir()} == before
    assert models_store.get_active() == existing


def test_download_holds_model_lease_through_stream_and_releases_on_success(
    tmp_models_dir, monkeypatch, network_policy
):
    read_started = threading.Event()
    release_read = threading.Event()
    payload = b"model-data"

    class BlockingResponse:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._read = False

        def read(self, _size=-1):
            if self._read:
                return b""
            self._read = True
            read_started.set()
            assert release_read.wait(2)
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(models_store, "urlopen", lambda *a, **k: BlockingResponse())
    failures = []

    def run_download():
        try:
            models_store.download(
                "ggml-tiny.bin", network_policy=network_policy, verify=False
            )
        except BaseException as exc:  # expose worker failures to the test thread
            failures.append(exc)

    worker = threading.Thread(target=run_download)
    worker.start()
    try:
        assert read_started.wait(2)
        assert network_policy.active_leases == 1
        assert (tmp_models_dir / "ggml-tiny.bin.part").exists()
    finally:
        release_read.set()
        worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert network_policy.active_leases == 0
    assert (tmp_models_dir / "ggml-tiny.bin").read_bytes() == payload


def test_download_releases_model_lease_and_partial_file_on_stream_error(
    tmp_models_dir, monkeypatch, network_policy
):
    class FailingResponse:
        headers = {}

        def read(self, _size=-1):
            assert network_policy.active_leases == 1
            raise OSError("connection reset")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(models_store, "urlopen", lambda *a, **k: FailingResponse())

    with pytest.raises(OSError, match="connection reset"):
        models_store.download(
            "ggml-tiny.bin", network_policy=network_policy, verify=False
        )

    assert network_policy.active_leases == 0
    assert not (tmp_models_dir / "ggml-tiny.bin").exists()
    assert not (tmp_models_dir / "ggml-tiny.bin.part").exists()
