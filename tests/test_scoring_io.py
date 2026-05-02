"""
Tests for I/O and parsing functions in utils.scoring:
  - generate_synthetic_regions_bed
  - subset_bed
  - parse_happy_vcf_assessed_metrics
  - HappyScorer CSV parsing (via mocked subprocess)
"""

import gzip
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from utils.scoring import (
    generate_challenge_region_bed,
    generate_synthetic_regions_bed,
    parse_region_overcall_metrics,
    parse_happy_vcf_assessed_metrics,
    HappyScorer,
    subset_bed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VCF_HEADER = "##fileformat=VCFv4.2\n"
VCF_COLNAMES = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"

HAPPY_VCF_HEADER = "##fileformat=VCFv4.2\n"
HAPPY_VCF_COLNAMES = (
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
)


def _write_vcf(path: Path, body_lines: list[str], *, gz: bool = False):
    """Write a minimal VCF (plain or gzipped) to *path*."""
    content = VCF_HEADER + VCF_COLNAMES + "".join(body_lines)
    if gz:
        with gzip.open(path, "wt") as f:
            f.write(content)
    else:
        path.write_text(content)


def _write_happy_vcf(path: Path, body_lines: list[str]):
    """Write a minimal hap.py-style gzipped VCF to *path*."""
    content = HAPPY_VCF_HEADER + HAPPY_VCF_COLNAMES + "".join(body_lines)
    with gzip.open(path, "wt") as f:
        f.write(content)


def _read_bed(path: Path) -> list[tuple[str, int, int]]:
    """Parse a BED file into a list of (chrom, start, end) tuples."""
    regions = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        regions.append((parts[0], int(parts[1]), int(parts[2])))
    return regions


# ---------------------------------------------------------------------------
# TestGenerateSyntheticRegionsBed
# ---------------------------------------------------------------------------

class TestGenerateSyntheticRegionsBed:
    """Tests for generate_synthetic_regions_bed."""

    def test_basic_synthetic_vcf(self, tmp_path):
        """Uncompressed VCF with SYNTHETIC in INFO produces correct BED coords."""
        vcf = tmp_path / "truth.vcf"
        bed = tmp_path / "regions.bed"
        _write_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\tSYNTHETIC;SVTYPE=SNP\tGT\t0/1\n",
        ])

        result = generate_synthetic_regions_bed(str(vcf), str(bed), padding=50)

        assert result is True
        regions = _read_bed(bed)
        assert len(regions) == 1
        chrom, start, end = regions[0]
        assert chrom == "chr20"
        # VCF POS=10000100, REF=A (len 1), padding=50
        # BED start = max(0, 10000100 - 1 - 50) = 10000049
        # BED end   = 10000100 - 1 + 1 + 50     = 10000150
        assert start == 10000049
        assert end == 10000150

    def test_gzipped_vcf(self, tmp_path):
        """Gzipped VCF is parsed correctly."""
        vcf = tmp_path / "truth.vcf.gz"
        bed = tmp_path / "regions.bed"
        _write_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\tSYNTHETIC;SVTYPE=SNP\tGT\t0/1\n",
        ], gz=True)

        result = generate_synthetic_regions_bed(str(vcf), str(bed), padding=50)

        assert result is True
        regions = _read_bed(bed)
        assert len(regions) == 1
        assert regions[0] == ("chr20", 10000049, 10000150)

    def test_no_synthetic_variants(self, tmp_path):
        """VCF without SYNTHETIC in INFO returns False and no BED file."""
        vcf = tmp_path / "truth.vcf"
        bed = tmp_path / "regions.bed"
        _write_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\tSVTYPE=SNP\tGT\t0/1\n",
        ])

        result = generate_synthetic_regions_bed(str(vcf), str(bed))

        assert result is False

    def test_overlapping_regions_merged(self, tmp_path):
        """Two SYNTHETIC mutations close enough to overlap after padding are merged."""
        vcf = tmp_path / "truth.vcf"
        bed = tmp_path / "regions.bed"
        # Positions 10000100 and 10000120 with padding=50 overlap:
        #   Region 1: (10000049, 10000150)
        #   Region 2: (10000069, 10000170)
        _write_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\tSYNTHETIC;SVTYPE=SNP\tGT\t0/1\n",
            "chr20\t10000120\t.\tC\tT\t50\tPASS\tSYNTHETIC;SVTYPE=SNP\tGT\t1/1\n",
        ])

        result = generate_synthetic_regions_bed(str(vcf), str(bed), padding=50)

        assert result is True
        regions = _read_bed(bed)
        assert len(regions) == 1
        chrom, start, end = regions[0]
        assert chrom == "chr20"
        assert start == 10000049
        assert end == 10000170

    def test_custom_padding(self, tmp_path):
        """Non-default padding is applied correctly."""
        vcf = tmp_path / "truth.vcf"
        bed = tmp_path / "regions.bed"
        _write_vcf(vcf, [
            "chr20\t10000100\t.\tACGT\tA\t50\tPASS\tSYNTHETIC;SVTYPE=DEL\tGT\t0/1\n",
        ])

        result = generate_synthetic_regions_bed(str(vcf), str(bed), padding=100)

        assert result is True
        regions = _read_bed(bed)
        assert len(regions) == 1
        chrom, start, end = regions[0]
        assert chrom == "chr20"
        # REF=ACGT (len 4), padding=100
        # start = max(0, 10000100 - 1 - 100) = 9999999
        # end   = 10000100 - 1 + 4 + 100     = 10000203
        assert start == 9999999
        assert end == 10000203


