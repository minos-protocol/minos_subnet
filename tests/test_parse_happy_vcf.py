"""Tests for parse_happy_vcf.

Scoring v2 promotes this function from audit data to the consensus path: its
output feeds difficulty_class_counts, which feeds 70% of the v2 score. That
makes synthetic detection, per-ALT handling, and which sample zygosity is read
from all load-bearing, so each is pinned here.

The fixtures are real hap.py-shaped VCFs written to disk and read back through
pysam, because the behaviour under test is how pysam presents records — which a
test built on hand-made dicts would not exercise.
"""

import pytest

pysam = pytest.importorskip("pysam")

from utils.scoring import parse_happy_vcf, difficulty_class_counts  # noqa: E402


HEADER = """##fileformat=VCFv4.2
##contig=<ID=chr20,length=64444167>
##INFO=<ID=SYNTHETIC,Number=0,Type=Flag,Description="Spiked-in variant">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=BD,Number=1,Type=String,Description="Benchmark decision">
##FORMAT=<ID=BVT,Number=1,Type=String,Description="Benchmark variant type">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY
"""


def _write_vcf(tmp_path, rows, name="happy.vcf"):
    path = tmp_path / name
    path.write_text(HEADER + "".join(rows))
    return str(path)


def _row(pos, ref, alt, truth, query, info=".", qual="50"):
    return f"chr20\t{pos}\t.\t{ref}\t{alt}\t{qual}\tPASS\t{info}\tGT:BD:BVT:DP\t{truth}\t{query}\n"


class TestBenchmarkDecisions:
    def test_counts_each_decision_once(self, tmp_path):
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G", "1/1:TP:SNP:30", "1/1:TP:SNP:30"),
            _row(200, "A", "G", "0/1:FN:SNP:0", "./.:.:.:0"),
            _row(300, "A", "G", "./.:.:.:0", "0/1:FP:SNP:25"),
        ])
        recs = parse_happy_vcf(vcf)
        assert [r["classification"] for r in recs] == ["TP", "FN", "FP"]

    def test_a_multi_allelic_site_is_one_decision_not_one_per_allele(self, tmp_path):
        """BD and BVT describe the whole record. Expanding over record.alts
        replayed one TP/FP/FN once per allele and inflated every downstream
        class count at exactly the messy sites."""
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G,T,C", "1/1:TP:SNP:30", "1/1:TP:SNP:30"),
        ])
        recs = parse_happy_vcf(vcf)
        assert len(recs) == 1
        assert recs[0]["alt_alleles"] == "G,T,C"

        counts = difficulty_class_counts(recs)
        assert sum(c["tp"] for c in counts.values()) == 1

    def test_not_assessed_records_are_dropped(self, tmp_path):
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G", "0/1:N:SNP:0", "0/1:N:SNP:0"),
        ])
        assert parse_happy_vcf(vcf) == []


class TestZygosity:
    def test_a_missed_hom_snp_keeps_its_truth_zygosity(self, tmp_path):
        """An FN has no query genotype, so reading zygosity from the call filed
        every missed hom SNP under the 9x-heavier het class."""
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G", "1/1:FN:SNP:0", "./.:.:.:0"),
        ])
        rec = parse_happy_vcf(vcf)[0]
        assert rec["truth_genotype"] == "1/1"
        assert difficulty_class_counts([rec])["snp_hom"]["fn"] == 1

    def test_the_call_is_still_recorded_separately(self, tmp_path):
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G", "0/1:TP:SNP:30", "1/1:TP:SNP:30"),
        ])
        rec = parse_happy_vcf(vcf)[0]
        assert rec["truth_genotype"] == "0/1"
        assert rec["called_genotype"] == "1/1"


class TestSyntheticFlag:
    def test_synthetic_variants_are_detected(self, tmp_path):
        """This never fired: the flag was matched against str() of a pysam
        VariantRecordInfo, which renders as '<... object at 0x...>'."""
        truth = _write_vcf(tmp_path, [
            _row(100, "A", "G", "1/1:.:.:0", "1/1:.:.:0", info="SYNTHETIC"),
            _row(200, "A", "G", "0/1:.:.:0", "0/1:.:.:0", info="."),
        ], name="truth.vcf")
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "G", "1/1:TP:SNP:30", "1/1:TP:SNP:30"),
            _row(200, "A", "G", "0/1:TP:SNP:30", "0/1:TP:SNP:30"),
        ])
        recs = parse_happy_vcf(vcf, truth_vcf_path=truth)
        by_pos = {r["pos"]: r["is_synthetic"] for r in recs}
        assert by_pos[100] is True
        assert by_pos[200] is False

    def test_without_a_truth_vcf_the_flag_is_unknown_not_false(self, tmp_path):
        vcf = _write_vcf(tmp_path, [_row(100, "A", "G", "1/1:TP:SNP:30", "1/1:TP:SNP:30")])
        assert parse_happy_vcf(vcf)[0]["is_synthetic"] is None


class TestVariantType:
    def test_an_unrecognised_bvt_is_inferred_not_silently_called_snp(self, tmp_path):
        """SNP is the lowest-weighted class in v2, so coercing an unknown type
        to it let a parsing surprise quietly discount itself."""
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "ATTT", "0/1:TP:NOCALL:30", "0/1:TP:NOCALL:30"),
        ])
        rec = parse_happy_vcf(vcf)[0]
        assert rec["variant_type"] == "INDEL"


class TestRobustness:
    def test_a_missing_file_returns_empty_rather_than_raising(self, tmp_path):
        assert parse_happy_vcf(str(tmp_path / "nope.vcf.gz")) == []
