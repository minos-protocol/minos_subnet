"""Tests for the pure math functions in utils.scoring (AdvancedScorer, HappyScorer)."""

import gzip
import math

import pytest

from utils.scoring import (
    AdvancedScorer,
    HappyScorer,
    compute_synthetic_only_metrics,
    parse_happy_vcf_assessed_metrics,
)


# ---------------------------------------------------------------------------
# TestEmphasis
# ---------------------------------------------------------------------------

class TestEmphasis:
    """Tests for AdvancedScorer.emphasis()."""

    def test_zero_metric(self):
        """emphasis(0.0, 3.0) should be exactly 0.0."""
        assert AdvancedScorer.emphasis(0.0, 3.0) == 0.0

    def test_near_one_metric(self):
        """emphasis(0.999999, 3.0) should be very close to 1.0."""
        result = AdvancedScorer.emphasis(0.999999, 3.0)
        assert result == pytest.approx(1.0, abs=1e-6)

    def test_half_gamma_three(self):
        """emphasis(0.5, 3.0) = 1 - (0.5)^3 = 0.875."""
        expected = 1.0 - (0.5) ** 3
        assert AdvancedScorer.emphasis(0.5, 3.0) == pytest.approx(expected)

    def test_half_gamma_half(self):
        """emphasis(0.5, 0.5) = 1 - (0.5)^0.5 ~ 0.2929."""
        expected = 1.0 - (0.5) ** 0.5
        assert AdvancedScorer.emphasis(0.5, 0.5) == pytest.approx(expected, abs=1e-4)

    def test_high_metric_gamma_three(self):
        """emphasis(0.9, 3.0) = 1 - (0.1)^3 = 0.999."""
        expected = 1.0 - (0.1) ** 3
        assert AdvancedScorer.emphasis(0.9, 3.0) == pytest.approx(expected)

    def test_negative_metric_clamped(self):
        """Negative metric should be clamped to 0 before computation."""
        assert AdvancedScorer.emphasis(-0.5, 3.0) == AdvancedScorer.emphasis(0.0, 3.0)
        assert AdvancedScorer.emphasis(-0.5, 3.0) == 0.0

    def test_metric_above_one_clamped(self):
        """Metric > 1 should be clamped to 0.999999."""
        assert AdvancedScorer.emphasis(1.5, 3.0) == pytest.approx(
            AdvancedScorer.emphasis(0.999999, 3.0)
        )

    def test_zero_metric_gamma_half(self):
        """emphasis(0.0, 0.5) should be exactly 0.0."""
        assert AdvancedScorer.emphasis(0.0, 0.5) == 0.0


# ---------------------------------------------------------------------------
# TestRatioPenalty
# ---------------------------------------------------------------------------

class TestRatioPenalty:
    """Tests for AdvancedScorer.ratio_penalty()."""

    def test_zero_delta_no_penalty(self):
        """Zero delta should produce penalty of 1.0 (no penalty)."""
        assert AdvancedScorer.ratio_penalty(0.0, 0.1) == 1.0

    def test_moderate_delta(self):
        """ratio_penalty(0.05, 0.1) = exp(-0.5) ~ 0.6065."""
        expected = math.exp(-0.5)
        assert AdvancedScorer.ratio_penalty(0.05, 0.1) == pytest.approx(expected)

    def test_large_delta_near_zero(self):
        """ratio_penalty(1.0, 0.1) = exp(-10) ~ 4.54e-5, near zero."""
        expected = math.exp(-10.0)
        assert AdvancedScorer.ratio_penalty(1.0, 0.1) == pytest.approx(expected)

    def test_symmetric_for_negative_delta(self):
        """Negative delta should give the same result as positive delta (abs)."""
        assert AdvancedScorer.ratio_penalty(-0.05, 0.1) == pytest.approx(
            AdvancedScorer.ratio_penalty(0.05, 0.1)
        )


# ---------------------------------------------------------------------------
# TestComputeAdvancedScore
# ---------------------------------------------------------------------------

