"""Download integrity tests for utils.file_utils.

These exercise the real download path against a local socket server that can
lie about Content-Length, because the failure mode being guarded against only
shows up at the HTTP layer: CPython's HTTPResponse.read(amt) calls _close_conn()
on a premature close instead of raising IncompleteRead, so a truncated body
looks exactly like a finished one to the read loop.
"""

import hashlib
import socket
import threading

import pytest

from pathlib import Path

from utils import file_utils
from utils.file_utils import (
    download_file,
    download_file_verified,
    download_file_with_fallback,
)


class CannedServer:
    """Minimal HTTP server that serves fixed, optionally dishonest responses.

    routes maps path -> {"body": bytes, "declared": int|None, "send": bytes,
    "no_content_length": bool}.
    """

    def __init__(self, routes):
        self.routes = routes
        self.hits = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            request_line = data.split(b"\r\n", 1)[0].decode("latin-1")
            method, path = request_line.split(" ")[:2]
            self.hits.append((method, path))

            spec = self.routes.get(path)
            if spec is None:
                conn.sendall(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            body = spec["body"]
            payload = spec.get("send", body)
            if spec.get("no_content_length"):
                header = "HTTP/1.0 200 OK\r\nConnection: close\r\n\r\n"
            else:
                declared = spec.get("declared", len(body))
                header = (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {declared}\r\n"
                    "Connection: close\r\n\r\n"
                )
            conn.sendall(header.encode("latin-1"))
            if method != "HEAD":
                conn.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def server():
    """Server whose /full route is honest and whose /truncated route lies."""
    body = b"C" * 1000
    srv = CannedServer({
        "/full": {"body": body},
        "/truncated": {"body": body, "declared": 1000, "send": b"C" * 100},
        "/nolen": {"body": body, "no_content_length": True},
        "/empty": {"body": b""},
    })
    yield srv
    srv.close()


BODY = b"C" * 1000
BODY_SHA = hashlib.sha256(BODY).hexdigest()
WRONG_SHA = "0" * 64


def _partial(path):
    return path.with_name(path.name + ".part")


class _FakeTqdm:
    """Stand-in so the progress-bar read loop is covered even without tqdm."""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, n):
        pass


def _force_progress_branch(monkeypatch):
    import utils.file_utils as fu

    monkeypatch.setattr(fu, "HAS_TQDM", True)
    monkeypatch.setattr(fu, "tqdm", _FakeTqdm, raising=False)


@pytest.mark.parametrize("show_progress", [False, True])
def test_truncated_body_fails(tmp_path, server, monkeypatch, show_progress):
    """Server advertises 1000 bytes and sends 100: must NOT report success."""
    if show_progress:
        _force_progress_branch(monkeypatch)
    dest = tmp_path / "sample.bam"
    result = download_file(
        server.url("/truncated"), dest, use_cache=False, show_progress=show_progress
    )

    assert result is None
    assert not dest.exists()
    assert not _partial(dest).exists()


def test_truncated_body_leaves_no_partial_for_next_run(tmp_path, server):
    """The next attempt must not cache-hit on the bytes of a failed download."""
    dest = tmp_path / "truth.vcf.gz.tbi"
    assert download_file(server.url("/truncated"), dest, show_progress=False) is None

    # use_cache=True is the real caller's default; with a leftover partial at
    # the final path this returned the corrupt file without touching the network.
    server.hits.clear()
    second = download_file(server.url("/truncated"), dest, show_progress=False)
    assert second is None
    assert ("GET", "/truncated") in server.hits


@pytest.mark.parametrize("show_progress", [False, True])
def test_successful_download(tmp_path, server, monkeypatch, show_progress):
    if show_progress:
        _force_progress_branch(monkeypatch)
    dest = tmp_path / "sample.bam"
    result = download_file(server.url("/full"), dest, show_progress=show_progress)

    assert result == dest
    assert dest.read_bytes() == BODY
    assert not _partial(dest).exists()


def test_download_without_content_length(tmp_path, server):
    """A close-delimited HTTP/1.0 response has no length to verify against."""
    dest = tmp_path / "nolen.bin"
    result = download_file(server.url("/nolen"), dest, show_progress=False)

    assert result == dest
    assert dest.read_bytes() == BODY


