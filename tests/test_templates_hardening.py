"""Hardening tests for the variant-caller templates.

Every test here stubs `subprocess.run`, so no Docker image is needed. The
behaviour under test is what the template does with the *result* of a docker
run: whether it notices a failure, which container it reaps, and what resource
limits it asked the daemon for.
"""

import gzip
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import templates._common as _common
import templates.deepvariant as deepvariant
from templates._common import count_variants
from templates.freebayes import variant_call as freebayes_call
from templates.gatk import variant_call as gatk_call


def _inputs(tmp_path: Path):
    """A BAM/reference set that passes the template pre-flight checks."""
    bam = tmp_path / "sample.bam"
    bam.write_text("")
    (tmp_path / "sample.bam.bai").write_text("")
    ref = tmp_path / "ref.fa"
    ref.write_text("")
    (tmp_path / "ref.fa.fai").write_text("")
    out = tmp_path / "out.vcf.gz"
    return bam, ref, out


def _gzip_vcf(path: Path, n_variants: int) -> None:
    with gzip.open(path, "wt") as fh:
        fh.write("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n")
        for i in range(n_variants):
            fh.write(f"chr20\t{i + 1}\t.\tA\tG\n")


class _FreebayesDocker:
    """Stub for the two `docker run` calls freebayes.py makes.

    Call 1 is freebayes itself (stdout redirected to the temp VCF by the
    template). Call 2 is the bgzip/bcftools compress step, which in reality
    creates the output file via a shell redirect *before* doing any work — so
    the stub creates it too, however the step ends.
    """

    def __init__(self, out: Path, compress_rc=0, compress_stderr="",
                 compress_bytes=b"", compress_timeout=False,
                 freebayes_timeout=False):
        self.out = out
        self.compress_rc = compress_rc
        self.compress_stderr = compress_stderr
        self.compress_bytes = compress_bytes
        self.compress_timeout = compress_timeout
        self.freebayes_timeout = freebayes_timeout
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        is_compress = any("bcftools" in part for part in cmd)
        if is_compress:
            if self.compress_timeout:
                raise subprocess.TimeoutExpired(cmd, 120)
            self.out.write_bytes(self.compress_bytes)
            return MagicMock(returncode=self.compress_rc,
                             stderr=self.compress_stderr, stdout="")
        if self.freebayes_timeout:
            raise subprocess.TimeoutExpired(cmd, 1800)
        kwargs["stdout"].write("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n"
                               + "chr20\t1\t.\tA\tG\n" * 20)
        return MagicMock(returncode=0, stderr="")

    @property
    def freebayes_cmd(self):
        return self.commands[0]

    @property
    def compress_cmd(self):
        return self.commands[1]


class TestFreebayesCompressFailure:
    def test_failed_compression_is_not_reported_as_an_empty_callset(self, tmp_path):
        """rc!=0 from bgzip must fail the round, not score 0 variants.

        The shell redirect creates out.vcf.gz before bgzip writes anything,
        so an existence check alone would pass, and gzip.open on the 0-byte
        file returns '' without raising. The return code is what separates
        them.
        """
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_rc=125,
                                  compress_stderr="docker: no space left on device")

        with patch("templates.freebayes.subprocess.run", side_effect=docker):
            result = freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert result["success"] is False
        assert result["variant_count"] == 0
        assert "rc=125" in result["error"]
        assert "no space left on device" in result["error"]

    def test_partial_output_is_removed_so_it_cannot_be_scored(self, tmp_path):
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_rc=1, compress_stderr="killed",
                                  compress_bytes=b"\x1f\x8b\x08\x00trunc")

        with patch("templates.freebayes.subprocess.run", side_effect=docker):
            freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert not out.exists()

    def test_image_pull_failure_gets_the_actionable_message(self, tmp_path):
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(
            out, compress_rc=125,
            compress_stderr="Unable to find image 'quay.io/biocontainers/bcftools:1.20' locally",
        )

        with patch("templates.freebayes.subprocess.run", side_effect=docker):
            result = freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert result["success"] is False
        assert "docker pull" in result["error"]

    def test_successful_compression_still_succeeds(self, tmp_path):
        """The rc branch must not break the happy path."""
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_rc=0)

        def _make_real_vcf(cmd, **kwargs):
            if any("bcftools" in part for part in cmd):
                _gzip_vcf(out, 7)
                return MagicMock(returncode=0, stderr="", stdout="")
            return docker(cmd, **kwargs)

        with patch("templates.freebayes.subprocess.run", side_effect=_make_real_vcf):
            result = freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert result["success"] is True
        assert result["variant_count"] == 7


class TestFreebayesTimeoutReaping:
    def test_compress_timeout_reaps_the_compress_container(self, tmp_path):
        """The container that timed out is freebayes-compress, not freebayes."""
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_timeout=True)

        with patch("templates.freebayes.subprocess.run", side_effect=docker), \
                patch("templates.freebayes.reap_container") as reap:
            result = freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert result["success"] is False
        reaped = {call.args[0] for call in reap.call_args_list}
        compress_name = docker.compress_cmd[docker.compress_cmd.index("--name") + 1]
        assert compress_name in reaped, f"compress container not reaped: {reaped}"
        assert "freebayes-compress" in compress_name

    def test_compress_timeout_removes_the_partial_output(self, tmp_path):
        bam, ref, out = _inputs(tmp_path)
        out.write_bytes(b"")  # what the shell redirect leaves behind

        docker = _FreebayesDocker(out, compress_timeout=True)
        with patch("templates.freebayes.subprocess.run", side_effect=docker), \
                patch("templates.freebayes.reap_container"):
            freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert not out.exists()

    def test_freebayes_timeout_reaps_only_the_started_container(self, tmp_path):
        """A timeout before the compress step must not touch an unbound name."""
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, freebayes_timeout=True)

        with patch("templates.freebayes.subprocess.run", side_effect=docker), \
                patch("templates.freebayes.reap_container") as reap:
            result = freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert result["success"] is False
        assert "Timeout" in result["error"]
        reaped = [call.args[0] for call in reap.call_args_list]
        assert len(reaped) == 1
        assert "freebayes-compress" not in reaped[0]


