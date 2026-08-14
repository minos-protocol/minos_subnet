"""Tests for the BCFtools variant-call template.

These exercise the pre-flight validation in `variant_call` that runs before any
Docker invocation, so they need no bcftools/samtools images.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from templates.bcftools import variant_call


def _touch(path: Path) -> Path:
    path.write_text("")
    return path


class TestBcftoolsCallerMode:
    def _dummy_inputs(self, tmp_path: Path):
        bam = _touch(tmp_path / "sample.bam")
        ref = _touch(tmp_path / "ref.fa")
        _touch(tmp_path / "ref.fa.fai")  # bcftools mpileup requires the .fai
        out = tmp_path / "out.vcf.gz"
        return bam, ref, out

    def test_both_callers_enabled_is_rejected(self, tmp_path):
        """-m and -c are mutually exclusive; enabling both fails pre-flight."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        result = variant_call(
            bam,
            ref,
            out,
            "chr20:1-1000",
            {"bcftools_options": {
                "multiallelic_caller": True,
                "consensus_caller": True,
            }},
        )

        assert result["success"] is False
        assert "mutually exclusive" in result["error"]

    def test_invalid_bcftools_param_is_rejected(self, tmp_path):
        """Out-of-range params are rejected before Docker runs."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        result = variant_call(
            bam,
            ref,
            out,
            "chr20:1-1000",
            {"bcftools_options": {"indel_size": 999}},
        )

        assert result["success"] is False
        assert "Invalid BCFtools parameters" in result["error"]

    def test_both_callers_false_defaults_to_multiallelic(self, tmp_path):
        """With neither caller enabled, `bcftools call` defaults to -m (not -c)."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        with patch("templates.bcftools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            variant_call(
                bam,
                ref,
                out,
                "chr20:1-1000",
                {"bcftools_options": {
                    "multiallelic_caller": False,
                    "consensus_caller": False,
                }},
            )

        # The last subprocess.run is the mpileup|call|norm pipeline; its shell
        # script must invoke `bcftools call` with the multiallelic caller (-m,
        # here as the combined -mv) and never the consensus caller (-c).
        assert mock_run.call_args_list, "expected bcftools to be invoked"
        script = mock_run.call_args_list[-1].args[0][-1]
        assert "bcftools call" in script
        call_segment = script.split("bcftools call", 1)[1].split("|", 1)[0]
        assert "-m" in call_segment
        assert "-c" not in call_segment


class TestBcftoolsDifficultRegionsMask:
    """difficult_regions_mask renders a post-call exclude step in the pipeline."""

    def _dummy_inputs(self, tmp_path: Path):
        bam = _touch(tmp_path / "sample.bam")
        ref = _touch(tmp_path / "ref.fa")
        _touch(tmp_path / "ref.fa.fai")  # bcftools mpileup requires the .fai
        out = tmp_path / "out.vcf.gz"
        return bam, ref, out

    def test_mask_renders_post_call_exclude_step(self, tmp_path):
        """A configured mask adds `bcftools view -T ^` after norm + a ro mount."""
        bam, ref, out = self._dummy_inputs(tmp_path)
        mask_dir = tmp_path / "masks"
        mask_dir.mkdir()
        mask = _touch(mask_dir / "hard_regions.bed")

        with patch("templates.bcftools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            variant_call(
                bam,
                ref,
                out,
                "chr20:1-1000",
                {"bcftools_options": {"difficult_regions_mask": str(mask)}},
            )

        assert mock_run.call_args_list, "expected bcftools to be invoked"
        argv = mock_run.call_args_list[-1].args[0]
        script = argv[-1]
        # The exclude step runs after norm, targets the container mask mount,
        # and writes the final VCF.
        assert "bcftools view" in script
        assert "-T ^/mask/hard_regions.bed" in script
        assert "| bcftools view" in script
        assert "bcftools index" in script
        # The mask directory is mounted read-only into the container.
        assert f"{mask_dir.resolve()}:/mask:ro" in argv

    def test_no_mask_leaves_pipeline_unchanged(self, tmp_path):
        """Without the parameter the pipeline stays mpileup|call|norm."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        with patch("templates.bcftools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            variant_call(
                bam,
                ref,
                out,
                "chr20:1-1000",
                {"bcftools_options": {"min_BQ": 13}},
            )

        assert mock_run.call_args_list, "expected bcftools to be invoked"
        argv = mock_run.call_args_list[-1].args[0]
        script = argv[-1]
        assert "bcftools view" not in script
        assert "bcftools norm" in script
        assert not any(a.endswith(":/mask:ro") for a in argv)

    def test_missing_mask_file_fails_preflight(self, tmp_path):
        """A configured mask that does not exist fails before Docker runs."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        with patch("templates.bcftools.subprocess.run") as mock_run:
            result = variant_call(
                bam,
                ref,
                out,
                "chr20:1-1000",
                {"bcftools_options": {
                    "difficult_regions_mask": str(tmp_path / "masks" / "absent.bed"),
                }},
            )

        assert result["success"] is False
        assert "Difficult-regions mask not found" in result["error"]
        mock_run.assert_not_called()

    def test_invalid_mask_value_rejected(self, tmp_path):
        """Unsafe mask values fail parameter validation before Docker runs."""
        bam, ref, out = self._dummy_inputs(tmp_path)

        result = variant_call(
            bam,
            ref,
            out,
            "chr20:1-1000",
            {"bcftools_options": {"difficult_regions_mask": "bad;name.bed"}},
        )

        assert result["success"] is False
        assert "Invalid BCFtools parameters" in result["error"]
