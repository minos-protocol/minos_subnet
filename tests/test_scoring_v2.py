"""Tests for difficulty-weighted scoring (v2).

The point of v2 is that score should track variant-calling skill. The tests that
matter are the ones asserting where the weight actually lands: improving SNPs —
which the whole field already gets — should barely move the score, and improving
indels should move it a lot.
"""

import math

import pytest

from utils.scoring import (
    AdvancedScorer,
    DIFFICULTY_WEIGHTS,
    GATE_HETHOM_MAX_DELTA,
    GATE_TITV_MAX_DELTA,
    GERMLINE_FP_SCALE,
    classify_variant_difficulty,
    difficulty_class_counts,
    difficulty_weighted_f1,
    plausibility_gate,
)


def rec(vt="SNP", ref="A", alt="G", gt="0/1", cls="TP"):
    return {"variant_type": vt, "ref": ref, "alt": alt,
            "called_genotype": gt, "classification": cls}


class TestClassification:
    @pytest.mark.parametrize("ref,alt,expected", [
        ("A", "G", "snp_het"),
        ("A", "AT", "indel_1bp"),
        ("A", "ATT", "indel_2_3bp"),
        ("ATTTT", "A", "indel_4_7bp"),
        ("A", "A" + "T" * 9, "indel_8bp"),
    ])
    def test_buckets_by_indel_length(self, ref, alt, expected):
        assert classify_variant_difficulty(rec(ref=ref, alt=alt)) == expected

    def test_separates_hom_from_het_snps(self):
        assert classify_variant_difficulty(rec(gt="1/1")) == "snp_hom"
        assert classify_variant_difficulty(rec(gt="0/1")) == "snp_het"
        assert classify_variant_difficulty(rec(gt="1|1")) == "snp_hom"

    def test_uses_only_intrinsic_properties(self):
        """Classification must not depend on the callset or the field, or two
        validators scoring different subsets would disagree.

        This must exercise the SNP path. An indel is bucketed purely by allele
        length and never consults a genotype, so asserting the invariant with
        ref="A", alt="AT" passed no matter what the SNP branch did — which is
        exactly how the called-genotype bug survived a test named for the
        invariant it broke.
        """
        a = rec(ref="A", alt="AT", cls="TP")
        b = rec(ref="A", alt="AT", cls="FN")
        assert classify_variant_difficulty(a) == classify_variant_difficulty(b)

        # The real case: one truth variant, called by one miner and missed by
        # another. Same variant, so same class.
        called = {"variant_type": "SNP", "ref": "A", "alt": "G",
                  "truth_genotype": "1/1", "called_genotype": "1/1", "classification": "TP"}
        missed = {"variant_type": "SNP", "ref": "A", "alt": "G",
                  "truth_genotype": "1/1", "called_genotype": None, "classification": "FN"}
        assert classify_variant_difficulty(called) == classify_variant_difficulty(missed) == "snp_hom"

    def test_zygosity_comes_from_truth_not_from_the_call(self):
        """A missed hom SNP is still a hom SNP.

        An FN has no query genotype by definition, so bucketing on the call put
        every missed hom SNP in snp_het — charging 0.18 where 0.02 was correct,
        a 9x error on precisely the variants a miner is penalised for, and
        making a truth variant's class depend on who was scoring it.
        """
        missed_hom = {"variant_type": "SNP", "ref": "A", "alt": "G",
                      "truth_genotype": "1/1", "called_genotype": None, "classification": "FN"}
        assert classify_variant_difficulty(missed_hom) == "snp_hom"

        missed_het = {"variant_type": "SNP", "ref": "A", "alt": "G",
                      "truth_genotype": "0/1", "called_genotype": None, "classification": "FN"}
        assert classify_variant_difficulty(missed_het) == "snp_het"

    @pytest.mark.parametrize("gt,expected", [
        ("1/1", "snp_hom"), ("1|1", "snp_hom"),
        ("2/2", "snp_hom"), ("3|3", "snp_hom"),   # multi-allelic hom-alt
        ("0/1", "snp_het"), ("1/2", "snp_het"),
        ("0/0", "snp_het"), ("./.", "snp_het"), ("", "snp_het"),
    ])
    def test_any_homozygous_alt_genotype_is_hom(self, gt, expected):
        """2/2 is as homozygous as 1/1. Matching only the two literal spellings
        filed multi-allelic hom sites under the 9x-heavier het class."""
        r = {"variant_type": "SNP", "ref": "A", "alt": "G", "truth_genotype": gt}
        assert classify_variant_difficulty(r) == expected

    def test_same_length_alleles_do_not_take_the_heaviest_class(self):
        """A record typed INDEL whose alleles are the same length is a
        substitution. indel_1bp carries the HIGHEST weight (0.40), so falling
        through the length ladder handed an MNP the most-discriminating class."""
        mnp = {"variant_type": "INDEL", "ref": "AT", "alt": "GC", "truth_genotype": "0/1"}
        assert classify_variant_difficulty(mnp) == "snp_het"


