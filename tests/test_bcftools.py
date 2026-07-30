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