def test_verified_download_correct_hash(tmp_path, server):
    dest = tmp_path / "sample.bam"
    result = download_file_verified(
        server.url("/full"), dest, expected_sha256=BODY_SHA, show_progress=False
    )

    assert result == dest
    assert dest.read_bytes() == BODY


def test_verified_download_wrong_hash_rejected_when_enforced(tmp_path, server, monkeypatch):
    """A fresh download is hashed, not just the cache branch."""
    monkeypatch.setenv("MINOS_ENFORCE_DOWNLOAD_SHA256", "1")
    dest = tmp_path / "sample.bam"
    result = download_file_verified(
        server.url("/full"), dest, expected_sha256=WRONG_SHA, show_progress=False
    )

    assert result is None
    assert not dest.exists()
    assert not _partial(dest).exists()


def test_verified_download_wrong_hash_warns_by_default(tmp_path, server, monkeypatch):
    """Enforcement is opt-in: the published digest has not been exercised on
    this path, so a mismatch must not fail every download by default."""
    monkeypatch.delenv("MINOS_ENFORCE_DOWNLOAD_SHA256", raising=False)
    dest = tmp_path / "sample.bam"
    result = download_file_verified(
        server.url("/full"), dest, expected_sha256=WRONG_SHA, show_progress=False
    )

    assert result == dest
    assert dest.read_bytes() == BODY


def test_truncation_is_rejected_regardless_of_digest_enforcement(tmp_path, server, monkeypatch):
    """The length check is independent of the digest flag."""
    monkeypatch.delenv("MINOS_ENFORCE_DOWNLOAD_SHA256", raising=False)
    dest = tmp_path / "sample.bam"
    result = download_file_verified(
        server.url("/truncated"), dest, expected_sha256=None, show_progress=False
    )

    assert result is None
    assert not dest.exists()


@pytest.mark.parametrize("supplied", [BODY_SHA.upper(), f"  {BODY_SHA}  "])
def test_digest_comparison_is_normalised(tmp_path, server, supplied):
    """An uppercase or padded digest that is otherwise correct must match."""
    dest = tmp_path / "sample.bam"
    assert download_file_verified(
        server.url("/full"), dest, expected_sha256=supplied, show_progress=False
    ) == dest


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_digest_is_treated_as_absent(tmp_path, server, blank):
    """A placeholder must not become a digest that can never match."""
    dest = tmp_path / "sample.bam"
    assert download_file_verified(
        server.url("/full"), dest, expected_sha256=blank, show_progress=False
    ) == dest


def test_verified_cache_hit_matching_hash_skips_network(tmp_path, server):
    dest = tmp_path / "sample.bam"
    dest.write_bytes(BODY)
    server.hits.clear()

    result = download_file_verified(
        server.url("/full"), dest, expected_sha256=BODY_SHA, show_progress=False
    )

    assert result == dest
    assert server.hits == []


def test_verified_cache_hit_no_hash_skips_network(tmp_path, server):
    dest = tmp_path / "sample.bam"
    dest.write_bytes(b"anything")
    server.hits.clear()

    result = download_file_verified(server.url("/full"), dest, show_progress=False)

    assert result == dest
    assert server.hits == []


def test_verified_stale_cache_is_replaced(tmp_path, server):
    """Wrong-hash file on disk is re-downloaded and the good body wins."""
    dest = tmp_path / "sample.bam"
    dest.write_bytes(b"stale")

    result = download_file_verified(
        server.url("/full"), dest, expected_sha256=BODY_SHA, show_progress=False
    )

    assert result == dest
    assert dest.read_bytes() == BODY


def test_fallback_uses_backup_after_truncated_primary(tmp_path, server):
    dest = tmp_path / "sample.bam"
    result = download_file_with_fallback(
        server.url("/truncated"),
        dest,
        backup_url=server.url("/full"),
        expected_sha256=BODY_SHA,
        show_progress=False,
    )

    assert result == dest
    assert dest.read_bytes() == BODY


def test_s3_branch_commits_only_on_success(tmp_path, monkeypatch):
    """The s3:// path also stages through .part before committing."""
    import utils.file_utils as fu

    def fake_s3(uri, staged_path, show_progress=True):
        staged_path.write_bytes(BODY)
        assert staged_path.name.endswith(".part")
        return staged_path

    monkeypatch.setattr(fu, "_download_from_s3_uri", fake_s3)
    dest = tmp_path / "sample.bam"
    assert fu.download_file("s3://bucket/key", dest, show_progress=False) == dest
    assert dest.read_bytes() == BODY
    assert not _partial(dest).exists()