class TestFreebayesMemoryCap:
    def _memory_flag(self, cmd):
        return [part for part in cmd if part.startswith("--memory=")]

    def test_both_containers_are_memory_capped(self, tmp_path):
        """An uncapped container can exceed the share the validator's
        concurrency maths assumed, making the host OOM-killer pick a victim
        other than the offending job."""
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_rc=0)

        with patch("templates.freebayes.subprocess.run", side_effect=docker):
            freebayes_call(bam, ref, out, "chr20:1-1000", {"memory_gb": 6})

        assert self._memory_flag(docker.freebayes_cmd) == ["--memory=6g"]
        assert self._memory_flag(docker.compress_cmd) == ["--memory=6g"]

    def test_memory_cap_is_present_without_explicit_config(self, tmp_path):
        bam, ref, out = _inputs(tmp_path)
        docker = _FreebayesDocker(out, compress_rc=0)

        with patch("templates.freebayes.subprocess.run", side_effect=docker):
            freebayes_call(bam, ref, out, "chr20:1-1000", {})

        assert self._memory_flag(docker.freebayes_cmd)
        assert self._memory_flag(docker.compress_cmd)


class TestGatkHeapHeadroom:
    def _gatk_cmd(self, tmp_path, config):
        bam, ref, out = _inputs(tmp_path)
        captured = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return MagicMock(returncode=1, stderr="stub", stdout="")

        with patch("templates.gatk.subprocess.run", side_effect=_run):
            gatk_call(bam, ref, out, "chr20:1-1000", config)
        return captured["cmd"]

    def _mb(self, token, suffix_mb=True):
        if token.endswith("m"):
            return int(token[:-1])
        if token.endswith("g"):
            return int(token[:-1]) * 1024
        raise AssertionError(f"unparsable size: {token}")

    @pytest.mark.parametrize("memory_gb", [1, 2, 4, 16, 64])
    def test_heap_leaves_headroom_under_the_container_limit(self, tmp_path, memory_gb):
        """-Xmx == --memory means the cgroup kills the JVM before it can throw
        a manageable OutOfMemoryError; the heap must sit strictly below."""
        cmd = self._gatk_cmd(tmp_path, {"memory_gb": memory_gb})

        limit = self._mb(cmd[cmd.index("--memory=%dg" % memory_gb)].split("=")[1])
        xmx = next(p for p in cmd if p.startswith("-Xmx"))
        heap = self._mb(xmx[len("-Xmx"):])

        assert heap < limit, f"{xmx} leaves no headroom under {limit}m"
        assert heap >= limit * 0.5, f"{xmx} wastes more than half of {limit}m"

    def test_heap_is_still_usable_on_a_small_container(self, tmp_path):
        """Rounding the fraction down in whole GB would give a 2g container a
        1g heap; MB granularity keeps it usable."""
        cmd = self._gatk_cmd(tmp_path, {"memory_gb": 2})
        xmx = next(p for p in cmd if p.startswith("-Xmx"))
        assert self._mb(xmx[len("-Xmx"):]) > 1024


class TestCountVariantsOnBrokenGzip:
    def test_truncated_gzip_is_reported_not_raised(self, tmp_path):
        """A killed/ENOSPC compression leaves a mid-stream truncation, which
        raises EOFError or zlib.error — neither is a BadGzipFile."""
        path = tmp_path / "truncated.vcf.gz"
        _gzip_vcf(path, 500)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])

        # Sanity: the raw read really does raise something outside the old tuple.
        with pytest.raises(Exception) as exc:
            with gzip.open(path, "rt") as fh:
                fh.read()
        assert not isinstance(exc.value, gzip.BadGzipFile)

        assert count_variants(path) >= 0  # must not propagate

    def test_empty_file_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "empty.vcf.gz"
        path.write_bytes(b"")
        assert count_variants(path) == 0

    def test_non_gzip_content_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "garbage.vcf.gz"
        path.write_bytes(b"not a gzip stream at all")
        assert count_variants(path) == 0

    def test_binary_garbage_inside_a_valid_gzip_is_reported_not_raised(self, tmp_path):
        """Decoded as text this raises UnicodeDecodeError, not an OSError."""
        path = tmp_path / "binary.vcf.gz"
        with gzip.open(path, "wb") as fh:
            fh.write(bytes(range(256)) * 8)
        assert count_variants(path) == 0

    def test_valid_gzip_still_counts(self, tmp_path):
        path = tmp_path / "good.vcf.gz"
        _gzip_vcf(path, 11)
        assert count_variants(path) == 11


class TestDeepvariantCountVariantsDeduplicated:
    def test_deepvariant_uses_the_shared_helper(self):
        assert deepvariant.count_variants is _common.count_variants

    def test_deepvariant_defines_no_private_count_variants(self):
        assert not hasattr(deepvariant, "_count_variants")
