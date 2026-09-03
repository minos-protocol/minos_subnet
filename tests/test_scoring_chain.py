"""The scoring chain end to end: hap.py output -> metrics -> score.

Every link has its own tests, but those cover each link in isolation: a change
to what the parser emits, or to what the scorer expects of it, can satisfy both
suites while the two no longer agree at the seam. These drive the real parser
and the real scorer over hap.py VCFs in the format hap.py emits; only hap.py
itself is stubbed, and its output is the input here.

They assert ordering and bands rather than exact numbers, so a deliberate
reweighting does not read as a failure while the properties below still hold.
"""
import gzip
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from utils.scoring import (  # noqa: E402
    AdvancedScorer,
    parse_happy_vcf_assessed_metrics,
)

HEADER = (
    "##fileformat=VCFv4.2\n"
    '##FORMAT=<ID=BD,Number=1,Type=String,Description="Decision">\n'
    '##FORMAT=<ID=BVT,Number=1,Type=String,Description="Variant type">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
)


def _row(pos, ref, alt, truth_bd, query_bd, vt="SNP", tgt="0/1", qgt="0/1"):
    truth = f"{tgt}:{truth_bd}:{vt}" if truth_bd else ".:.:."
    query = f"{qgt}:{query_bd}:{vt}" if query_bd else ".:.:."
    return f"chr20\t{pos}\t.\t{ref}\t{alt}\t50\tPASS\t.\tGT:BD:BVT\t{truth}\t{query}\n"


def _happy_vcf(tmp_path, rows):
    path = tmp_path / "happy.vcf.gz"
    with gzip.open(path, "wt") as fh:
        fh.write(HEADER)
        fh.writelines(rows)
    return path


def _score(tmp_path, rows):
    """Run the chain a validator runs once hap.py returns."""
    metrics = parse_happy_vcf_assessed_metrics(str(_happy_vcf(tmp_path, rows)))
    assert metrics is not None, "the parser rejected a well-formed hap.py VCF"
    tp = sum(1 for r in rows if ":TP:" in r.split("\t")[10])
    fp = sum(1 for r in rows if ":FP:" in r.split("\t")[10])
    fn = sum(1 for r in rows if ":FN:" in r.split("\t")[9])
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    metrics.update(
        f1_snp=f1, f1_indel=f1, recall_snp=recall, recall_indel=recall,
        truth_total_snp=tp + fn, truth_total_indel=0,
        region_fp_snp=fp, region_fp_indel=0,
    )
    return AdvancedScorer.compute_advanced_score(metrics), metrics


def _perfect(n=20):
    return [_row(1000 + i, "A", "G", "TP", "TP") for i in range(n)]


class TestTheChainProducesSaneScores:
    def test_a_perfect_callset_scores_near_the_top(self, tmp_path):
        score, _ = _score(tmp_path, _perfect())
        assert score > 95, f"a perfect callset scored {score}"

    def test_missing_half_the_truth_costs_a_lot(self, tmp_path):
        rows = _perfect(10) + [_row(2000 + i, "A", "G", "FN", "") for i in range(10)]
        score, _ = _score(tmp_path, rows)
        assert 30 < score < 80, f"half-missed scored {score}"

    def test_more_false_positives_always_scores_worse(self, tmp_path):
        """Monotonicity: adding FPs must never help. A reweighting that breaks
        this makes flooding the callset a winning strategy."""
        scores = []
        for fp_count in (0, 10, 50, 200):
            rows = _perfect() + [
                _row(3000 + i, "A", "G", "", "FP") for i in range(fp_count)
            ]
            scores.append(_score(tmp_path, rows)[0])
        assert scores == sorted(scores, reverse=True), f"non-monotonic in FPs: {scores}"

    def test_more_recall_always_scores_better(self, tmp_path):
        """The other direction: finding more true variants must never hurt."""
        scores = []
        for tp in (5, 10, 15, 20):
            rows = _perfect(tp) + [
                _row(2000 + i, "A", "G", "FN", "") for i in range(20 - tp)
            ]
            scores.append(_score(tmp_path, rows)[0])
        assert scores == sorted(scores), f"non-monotonic in recall: {scores}"


class TestWhatTheParserFeedsTheScorer:
    def test_unassessed_calls_stay_out_of_the_query_total(self, tmp_path):
        """hap.py marks calls outside the confident BED as UNK. Counting them
        would inflate query_total and make a correct caller look like it
        overcalled."""
        rows = _perfect(5) + [_row(4000 + i, "A", "G", "", "UNK") for i in range(50)]
        _, metrics = _score(tmp_path, rows)
        assert metrics["query_total_snp"] == 5

    def test_a_callset_that_is_entirely_unassessed_yields_no_score(self, tmp_path):
        """Nothing was assessed, so there is nothing to score — and it must not
        fall through to a default."""
        rows = [_row(4000 + i, "A", "G", "", "UNK") for i in range(10)]
        score, _ = _score(tmp_path, rows)
        assert score == 0.0

    def test_an_empty_callset_against_real_truth_is_visible_in_the_metrics(self, tmp_path):
        """The scorer alone gives an empty callset a non-trivial score, so the
        validator suppresses it from the METRICS rather than from the value.
        This pins the shape that guard depends on."""
        rows = [_row(1000 + i, "A", "G", "FN", "") for i in range(20)]
        score, metrics = _score(tmp_path, rows)
        query_total = (metrics.get("query_total_snp") or 0) + (metrics.get("query_total_indel") or 0)
        truth_total = (metrics.get("truth_total_snp") or 0) + (metrics.get("truth_total_indel") or 0)
        assert query_total == 0 and truth_total > 0, (
            "the zero-input fingerprint the validator checks no longer holds"
        )
        assert score > 0, "if this ever returns 0 the guard is load-bearing for a different reason"


class TestFloodedCallsets:
    def test_a_flood_scores_far_below_a_clean_callset(self, tmp_path):
        clean, _ = _score(tmp_path, _perfect())
        flooded, _ = _score(
            tmp_path, _perfect() + [_row(3000 + i, "A", "G", "", "FP") for i in range(400)]
        )
        assert flooded < clean - 40, f"flood={flooded} clean={clean}"

    def test_a_flood_still_scores_above_zero(self, tmp_path):
        """It found every true variant. Scoring it at zero would say the same as
        submitting nothing, which is not the same thing."""
        flooded, _ = _score(
            tmp_path, _perfect() + [_row(3000 + i, "A", "G", "", "FP") for i in range(400)]
        )
        assert flooded > 0