# ---------------------------------------------------------------------------
# TestGenerateChallengeRegionBed
# ---------------------------------------------------------------------------

class TestGenerateChallengeRegionBed:
    """Tests for generate_challenge_region_bed."""

    def test_valid_region(self, tmp_path):
        bed = tmp_path / "challenge.bed"

        result = generate_challenge_region_bed("chr20:20822504-25822504", str(bed))

        assert result is True
        assert _read_bed(bed) == [("chr20", 20822503, 25822504)]

    def test_invalid_region(self, tmp_path):
        bed = tmp_path / "challenge.bed"

        result = generate_challenge_region_bed("chr20", str(bed))

        assert result is False
        assert not bed.exists()


# ---------------------------------------------------------------------------
# TestSubsetBed
# ---------------------------------------------------------------------------

class TestSubsetBed:
    """Tests for subset_bed."""

    def _write_bed(self, path: Path, entries: list[tuple[str, int, int]]):
        with path.open("w") as f:
            for chrom, start, end in entries:
                f.write(f"{chrom}\t{start}\t{end}\n")

    def test_overlapping_entries_kept(self, tmp_path):
        """Only BED entries overlapping the target region are retained."""
        source = tmp_path / "source.bed"
        target = tmp_path / "target.bed"
        self._write_bed(source, [
            ("chr20", 10000000, 10001000),  # overlaps
            ("chr20", 10005000, 10006000),  # overlaps
            ("chr20", 20000000, 20001000),  # outside
            ("chr21", 10000000, 10001000),  # wrong chrom
        ])

        result = subset_bed(source, target, "chr20:10000000-10010000")

        assert result is True
        regions = _read_bed(target)
        assert len(regions) == 2
        assert regions[0] == ("chr20", 10000000, 10001000)
        assert regions[1] == ("chr20", 10005000, 10006000)

    def test_entry_fully_outside_excluded(self, tmp_path):
        """An entry completely outside the region is excluded."""
        source = tmp_path / "source.bed"
        target = tmp_path / "target.bed"
        self._write_bed(source, [
            ("chr20", 50000000, 50001000),
        ])

        result = subset_bed(source, target, "chr20:10000000-10010000")

        assert result is True
        regions = _read_bed(target)
        assert len(regions) == 0

    def test_partial_overlap_included(self, tmp_path):
        """An entry that only partially overlaps the region is included."""
        source = tmp_path / "source.bed"
        target = tmp_path / "target.bed"
        # Entry starts before region but extends into it
        self._write_bed(source, [
            ("chr20", 9999000, 10000500),
        ])

        result = subset_bed(source, target, "chr20:10000000-10010000")

        assert result is True
        regions = _read_bed(target)
        assert len(regions) == 1
        assert regions[0] == ("chr20", 9999000, 10000500)


# ---------------------------------------------------------------------------
# TestParseHappyVcfAssessedMetrics
# ---------------------------------------------------------------------------