def test_s3_branch_failure_leaves_nothing(tmp_path, monkeypatch):
    import utils.file_utils as fu

    def fake_s3(uri, staged_path, show_progress=True):
        staged_path.write_bytes(b"half")
        return None

    monkeypatch.setattr(fu, "_download_from_s3_uri", fake_s3)
    dest = tmp_path / "sample.bam"
    assert fu.download_file("s3://bucket/key", dest, show_progress=False) is None
    assert not dest.exists()
    assert not _partial(dest).exists()


class TestABadPrimaryFallsBackToTheBackup:
    """A digest mismatch on the primary used to be accepted outright, which
    returned the bad bytes and never asked the backup -- defeating the point of
    having two sources."""

    @staticmethod
    def _serve(mapping):
        """Patch download_file to write per-URL content, so no network is used."""
        def fake(url, local_path, use_cache=False, show_progress=True):
            payload = mapping.get(url)
            if payload is None:
                return None
            Path(local_path).write_bytes(payload)
            return Path(local_path)
        return fake

    def _digest(self, b):
        import hashlib
        return hashlib.sha256(b).hexdigest()

    def test_a_good_backup_replaces_a_corrupt_primary(self, tmp_path, monkeypatch):
        good, bad = b"the real file", b"corrupted"
        monkeypatch.setattr(file_utils, "download_file",
                            self._serve({"P": bad, "B": good}))
        out = file_utils.download_file_with_fallback(
            "P", tmp_path / "f.bin", backup_url="B",
            expected_sha256=self._digest(good), show_progress=False)
        assert out is not None
        assert out.read_bytes() == good, "kept the corrupt primary copy"
        assert not (tmp_path / "f.bin.primary").exists(), "left a quarantine file behind"

    def test_a_corrupt_primary_is_kept_when_the_backup_is_dead(self, tmp_path, monkeypatch):
        """Leniency is preserved: with nothing better available, the mismatching
        bytes are still returned rather than failing the round."""
        good, bad = b"the real file", b"corrupted"
        monkeypatch.setattr(file_utils, "download_file",
                            self._serve({"P": bad}))
        out = file_utils.download_file_with_fallback(
            "P", tmp_path / "f.bin", backup_url="B",
            expected_sha256=self._digest(good), show_progress=False)
        assert out is not None, "dropped usable bytes when the backup was dead"
        assert out.read_bytes() == bad
        assert not (tmp_path / "f.bin.primary").exists()

    def test_with_no_backup_the_primary_is_accepted_as_before(self, tmp_path, monkeypatch):
        good, bad = b"the real file", b"corrupted"
        monkeypatch.setattr(file_utils, "download_file",
                            self._serve({"P": bad}))
        out = file_utils.download_file_with_fallback(
            "P", tmp_path / "f.bin", backup_url=None,
            expected_sha256=self._digest(good), show_progress=False)
        assert out is not None and out.read_bytes() == bad

    def test_a_matching_primary_never_touches_the_backup(self, tmp_path, monkeypatch):
        good = b"the real file"
        calls = []
        def fake(url, local_path, use_cache=False, show_progress=True):
            calls.append(url)
            Path(local_path).write_bytes(good)
            return Path(local_path)
        monkeypatch.setattr(file_utils, "download_file", fake)
        out = file_utils.download_file_with_fallback(
            "P", tmp_path / "f.bin", backup_url="B",
            expected_sha256=self._digest(good), show_progress=False)
        assert out is not None
        assert calls == ["P"], f"fetched the backup unnecessarily: {calls}"

    def test_a_non_string_digest_does_not_crash_the_cache_check(self, tmp_path):
        """The platform supplies this value; an int or dict there must not turn
        a cache miss into a TypeError."""
        f = tmp_path / "f.bin"
        f.write_bytes(b"whatever")
        for bad in (12345, {"a": 1}, ["x"], 3.14):
            file_utils.download_file_verified(
                "http://127.0.0.1:1/nope", f, expected_sha256=bad, show_progress=False)