class TestWeights:
    def test_weights_sum_to_one(self):
        assert sum(DIFFICULTY_WEIGHTS.values()) == pytest.approx(1.0)

    def test_solved_classes_carry_near_zero_weight(self):
        """SNP hom varies by sd 0.0006 across the whole field — it must not
        dominate the score the way truth-count weighting made it."""
        assert DIFFICULTY_WEIGHTS["snp_hom"] <= 0.05
        assert DIFFICULTY_WEIGHTS["indel_1bp"] > DIFFICULTY_WEIGHTS["snp_het"]
        assert DIFFICULTY_WEIGHTS["indel_1bp"] > 5 * DIFFICULTY_WEIGHTS["snp_hom"]


class TestDifficultyWeightedF1:
    def test_perfect_calling_scores_one(self):
        counts = {k: {"tp": 10, "fn": 0, "fp": 0} for k in DIFFICULTY_WEIGHTS}
        assert difficulty_weighted_f1(counts) == pytest.approx(1.0)

    def test_absent_class_is_dropped_and_weights_renormalised(self):
        """A region with no long indels must not score as if the miner failed
        them — otherwise the score depends on which region you were given."""
        counts = {"snp_het": {"tp": 10, "fn": 0, "fp": 0},
                  "indel_1bp": {"tp": 10, "fn": 0, "fp": 0}}
        assert difficulty_weighted_f1(counts) == pytest.approx(1.0)

    def test_returns_none_when_nothing_is_scorable(self):
        assert difficulty_weighted_f1({}) is None
        # Truth-free AND call-free: genuinely nothing to score.
        assert difficulty_weighted_f1({"snp_het": {"tp": 0, "fn": 0, "fp": 0}}) is None

    def test_false_positives_are_not_free_in_a_class_with_no_truth(self):
        """A class with no truth variants used to be renormalised away, which
        made its false positives cost nothing: emit 500 long-indel FPs into a
        region containing no long indels and the class simply vanished. Absent
        truth only justifies dropping the class when the miner called nothing
        there either."""
        assert difficulty_weighted_f1({"snp_het": {"tp": 0, "fn": 0, "fp": 5}}) == 0.0

        clean = {"snp_het": {"tp": 10, "fn": 0, "fp": 0}}
        polluted = {
            "snp_het": {"tp": 10, "fn": 0, "fp": 0},
            "indel_8bp": {"tp": 0, "fn": 0, "fp": 500},
        }
        assert difficulty_weighted_f1(clean) == 1.0
        assert difficulty_weighted_f1(polluted) < difficulty_weighted_f1(clean)

    def test_indel_failure_costs_far_more_than_snp_failure(self):
        """THE POINT OF v2. Under truth-count weighting the SNP miss dominates
        because SNPs are ~83% of the truth set; here the indel miss must."""
        perfect = {k: {"tp": 100, "fn": 0, "fp": 0} for k in DIFFICULTY_WEIGHTS}

        snp_miss = {k: dict(v) for k, v in perfect.items()}
        snp_miss["snp_hom"] = {"tp": 50, "fn": 50, "fp": 0}

        indel_miss = {k: dict(v) for k, v in perfect.items()}
        indel_miss["indel_1bp"] = {"tp": 50, "fn": 50, "fp": 0}

        loss_snp = 1.0 - difficulty_weighted_f1(snp_miss)
        loss_indel = 1.0 - difficulty_weighted_f1(indel_miss)
        assert loss_indel > 10 * loss_snp, (
            f"indel loss {loss_indel:.4f} should dwarf snp_hom loss {loss_snp:.4f}"
        )

    def test_false_positives_reduce_the_class_f1(self):
        clean = {"indel_1bp": {"tp": 100, "fn": 0, "fp": 0}}
        noisy = {"indel_1bp": {"tp": 100, "fn": 0, "fp": 100}}
        assert difficulty_weighted_f1(noisy) < difficulty_weighted_f1(clean)