class TestParseHappyVcfAssessedMetrics:
    """Tests for parse_happy_vcf_assessed_metrics."""

    def test_titv_and_hethom_ratios(self, tmp_path):
        """Correct Ti/Tv and Het/Hom ratios from a minimal hap.py VCF."""
        vcf = tmp_path / "happy_output.vcf.gz"
        _write_happy_vcf(vcf, [
            # Row 1: query TP SNP ti het, truth TP SNP ti het
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n",
            # Row 2: query FP SNP tv homalt, truth TP SNP tv homalt
            "chr20\t10000200\t.\tC\tT\t50\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:tv:homalt\tFP:SNP:tv:homalt\n",
        ])

        result = parse_happy_vcf_assessed_metrics(str(vcf))

        assert result is not None
        # Query: 2 assessed SNPs (1 TP + 1 FP), 1 ti + 1 tv -> titv = 1.0
        assert result["query_total_snp"] == 2
        assert result["titv_query_snp"] == pytest.approx(1.0)
        # Truth: 2 assessed SNPs (both TP), 1 ti + 1 tv -> titv = 1.0
        assert result["titv_truth_snp"] == pytest.approx(1.0)
        # Query het/hom: 1 het / 1 homalt = 1.0
        assert result["hethom_query_snp"] == pytest.approx(1.0)
        # Truth het/hom: 1 het / 1 homalt = 1.0
        assert result["hethom_truth_snp"] == pytest.approx(1.0)
        assert result["query_total_indel"] == 0

    def test_missing_file_returns_none(self, tmp_path):
        """Non-existent file returns None."""
        result = parse_happy_vcf_assessed_metrics(str(tmp_path / "nonexistent.vcf.gz"))
        assert result is None


# ---------------------------------------------------------------------------
# TestParseRegionOvercallMetrics
# ---------------------------------------------------------------------------

class TestParseRegionOvercallMetrics:
    """Tests for parse_region_overcall_metrics."""

    def test_counts_full_region_query_fps_without_penalty_below_gate(self, tmp_path):
        vcf = tmp_path / "happy_output.vcf.gz"
        _write_happy_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tBD:BVT\tTP:SNP\tFP:SNP\n",
            "chr20\t10000200\t.\tA\tAT\t50\tPASS\t.\tBD:BVT\tTP:INDEL\tFP:INDEL\n",
            "chr20\t10000300\t.\tC\tT\t50\tPASS\t.\tBD:BVT\tTP:SNP\tTP:SNP\n",
            "chr20\t10000400\t.\tG\tA\t50\tPASS\t.\tBD:BVT\tTP:SNP\tUNK:SNP\n",
        ])

        result = parse_region_overcall_metrics(
            str(vcf),
            synthetic_truth_total=10,
            synthetic_snp_truth_total=8,
        )

        assert result is not None
        assert result["region_fp_snp"] == 1
        assert result["region_fp_indel"] == 1
        assert result["region_fp_total"] == 2
        assert result["fp_per_target"] == pytest.approx(0.2)
        assert result["snp_fp_per_target"] == pytest.approx(0.125)
        assert result["overcall_penalty"] == 0.0

    def test_penalty_requires_total_and_snp_gates(self, tmp_path):
        vcf = tmp_path / "happy_output.vcf.gz"
        _write_happy_vcf(vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tBD:BVT\tTP:SNP\tFP:SNP\n",
        ] * 12)

        result = parse_region_overcall_metrics(
            str(vcf),
            synthetic_truth_total=1,
            synthetic_snp_truth_total=1,
        )

        assert result is not None
        assert result["region_fp_total"] == 12
        assert result["fp_per_target"] == pytest.approx(12.0)
        assert result["snp_fp_per_target"] == pytest.approx(12.0)
        assert result["overcall_penalty"] == pytest.approx(8.0)

    def test_no_penalty_when_snp_gate_not_met(self, tmp_path):
        vcf = tmp_path / "happy_output.vcf.gz"
        _write_happy_vcf(vcf, [
            "chr20\t10000100\t.\tA\tAT\t50\tPASS\t.\tBD:BVT\tTP:INDEL\tFP:INDEL\n",
        ] * 12)

        result = parse_region_overcall_metrics(
            str(vcf),
            synthetic_truth_total=1,
            synthetic_snp_truth_total=1,
        )

        assert result is not None
        assert result["fp_per_target"] == pytest.approx(12.0)
        assert result["snp_fp_per_target"] == 0.0
        assert result["overcall_penalty"] == 0.0


