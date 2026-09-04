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
        """BD and BVT describe the whole record, so expanding over record.alts
        would replay one TP/FP/FN per allele and inflate every downstream
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
        """An FN has no query genotype, so reading zygosity from the call would
        file every missed hom SNP under the heavier het class."""
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
        """The flag has to be tested against the INFO keys: str() of a pysam
        VariantRecordInfo renders as '<... object at 0x...>' and matches
        nothing."""
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
    def test_an_unrecognised_bvt_is_inferred_not_defaulted_to_snp(self, tmp_path):
        """SNP is the lowest-weighted class in v2, so coercing an unknown type
        to it would let a parsing surprise discount itself."""
        vcf = _write_vcf(tmp_path, [
            _row(100, "A", "ATTT", "0/1:TP:NOCALL:30", "0/1:TP:NOCALL:30"),
        ])
        rec = parse_happy_vcf(vcf)[0]
        assert rec["variant_type"] == "INDEL"


class TestRobustness:
    def test_a_missing_file_reports_failure_rather_than_raising(self, tmp_path):
        assert parse_happy_vcf(str(tmp_path / "nope.vcf.gz")) is None, (
            "a file that could not be opened must report failure, not an empty "
            "callset -- they mean different things to the v2 scorer"
        )


class TestTheParseIsAtomic:
    """A partial parse is indistinguishable from a complete one, and the v2 core
    is built from these records. Dropping the FNs and FPs after a mid-file
    failure while keeping the TPs before it would inflate the score in the
    miner's favour, and a partial list is indistinguishable from a complete
    one."""

    def _corrupt_after_good_records(self, tmp_path):
        """Valid early TPs, a record pysam cannot read, then the FNs and FPs
        that would have counted against the miner."""
        rows = [
            _row(100, "A", "G", "1/1:TP:SNP:30", "1/1:TP:SNP:30"),
            _row(150, "A", "G", "1/1:TP:SNP:30", "1/1:TP:SNP:30"),
            "chr20\tNOT_A_POSITION\t.\tA\tG\t50\tPASS\t.\tGT:BD:BVT:DP\t1/1:TP:SNP:30\t1/1:TP:SNP:30\n",
            _row(300, "A", "G", "0/1:FN:SNP:0", "./.:.:.:0"),
            _row(400, "A", "G", "./.:.:.:0", "0/1:FP:SNP:25"),
        ]
        return _write_vcf(tmp_path, rows, name="corrupt.vcf")

    def test_a_mid_file_failure_discards_the_whole_parse(self, tmp_path):
        """pysam aborts on the bad position after reading two TPs. Without
        atomic parsing those two would be returned while the FN and FP after
        them were lost -- a better-looking callset than the miner produced."""
        recs = parse_happy_vcf(self._corrupt_after_good_records(tmp_path))
        assert recs is None, (
            f"returned a partial callset ({recs if recs is None else len(recs)} "
            "records): the TPs before the bad record survived while the FN and "
            "FP after it were dropped"
        )

    def test_a_partial_parse_cannot_reach_difficulty_counts(self, tmp_path):
        """The end-to-end consequence: the v2 core is built from these records,
        so a failed parse must yield no score rather than a flattering one."""
        recs = parse_happy_vcf(self._corrupt_after_good_records(tmp_path))
        assert recs is None
        # What the caller must not do: treat a truncated list as usable.
        assert not recs, "a falsy result is what stops v2 scoring downstream"

    def test_a_valid_empty_file_is_not_a_failure(self, tmp_path):
        """Empty and unparseable must stay distinguishable."""
        assert parse_happy_vcf(_write_vcf(tmp_path, [], name="empty.vcf")) == []