class TestPlausibilityGate:
    def test_passes_a_normal_callset(self):
        assert plausibility_gate({"titv_truth_snp": 2.29, "titv_query_snp": 2.18,
                                  "hethom_truth_snp": 1.60, "hethom_query_snp": 1.72}) is True

    def test_rejects_a_wildly_wrong_ratio(self):
        assert plausibility_gate({"titv_truth_snp": 2.0,
                                  "titv_query_snp": 2.0 + GATE_TITV_MAX_DELTA + 0.1}) is False
        assert plausibility_gate({"hethom_truth_snp": 1.5,
                                  "hethom_query_snp": 1.5 + GATE_HETHOM_MAX_DELTA + 0.1}) is False

    def test_undefined_ratio_neither_passes_nor_earns(self):
        """v1 paid FULL MARKS for an undefined ratio — an absent metric scored
        better than an imperfect one. A gate cannot be farmed this way: there is
        nothing to earn, only something to fail."""
        assert plausibility_gate({"titv_truth_snp": 0, "titv_query_snp": 0}) is True
        assert plausibility_gate({}) is True


class TestComputeScoreV2:
    def _counts(self, f1=1.0):
        n = 100
        tp = int(round(n * f1))
        return {k: {"tp": tp, "fn": n - tp, "fp": 0} for k in DIFFICULTY_WEIGHTS}

    def test_declines_without_per_variant_records(self):
        """Must return None so the caller falls back to v1, never score everyone
        identically on missing data."""
        assert AdvancedScorer.compute_score_v2({"fp_per_target": 1.0}, None) is None
        assert AdvancedScorer.compute_score_v2({"fp_per_target": 1.0}, {}) is None

    def test_declines_without_a_germline_fp_measurement(self):
        """Scoring on core alone would reward flooding."""
        assert AdvancedScorer.compute_score_v2({}, self._counts()) is None

    def test_gated_callset_scores_zero(self):
        s = AdvancedScorer.compute_score_v2(
            {"fp_per_target": 0.0, "titv_truth_snp": 2.0, "titv_query_snp": 9.0},
            self._counts())
        assert s == 0.0

    def test_perfect_and_clean_scores_one_hundred(self):
        s = AdvancedScorer.compute_score_v2({"fp_per_target": 0.0}, self._counts())
        assert s == pytest.approx(100.0)

    def test_germline_fp_has_no_free_band_and_no_cap(self):
        """v1 charged nothing below fp_per_target 10 and capped at 45. Here the
        FIRST false positive costs something and the cost never stops growing."""
        counts = self._counts()
        scores = [AdvancedScorer.compute_score_v2({"fp_per_target": f}, counts)
                  for f in (0.0, 0.5, 2.0, 9.0, 10.0, 25.0, 100.0)]
        assert all(a > b for a, b in zip(scores, scores[1:])), "must be strictly monotone"
        assert scores[0] - scores[1] > 0, "the first FP must cost something"
        assert scores[-2] > scores[-1], "no cap: cost must keep growing past 45"

    def test_germline_decay_matches_the_documented_scale(self):
        counts = self._counts()
        s = AdvancedScorer.compute_score_v2({"fp_per_target": GERMLINE_FP_SCALE}, counts)
        expected = 100.0 * (0.70 * 1.0 + 0.30 * math.exp(-1.0))
        assert s == pytest.approx(expected)

    def test_is_deterministic(self):
        """Consensus safety: same input, same score, every time and everywhere."""
        m = {"fp_per_target": 3.3, "titv_truth_snp": 2.2, "titv_query_snp": 2.1}
        c = self._counts(0.9)
        assert len({AdvancedScorer.compute_score_v2(m, c) for _ in range(20)}) == 1