class TestComputeAdvancedScore:
    """Tests for AdvancedScorer.compute_advanced_score()."""

    def test_perfect_metrics_near_100(self, perfect_happy_metrics):
        """Perfect metrics should produce a score near 100."""
        score = AdvancedScorer.compute_advanced_score(perfect_happy_metrics)
        assert score == pytest.approx(100.0, abs=2.0)

    def test_zero_metrics_low_score(self, zero_happy_metrics):
        """All-zero F1/recall metrics should produce a very low score.

        Note: score is not zero because coverage (frac_na=0 -> coverage=1.0)
        and quality defaults still contribute (~25 points from FP + quality
        components when there are zero calls).
        """
        score = AdvancedScorer.compute_advanced_score(zero_happy_metrics)
        assert score < 30.0

    def test_realistic_metrics_reasonable_range(self, sample_happy_metrics):
        """Realistic metrics should give a score between 0 and 100."""
        score = AdvancedScorer.compute_advanced_score(sample_happy_metrics)
        assert 0.0 <= score <= 100.0
        # Should be a good but not perfect score
        assert score > 50.0

    def test_overcall_penalty_subtracted(self, sample_happy_metrics):
        """Overcall guardrail penalty should be subtracted from final score."""
        base_score = AdvancedScorer.compute_advanced_score(sample_happy_metrics)
        metrics = dict(sample_happy_metrics)
        metrics["overcall_penalty"] = 12.5

        penalized_score = AdvancedScorer.compute_advanced_score(metrics)

        assert penalized_score == pytest.approx(max(0.0, base_score - 12.5))

    def test_snp_only_truth_weighting(self):
        """When truth_total_indel=0, weighting should only use SNP F1."""
        metrics = {
            'f1_snp': 0.95,
            'f1_indel': 0.50,
            'recall_snp': 0.95,
            'recall_indel': 0.0,
            'truth_total_snp': 100,
            'truth_total_indel': 0,
            'query_total_snp': 100,
            'query_total_indel': 0,
            'fp_snp': 0,
            'fp_indel': 0,
            'frac_na_snp': 0.0,
            'frac_na_indel': 0.0,
        }
        score = AdvancedScorer.compute_advanced_score(metrics)
        # weighted_f1 = (0.95*100 + 0.50*0) / 100 = 0.95
        # The indel F1 of 0.50 should NOT drag the score down
        assert score > 60.0

    def test_no_truth_totals_fail_closed(self):
        """When both truth totals are 0, AdvancedScorer should fail closed."""
        metrics = {
            'f1_snp': 0.90,
            'f1_indel': 0.80,
            'recall_snp': 0.90,
            'recall_indel': 0.80,
            'truth_total_snp': 0,
            'truth_total_indel': 0,
            'query_total_snp': 10,
            'query_total_indel': 5,
            'fp_snp': 0,
            'fp_indel': 0,
            'frac_na_snp': 0.0,
            'frac_na_indel': 0.0,
        }
        score = AdvancedScorer.compute_advanced_score(metrics)
        assert score == 0.0

    def test_high_f1_high_fp_penalized(self):
        """High F1 but many false positives should penalize the FP component."""
        metrics_low_fp = {
            'f1_snp': 0.95, 'f1_indel': 0.95,
            'recall_snp': 0.95, 'recall_indel': 0.95,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 2, 'fp_indel': 1,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        metrics_high_fp = {
            **metrics_low_fp,
            'fp_snp': 30, 'fp_indel': 20,
            'query_total_snp': 150, 'query_total_indel': 80,
        }
        score_low_fp = AdvancedScorer.compute_advanced_score(metrics_low_fp)
        score_high_fp = AdvancedScorer.compute_advanced_score(metrics_high_fp)
        assert score_low_fp > score_high_fp

    def test_perfect_f1_mismatched_titv_quality_penalized(self):
        """Perfect F1 but wildly off Ti/Tv ratio should penalize quality component."""
        metrics_good_titv = {
            'f1_snp': 1.0, 'f1_indel': 1.0,
            'recall_snp': 1.0, 'recall_indel': 1.0,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 0, 'fp_indel': 0,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        metrics_bad_titv = {
            **metrics_good_titv,
            'titv_query_snp': 5.0,  # Far from truth 2.1
        }
        score_good = AdvancedScorer.compute_advanced_score(metrics_good_titv)
        score_bad = AdvancedScorer.compute_advanced_score(metrics_bad_titv)
        assert score_good > score_bad

    def test_score_bounded_zero_to_hundred(self, sample_happy_metrics):
        """Score should always be in [0, 100]."""
        score = AdvancedScorer.compute_advanced_score(sample_happy_metrics)
        assert 0.0 <= score <= 100.0

    def test_empty_metrics_no_error(self):
        """Empty metrics dict should not raise and should return some score."""
        score = AdvancedScorer.compute_advanced_score({})
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0

    def test_component_weights_sum_to_one(self):
        """When all components equal 1.0, score should be exactly 100.

        Build metrics that make each component evaluate to 1.0:
        - Core: F1=0.999999, emphasis(0.999999, 0.5) ~ 1.0
        - Completeness: recall=0.999999, frac_na=0 -> coverage=1.0
        - FP: fp=0, size_ratio=1.0 -> fp_pen=1, size_pen=1
        - Quality: matching ratios -> ratio_penalty(0,...)=1.0
        """
        metrics = {
            'f1_snp': 0.999999, 'f1_indel': 0.999999,
            'recall_snp': 0.999999, 'recall_indel': 0.999999,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 0, 'fp_indel': 0,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        score = AdvancedScorer.compute_advanced_score(metrics)
        # 100 * (0.60 + 0.15 + 0.15 + 0.10) = 100.0
        assert score == pytest.approx(100.0, abs=0.5)

    def test_score_monotonic_with_f1(self):
        """Score should increase as F1 increases (monotonicity)."""
        base = {
            'recall_snp': 0.90, 'recall_indel': 0.85,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 2, 'fp_indel': 1,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        scores = []
        for f1_val in [0.5, 0.7, 0.85, 0.95]:
            m = {**base, 'f1_snp': f1_val, 'f1_indel': f1_val}
            scores.append(AdvancedScorer.compute_advanced_score(m))
        # Each score should be strictly greater than the previous
        for i in range(1, len(scores)):
            assert scores[i] > scores[i - 1], (
                f"Score at F1={[0.5,0.7,0.85,0.95][i]} ({scores[i]:.4f}) should be "
                f"> score at F1={[0.5,0.7,0.85,0.95][i-1]} ({scores[i-1]:.4f})"
            )

    def test_high_frac_na_penalizes_completeness_unreachable(self):
        """UNREACHABLE PATH. No real hap.py run produces a nonzero frac_na
        here: both producers set METRIC.Frac_NA to 0.0, so this exercises
        arithmetic the live pipeline does not reach. Kept to pin the arithmetic
        for whoever wires frac_na through; see TestCoverageIsADeadConstant for
        what the live pipeline actually feeds in.
        """
        base = {
            'f1_snp': 0.90, 'f1_indel': 0.85,
            'recall_snp': 0.90, 'recall_indel': 0.85,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 2, 'fp_indel': 1,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        metrics_low_na = {**base, 'frac_na_snp': 0.0, 'frac_na_indel': 0.0}
        metrics_high_na = {**base, 'frac_na_snp': 0.5, 'frac_na_indel': 0.6}
        score_low_na = AdvancedScorer.compute_advanced_score(metrics_low_na)
        score_high_na = AdvancedScorer.compute_advanced_score(metrics_high_na)
        assert score_low_na > score_high_na

    def test_matching_ratios_quality_near_one(self):
        """When query ratios exactly match truth ratios, quality component ~ 1.0."""
        metrics = {
            'f1_snp': 0.90, 'f1_indel': 0.85,
            'recall_snp': 0.90, 'recall_indel': 0.85,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 2, 'fp_indel': 1,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        # Manually compute the quality component to verify it is 1.0
        # titv delta=0 -> penalty=1.0, hethom deltas=0 -> penalties=1.0
        titv_pen = AdvancedScorer.ratio_penalty(0.0, 0.1)
        hethom_snp_pen = AdvancedScorer.ratio_penalty(0.0, 0.15)
        hethom_indel_pen = AdvancedScorer.ratio_penalty(0.0, 0.15)
        expected_quality = ((titv_pen) + (hethom_snp_pen + hethom_indel_pen) / 2) / 2
        assert expected_quality == pytest.approx(1.0)

        # Also verify the score is not penalized relative to a version without
        # ratio data (which defaults quality to 1.0)
        metrics_no_ratios = {**metrics}
        del metrics_no_ratios['titv_truth_snp']
        del metrics_no_ratios['titv_query_snp']
        del metrics_no_ratios['hethom_truth_snp']
        del metrics_no_ratios['hethom_query_snp']
        del metrics_no_ratios['hethom_truth_indel']
        del metrics_no_ratios['hethom_query_indel']
        score_with = AdvancedScorer.compute_advanced_score(metrics)
        score_without = AdvancedScorer.compute_advanced_score(metrics_no_ratios)
        assert score_with == pytest.approx(score_without, abs=0.5)

    def test_size_ratio_far_from_one_penalized(self):
        """When total_calls is very different from total_truth, FP component suffers."""
        base = {
            'f1_snp': 0.90, 'f1_indel': 0.85,
            'recall_snp': 0.90, 'recall_indel': 0.85,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'fp_snp': 0, 'fp_indel': 0,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
        }
        metrics_good_size = {**base, 'query_total_snp': 100, 'query_total_indel': 50}
        metrics_bad_size = {**base, 'query_total_snp': 300, 'query_total_indel': 150}
        score_good = AdvancedScorer.compute_advanced_score(metrics_good_size)
        score_bad = AdvancedScorer.compute_advanced_score(metrics_bad_size)
        assert score_good > score_bad

    def test_indel_heavy_truth_weighting(self):
        """When truth has more INDELs than SNPs, INDEL F1 should dominate."""
        metrics = {
            'f1_snp': 0.60,
            'f1_indel': 0.95,
            'recall_snp': 0.60,
            'recall_indel': 0.95,
            'truth_total_snp': 20,
            'truth_total_indel': 200,
            'query_total_snp': 20,
            'query_total_indel': 200,
            'fp_snp': 0,
            'fp_indel': 0,
            'frac_na_snp': 0.0,
            'frac_na_indel': 0.0,
        }
        # weighted_f1 = (0.60*20 + 0.95*200) / 220 = (12 + 190) / 220 = 0.918...
        total_truth = 20 + 200
        expected_weighted_f1 = (0.60 * 20 + 0.95 * 200) / total_truth
        assert expected_weighted_f1 == pytest.approx(0.9182, abs=0.001)

        # If we swap and make SNPs heavy, the low SNP F1 should dominate
        metrics_snp_heavy = {
            **metrics,
            'truth_total_snp': 200,
            'truth_total_indel': 20,
            'query_total_snp': 200,
            'query_total_indel': 20,
        }
        score_indel_heavy = AdvancedScorer.compute_advanced_score(metrics)
        score_snp_heavy = AdvancedScorer.compute_advanced_score(metrics_snp_heavy)
        # INDEL-heavy: weighted_f1 ~ 0.918 vs SNP-heavy: weighted_f1 ~ 0.632
        assert score_indel_heavy > score_snp_heavy


# ---------------------------------------------------------------------------
# TestHappyScorerZeroScores
# ---------------------------------------------------------------------------

class TestHappyScorerZeroScores:
    """Tests for HappyScorer._get_zero_scores()."""

    def test_zero_scores_returns_failed_sentinel(self):
        """_get_zero_scores should fail closed and let the validator submit 0."""
        scorer = HappyScorer()
        result = scorer._get_zero_scores()
        assert result is None


class TestNonFiniteMetrics:
    """hap.py emits 'inf' for a ratio whose denominator is zero — no homozygous
    variants, or no transversions. Both are ordinary in a small region."""

    def _perfect(self, **over):
        m = dict(
            f1_snp=1.0, f1_indel=1.0, recall_snp=1.0, recall_indel=1.0,
            truth_total_snp=100, truth_total_indel=50,
            query_total_snp=100, query_total_indel=50,
            fp_snp=0, fp_indel=0,
            titv_truth_snp=2.0, titv_query_snp=2.0,
            hethom_truth_snp=1.5, hethom_query_snp=1.5,
            hethom_truth_indel=1.5, hethom_query_indel=1.5,
        )
        m.update(over)
        return m

    def test_an_infinite_ratio_does_not_collapse_a_perfect_score_to_zero(self):
        """inf passes the `> 0` gate, inf-inf gives nan, and max(0.0, nan)
        returns 0.0 because nan loses every comparison — so without this guard
        a complete callset evaluates to zero.

        The callset still loses the quality component — a query ti/tv of inf
        against a finite truth ti/tv means no transversions were called, which
        is degenerate and should cost something. What it must not do is take
        the other 90% of the score down with it.
        """
        clean = AdvancedScorer.compute_advanced_score(self._perfect())
        poisoned = AdvancedScorer.compute_advanced_score(
            self._perfect(titv_query_snp=float("inf"))
        )
        assert clean > 99.0
        assert poisoned > 90.0, f"non-finite ratio still collapsed the score: {poisoned}"
        # It costs at most the 10% quality weight, not the whole score.
        assert clean - poisoned <= 10.0

    def test_both_ratios_absent_is_not_penalised(self):
        """No truth ratio to compare against is not the miner's fault."""
        m = self._perfect()
        for k in ("titv_truth_snp", "titv_query_snp"):
            m.pop(k, None)
        assert AdvancedScorer.compute_advanced_score(m) > 99.0

    def test_a_non_finite_penalty_still_scores_zero(self):
        """v1 behaviour, frozen deliberately.

        nan loses every comparison, so max(0.0, nan) returns 0.0 and a perfect
        callset is emitted as a legitimate zero. Raising instead would be
        better, and v2 does return None — but changing it here would move live
        v1 scores without a version bump to signal it, which is exactly what
        the scoring_version gate exists to prevent.
        """
        score = AdvancedScorer.compute_advanced_score(
            self._perfect(overcall_penalty=float("nan"))
        )
        assert score == 0.0


class TestCompletenessWeighting:
    def _m(self, **over):
        m = dict(
            f1_snp=1.0, f1_indel=0.0, recall_snp=1.0, recall_indel=0.0,
            truth_total_snp=100, truth_total_indel=0,
            query_total_snp=100, query_total_indel=0,
            fp_snp=0, fp_indel=0,
            titv_truth_snp=2.0, titv_query_snp=2.0,
            hethom_truth_snp=1.5, hethom_query_snp=1.5,
        )
        m.update(over)
        return m

    def test_a_class_with_no_truth_still_scores_zero_recall(self):
        """v1 behaviour, frozen deliberately.

        The recall mean is flat, so a round with no targets in one class is
        scored the same way for every miner in it. Weighting it here would move
        every live v1 score; v2 is where the weighting is revised.
        """
        score = AdvancedScorer.compute_advanced_score(self._m())
        assert 98.0 < score < 99.5, f"SNP-only perfect round scored {score}"

    def test_missing_one_indel_still_costs_less_than_missing_every_snp(self):
        """Not via avg_recall, which is a flat mean either way, but via the
        core F1 component — that one has always been truth-count weighted."""
        miss_indel = AdvancedScorer.compute_advanced_score(
            self._m(truth_total_indel=1, recall_indel=0.0, f1_indel=0.0)
        )
        miss_snps = AdvancedScorer.compute_advanced_score(
            self._m(truth_total_indel=1, recall_indel=1.0, f1_indel=1.0,
                    recall_snp=0.0, f1_snp=0.0)
        )
        assert miss_indel > miss_snps


# ---------------------------------------------------------------------------
# TestCoverageIsADeadConstant
# ---------------------------------------------------------------------------

class TestCoverageIsADeadConstant:
    """The coverage half of the completeness component is fixed at 1.0.

    Both metric producers set frac_na to 0.0 rather than passing hap.py's
    METRIC.Frac_NA through, so 7.5 of the 100 points are a constant that does
    not separate one miner from another. That is deliberate — Frac_NA is
    computed over the whole query VCF and comes back ~0.99 against a small eval
    region. These tests pin the real behaviour so the constant is not read as a
    live signal, and so wiring frac_na through has to be a deliberate act that
    breaks them.
    """

    HAPPY_HEADER = (
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
    )

    def _write_happy_vcf(self, path, body_lines):
        with gzip.open(path, "wt") as f:
            f.write(self.HAPPY_HEADER + "".join(body_lines))

    def test_assessed_metrics_producer_discards_frac_na(self, tmp_path):
        """Nine of ten calls fall outside the confident BED (BD=UNK) — hap.py
        would report Frac_NA ~0.9 — and the producer still emits 0.0."""
        vcf = tmp_path / "happy_output.vcf.gz"
        rows = [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n"
        ]
        rows += [
            f"chr20\t{20000000 + i}\t.\tA\tG\t50\tPASS\t.\tBD:BVT:BI:BLT\t.:.:.:.\tUNK:SNP:ti:het\n"
            for i in range(9)
        ]
        self._write_happy_vcf(vcf, rows)

        result = parse_happy_vcf_assessed_metrics(str(vcf))

        assert result is not None
        assert result["query_total_snp"] == 1, "UNK calls must stay out of the assessed total"
        assert result["frac_na_snp"] == 0.0
        assert result["frac_na_indel"] == 0.0

    def test_synthetic_only_producer_discards_frac_na(self, tmp_path):
        """The synthetic-target producer, which overwrites the one above on
        every scored round, also emits a hardcoded 0.0."""
        happy_vcf = tmp_path / "happy_output.vcf.gz"
        self._write_happy_vcf(happy_vcf, [
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n",
            "chr20\t20000000\t.\tA\tG\t50\tPASS\t.\tBD:BVT:BI:BLT\t.:.:.:.\tUNK:SNP:ti:het\n",
        ])
        mutations = tmp_path / "mutations.vcf"
        mutations.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr20\t10000100\t.\tA\tG\t50\tPASS\t.\n"
        )

        result = compute_synthetic_only_metrics(str(happy_vcf), str(mutations))

        assert result is not None
        assert result["frac_na_snp"] == 0.0
        assert result["frac_na_indel"] == 0.0

    def test_coverage_contributes_a_fixed_7_5_points(self):
        """With the frac_na the pipeline actually supplies, the coverage half of
        completeness is worth exactly 7.5 points to everyone."""
        metrics = {
            'f1_snp': 0.90, 'f1_indel': 0.85,
            'recall_snp': 0.90, 'recall_indel': 0.85,
            'truth_total_snp': 100, 'truth_total_indel': 50,
            'query_total_snp': 100, 'query_total_indel': 50,
            'fp_snp': 2, 'fp_indel': 1,
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'titv_truth_snp': 2.1, 'titv_query_snp': 2.1,
            'hethom_truth_snp': 1.5, 'hethom_query_snp': 1.5,
            'hethom_truth_indel': 1.5, 'hethom_query_indel': 1.5,
        }
        # coverage = 1 - 0.0 = 1.0, emphasis(1.0, 2.0) = 1.0, and it is half of
        # a component weighted 0.15 -> 100 * 0.15 * 0.5 = 7.5 points, flat.
        assert AdvancedScorer.emphasis(1.0 - 0.0, gamma=2.0) == pytest.approx(1.0)

        with_coverage = AdvancedScorer.compute_advanced_score(metrics)
        # Hypothetical: the same callset if coverage were 0 instead of 1.
        without_coverage = AdvancedScorer.compute_advanced_score(
            {**metrics, 'frac_na_snp': 1.0}
        )
        assert with_coverage - without_coverage == pytest.approx(7.5, abs=1e-6)


# ---------------------------------------------------------------------------
# TestV1IsFrozen
# ---------------------------------------------------------------------------

class TestV1IsFrozen:
    """v1 must score identically to the deployed scorer.

    v2 exists so that v1 never has to change. Changing compute_advanced_score
    would reorder a live leaderboard, and fields are routinely tied to fifteen
    decimal places.

    These pin its behaviour, so any change to v1 has to be deliberate rather
    than incidental.
    """

    def _m(self, **over):
        m = dict(
            f1_snp=0.9, f1_indel=0.8, recall_snp=0.9, recall_indel=0.8,
            truth_total_snp=2400, truth_total_indel=380,
            query_total_snp=2400, query_total_indel=380,
            fp_snp=0, fp_indel=0, frac_na_snp=0.0, frac_na_indel=0.0,
            titv_truth_snp=2.05, titv_query_snp=2.05,
            hethom_truth_snp=1.5, hethom_query_snp=1.5,
            hethom_truth_indel=1.2, hethom_query_indel=1.2,
        )
        m.update(over)
        return m

    def test_avg_recall_is_a_flat_mean_not_truth_weighted(self):
        """Weighting it by truth count changes the great majority of live
        submissions, so the flat mean stays until v2."""
        common = dict(f1_snp=0.9, f1_indel=0.9)
        all_snps = AdvancedScorer.compute_advanced_score(
            self._m(recall_snp=1.0, recall_indel=0.0, **common)
        )
        all_indels = AdvancedScorer.compute_advanced_score(
            self._m(recall_snp=0.0, recall_indel=1.0, **common)
        )
        assert all_snps == all_indels

    def test_an_absent_query_ratio_scores_the_same_as_a_matching_one(self):
        """The no-penalty default. v2 replaces it with a plausibility gate."""
        both = AdvancedScorer.compute_advanced_score(self._m())
        no_query_hethom = AdvancedScorer.compute_advanced_score(
            self._m(hethom_query_snp=0.0, hethom_query_indel=0.0)
        )
        assert no_query_hethom == both

    def test_a_non_finite_ratio_is_not_filtered_out(self):
        """Filtering it earlier changes the emitted score."""
        poisoned = AdvancedScorer.compute_advanced_score(
            self._m(titv_query_snp=float("inf"))
        )
        clean = AdvancedScorer.compute_advanced_score(self._m())
        assert poisoned < clean

    def test_a_non_finite_result_is_emitted_as_zero_not_raised(self):
        assert AdvancedScorer.compute_advanced_score(
            self._m(overcall_penalty=float("nan"))
        ) == 0.0