# ---------------------------------------------------------------------------
# TestHappyScorerCsvParsing
# ---------------------------------------------------------------------------

class TestHappyScorerCsvParsing:
    """Tests for HappyScorer.score_vcf CSV parsing via mocked subprocess."""

    def _setup_scoring_env(self, tmp_path):
        """Create minimal truth VCF, query VCF, reference, SDF dir, and BED."""
        truth_vcf = tmp_path / "truth.vcf.gz"
        query_vcf = tmp_path / "query.vcf.gz"
        ref_fasta = tmp_path / "ref.fa"
        sdf_dir = tmp_path / "chr20.sdf"
        sdf_dir.mkdir()
        (sdf_dir / "dummy").write_text("sdf")

        _write_vcf(truth_vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\tSYNTHETIC;SVTYPE=SNP\tGT\t0/1\n",
        ], gz=True)
        _write_vcf(query_vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n",
        ], gz=True)
        ref_fasta.write_text(">chr20\nACGT\n")

        return truth_vcf, query_vcf, ref_fasta, sdf_dir

    def test_csv_parsing_succeeds(self, tmp_path, happy_summary_csv_path):
        """Mocked subprocess produces summary CSV; scorer parses it correctly."""
        truth_vcf, query_vcf, ref_fasta, sdf_dir = self._setup_scoring_env(tmp_path)

        # The scorer builds output_prefix = output_dir / f"happy_{query_stem}"
        # and expects f"{output_prefix}.summary.csv" to exist after subprocess runs.
        expected_csv = tmp_path / "happy_query.vcf.summary.csv"

        def fake_subprocess_run(cmd, **kwargs):
            """Simulate hap.py: copy fixture CSV to expected location."""
            shutil.copy(happy_summary_csv_path, expected_csv)
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result

        scorer = HappyScorer()

        with patch("utils.scoring.subprocess.run", side_effect=fake_subprocess_run), \
             patch("utils.scoring.generate_synthetic_regions_bed", return_value=False), \
             patch("utils.scoring.slice_truth_vcf", return_value=False):
            result = scorer.score_vcf(
                truth_vcf=str(truth_vcf),
                query_vcf=str(query_vcf),
                reference_fasta=str(ref_fasta),
                reference_sdf=str(sdf_dir),
            )

        # PASS rows from the fixture CSV:
        #   SNP:  F1=0.95, Precision=0.95, Recall=0.95
        #   INDEL: F1=0.9, Precision=0.9, Recall=0.9
        assert result["f1_snp"] == pytest.approx(0.95)
        assert result["f1_indel"] == pytest.approx(0.9)
        assert result["precision_snp"] == pytest.approx(0.95)
        assert result["recall_snp"] == pytest.approx(0.95)
        assert result["precision_indel"] == pytest.approx(0.9)
        assert result["recall_indel"] == pytest.approx(0.9)
        assert result["weighted_f1"] == pytest.approx(0.7 * 0.95 + 0.3 * 0.9)

    def test_no_csv_returns_zero_scores(self, tmp_path):
        """When subprocess fails and no CSV is created, zero scores are returned."""
        truth_vcf, query_vcf, ref_fasta, sdf_dir = self._setup_scoring_env(tmp_path)

        def fake_subprocess_run(cmd, **kwargs):
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "Docker error"
            return mock_result

        scorer = HappyScorer()

        with patch("utils.scoring.subprocess.run", side_effect=fake_subprocess_run), \
             patch("utils.scoring.generate_synthetic_regions_bed", return_value=False), \
             patch("utils.scoring.slice_truth_vcf", return_value=False):
            result = scorer.score_vcf(
                truth_vcf=str(truth_vcf),
                query_vcf=str(query_vcf),
                reference_fasta=str(ref_fasta),
                reference_sdf=str(sdf_dir),
            )

        expected_zero = scorer._get_zero_scores()
        assert result == expected_zero
        assert result["f1_snp"] == 0.0
        assert result["f1_indel"] == 0.0
        assert result["weighted_f1"] == 0.0