class TestClassCounts:
    def test_counts_tp_fn_fp_per_class(self):
        recs = [rec(cls="TP"), rec(cls="FN"), rec(cls="FP"),
                rec(ref="A", alt="AT", cls="TP"), rec(ref="A", alt="AT", cls="FN")]
        c = difficulty_class_counts(recs)
        assert c["snp_het"] == {"tp": 1, "fn": 1, "fp": 1}
        assert c["indel_1bp"] == {"tp": 1, "fn": 1, "fp": 0}

    def test_ignores_unassessed_records(self):
        c = difficulty_class_counts([rec(cls="N"), rec(cls=None), rec(cls="UNK")])
        assert all(v == {"tp": 0, "fn": 0, "fp": 0} for v in c.values())

    def test_empty_input_is_safe(self):
        assert difficulty_weighted_f1(difficulty_class_counts([])) is None


class TestV2EnforcesItsInputContract:
    """max(0.0, nan) returns 0.0 because NaN compares False against everything,
    so a broken fp_per_target silently became exp(0) == 1.0 -- a PERFECT
    germline component. "This measurement is broken" must never read as "this
    miner was flawless"."""

    COUNTS = {"snp_het": {"tp": 10, "fp": 0, "fn": 0}}

    @pytest.mark.parametrize("bad", [
        float("nan"), float("inf"), float("-inf"), -1.0, -0.0001,
    ])
    def test_a_non_finite_or_negative_fp_rate_is_refused(self, bad):
        out = AdvancedScorer.compute_score_v2({"fp_per_target": bad}, self.COUNTS)
        assert out is None, f"fp_per_target={bad!r} produced a score of {out}"

    @pytest.mark.parametrize("bad", ["abc", {}, [], object()])
    def test_a_non_numeric_fp_rate_is_refused(self, bad):
        assert AdvancedScorer.compute_score_v2(
            {"fp_per_target": bad}, self.COUNTS) is None

    def test_a_zero_fp_rate_is_still_a_perfect_germline(self):
        """Genuinely zero is different from broken and must keep scoring."""
        out = AdvancedScorer.compute_score_v2({"fp_per_target": 0.0}, self.COUNTS)
        assert out is not None and out == pytest.approx(100.0)

    def test_the_emitted_score_is_within_range(self):
        out = AdvancedScorer.compute_score_v2({"fp_per_target": 2.0}, self.COUNTS)
        assert out is not None and 0.0 <= out <= 100.0


class TestTheGateCannotBeSidesteppedByOmission:
    """Under v1 these ratios were scored components, so an absent one earned
    nothing. In v2 the gate is pass/fail, so "absent means skip" is a free pass
    a miner can choose -- e.g. by emitting no homozygous SNP calls."""

    def _metrics(self, **kw):
        m = {"fp_per_target": 0.0,
             "titv_truth_snp": 2.0, "titv_query_snp": 2.0,
             "hethom_truth_snp": 1.5, "hethom_query_snp": 1.5}
        m.update(kw)
        return m

    def test_a_measurable_truth_with_no_query_ratio_fails(self):
        """The bypass: truth has homozygous SNPs, the query called none."""
        assert plausibility_gate(self._metrics(hethom_query_snp=0)) is False

    @pytest.mark.parametrize("absent", [None, 0, 0.0, float("nan"),
                                        float("inf"), -1.0, "abc"])
    def test_every_unusable_query_ratio_fails_the_gate(self, absent):
        assert plausibility_gate(self._metrics(titv_query_snp=absent)) is False

    def test_a_missing_query_key_fails_the_gate(self):
        m = self._metrics()
        del m["hethom_query_snp"]
        assert plausibility_gate(m) is False

    @pytest.mark.parametrize("unusable", [None, 0, float("nan"), -1.0])
    def test_an_unmeasurable_truth_ratio_is_skipped_not_failed(self, unusable):
        """A round with no measurable truth ratio is the round's property, not
        the miner's -- it must not disqualify them."""
        assert plausibility_gate(
            self._metrics(titv_truth_snp=unusable, titv_query_snp=unusable)) is True

    def test_both_undefined_still_passes(self):
        assert plausibility_gate({"titv_truth_snp": 0, "titv_query_snp": 0,
                                  "hethom_truth_snp": 0, "hethom_query_snp": 0}) is True

    def test_a_real_deviation_still_fails(self):
        assert plausibility_gate(self._metrics(titv_query_snp=9.0)) is False

    def test_the_bypass_scores_zero_end_to_end(self):
        """A gate failure is 0.0, not None: the miner was scored and failed."""
        out = AdvancedScorer.compute_score_v2(
            self._metrics(hethom_query_snp=0),
            {"snp_het": {"tp": 10, "fp": 0, "fn": 0}})
        assert out == 0.0
