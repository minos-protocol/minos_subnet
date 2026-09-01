"""Reference-data tarballs are extracted as root. A member that escapes the
destination writes anywhere on the box.

These drive the real `_download_and_extract` rather than a copy of its checks —
a re-implementation passes just as happily when the guard it mirrors has been
deleted.
"""
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def extractor(monkeypatch):
    """The real Setup object, with only the console silenced."""
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location("minos_setup", "setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = next(
        c for c in vars(mod).values()
        if isinstance(c, type) and hasattr(c, "_download_and_extract")
    )
    obj = object.__new__(cls)
    obj.console = types.SimpleNamespace(print=lambda *a, **k: None)
    return obj


def _archive(tmp_path, build):
    """Every archive also carries a legitimate `dest/` directory.

    Without it the extractor returns False anyway -- it verifies target_dir
    exists afterwards -- so a refusal test would pass whether the guard fired
    or not. With it, an accepted archive creates `dest/` and returns True, so
    False means the guard actually refused.
    """
    src = tmp_path / "payload"
    src.mkdir(exist_ok=True)
    good = src / "good"
    good.mkdir(exist_ok=True)
    (good / "ref.fa").write_text(">chr20\nACGT\n")

    tar_path = tmp_path / "data.tar.gz"
    # gzip: the extractor opens "r:gz". An uncompressed archive fails to open,
    # which looks exactly like the guard refusing it.
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(good, arcname="dest")
        build(tar, src)
    return tar_path


def _extract(extractor, tar_path, target):
    """Returns True only if the archive was accepted AND extracted."""
    def download(url, dest, *a, **k):
        Path(dest).write_bytes(Path(tar_path).read_bytes())
        return True
    return extractor._download_and_extract(download, "https://example/data.tar.gz", target)


class TestUnsafeMembersAreRefused:
    def test_a_traversal_name_is_refused(self, extractor, tmp_path):
        def build(tar, src):
            f = src / "ok.txt"; f.write_text("x")
            tar.add(f, arcname="../escaped.txt")
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not (tmp_path / "escaped.txt").exists()
        assert not target.exists(), "refused archives must extract nothing"

    def test_an_absolute_name_is_refused(self, extractor, tmp_path):
        def build(tar, src):
            # TarInfo directly: tar.add() strips a leading slash by design, so
            # arcname="/etc/x" would arrive as a harmless relative name.
            import io
            info = tarfile.TarInfo("/etc/planted.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not target.exists(), "refused archives must extract nothing"

    @pytest.mark.parametrize("kind,mk", [
        ("symlink", lambda: tarfile.SYMTYPE),
        ("hardlink", lambda: tarfile.LNKTYPE),
    ])
    def test_a_link_member_is_refused(self, extractor, tmp_path, kind, mk):
        """Links, devices and FIFOs are each a way out of the destination, and
        reference data has no legitimate use for any of them."""
        def build(tar, src):
            info = tarfile.TarInfo("link")
            info.type = mk()
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not target.exists(), "refused archives must extract nothing"

    def test_a_device_member_is_refused(self, extractor, tmp_path):
        def build(tar, src):
            info = tarfile.TarInfo("dev")
            info.type = tarfile.CHRTYPE
            info.devmajor, info.devminor = 1, 3
            tar.addfile(info)
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not target.exists(), "refused archives must extract nothing"


class TestTheExplicitGuardStandsAlone:
    """On 3.12+ `extractall(filter="data")` refuses these too, so a test that
    only checks the outcome passes even with the explicit pass deleted. These
    disable the interpreter filter, which is the situation on an older
    interpreter — the argument is silently ignored there.
    """

    @pytest.fixture
    def no_filter(self, monkeypatch):
        real = tarfile.TarFile.extractall

        def unfiltered(self, *a, **kw):
            kw.pop("filter", None)
            return real(self, *a, **kw)

        monkeypatch.setattr(tarfile.TarFile, "extractall", unfiltered)

    def test_a_link_member_is_still_refused_without_the_filter(
        self, extractor, tmp_path, no_filter
    ):
        def build(tar, src):
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not target.exists(), "refused archives must extract nothing"

    def test_a_traversal_name_is_still_refused_without_the_filter(
        self, extractor, tmp_path, no_filter
    ):
        def build(tar, src):
            f = src / "ok.txt"; f.write_text("x")
            tar.add(f, arcname="../escaped.txt")
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is False
        assert not (tmp_path / "escaped.txt").exists()
        assert not target.exists(), "refused archives must extract nothing"


class TestOrdinaryArchivesStillWork:
    def test_regular_files_and_directories_extract(self, extractor, tmp_path):
        def build(tar, src):
            pass          # the archive already carries a valid dest/
        target = tmp_path / "dest"
        assert _extract(extractor, _archive(tmp_path, build), target) is True
        assert (target / "ref.fa").read_text().startswith(">chr20")
