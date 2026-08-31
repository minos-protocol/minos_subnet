"""One reward candidate per owner.

The rule: a coldkey contributes ONE candidate per round, so running many hotkeys
under one owner stops multiplying an operator's share of the reward.

The property that makes it fair is that the choice is made BEFORE scores are
known — by explicit designation, or by earliest submission. These tests pin that
directly, because a collapse that could see scores would let an operator submit a
portfolio and keep whichever won, which is the behaviour being removed.

They also pin what this deliberately does NOT do: it never compares configs
across owners. Two operators who independently reach the same config are two
candidates. Similarity is not identity, and on this subnet the field converges by
design.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from utils.candidate_normalization import (  # noqa: E402
    CandidateNormalizationError,
    normalize_candidates,
)

OWNER_A, OWNER_B = "5ColdA", "5ColdB"


def _run(hotkeys, owners, scores, times=None, designations=None):
    return normalize_candidates(
        round_id="2026-08-31T00:08:00+00:00",
        candidate_hotkeys=hotkeys,
        owner_by_hotkey=owners,
        selection_score_by_hotkey=scores,
        selection_time_by_hotkey=times,
        reward_designated_by_hotkey=designations,
    )


class TestOneCandidatePerOwner:
    def test_many_hotkeys_under_one_coldkey_yield_one_candidate(self):
        hotkeys = [f"hk{i}" for i in range(8)]
        result = _run(
            hotkeys,
            {hk: OWNER_A for hk in hotkeys},
            {hk: 0.5 for hk in hotkeys},
            times={hk: float(i) for i, hk in enumerate(hotkeys)},
        )
        assert len(result.selected_hotkeys) == 1
        assert result.unique_owner_count == 1

    def test_separate_owners_each_keep_a_candidate(self):
        result = _run(
            ["hk_a", "hk_b"],
            {"hk_a": OWNER_A, "hk_b": OWNER_B},
            {"hk_a": 0.5, "hk_b": 0.4},
            times={"hk_a": 1.0, "hk_b": 2.0},
        )
        assert set(result.selected_hotkeys) == {"hk_a", "hk_b"}

    def test_the_alternates_are_recorded_as_represented(self):
        hotkeys = ["hk0", "hk1", "hk2"]
        result = _run(
            hotkeys,
            {hk: OWNER_A for hk in hotkeys},
            {hk: 0.5 for hk in hotkeys},
            times={"hk0": 1.0, "hk1": 2.0, "hk2": 3.0},
        )
        kept = result.selected_hotkeys[0]
        for hk in hotkeys:
            if hk == kept:
                continue
            assert result.decisions[hk].internal_reason == "owner_alternate"
            assert result.decisions[hk].represented_by == kept


class TestTheChoiceCannotSeeScores:
    """The load-bearing property. If the collapse could see scores, an operator
    could submit a portfolio of hotkeys and keep whichever happened to win."""

    def test_without_designation_the_earliest_submission_wins_not_the_best(self):
        result = _run(
            ["early_low", "late_high"],
            {"early_low": OWNER_A, "late_high": OWNER_A},
            {"early_low": 0.10, "late_high": 0.99},   # the later one scored far better
            times={"early_low": 1.0, "late_high": 2.0},
        )
        assert result.selected_hotkeys == ("early_low",), (
            "the higher-scoring hotkey was kept — the collapse is score-aware"
        )

    def test_designation_wins_over_both_score_and_time(self):
        result = _run(
            ["first", "chosen"],
            {"first": OWNER_A, "chosen": OWNER_A},
            {"first": 0.99, "chosen": 0.10},
            times={"first": 1.0, "chosen": 2.0},
            designations={"first": False, "chosen": True},
        )
        assert result.selected_hotkeys == ("chosen",)

    def test_an_owner_designating_two_candidates_is_an_error(self):
        """Not a tie to resolve later: resolving it after scores are known is
        exactly the post-score choice this removes."""
        with pytest.raises(CandidateNormalizationError, match="more than one"):
            _run(
                ["hk_a", "hk_b"],
                {"hk_a": OWNER_A, "hk_b": OWNER_A},
                {"hk_a": 0.5, "hk_b": 0.5},
                times={"hk_a": 1.0, "hk_b": 2.0},
                designations={"hk_a": True, "hk_b": True},
            )

    def test_an_owner_designating_nobody_contributes_nobody(self):
        result = _run(
            ["hk_a", "hk_b"],
            {"hk_a": OWNER_A, "hk_b": OWNER_A},
            {"hk_a": 0.5, "hk_b": 0.5},
            times={"hk_a": 1.0, "hk_b": 2.0},
            designations={"hk_a": False, "hk_b": False},
        )
        assert result.selected_hotkeys == ()

    def test_ties_on_time_break_deterministically(self):
        """Two validators must reach the same answer or they submit different
        weights."""
        args = (
            ["hk_b", "hk_a"],
            {"hk_a": OWNER_A, "hk_b": OWNER_A},
            {"hk_a": 0.5, "hk_b": 0.5},
        )
        first = _run(*args, times={"hk_a": 1.0, "hk_b": 1.0})
        second = _run(["hk_a", "hk_b"], args[1], args[2], times={"hk_a": 1.0, "hk_b": 1.0})
        assert first.selected_hotkeys == second.selected_hotkeys


class TestConfigsAreNeverComparedAcrossOwners:
    """What was deliberately removed. It could not tell a sybil farm from two
    operators who independently found the same good config."""

    def test_two_owners_with_identical_everything_both_survive(self):
        result = _run(
            ["hk_a", "hk_b"],
            {"hk_a": OWNER_A, "hk_b": OWNER_B},
            {"hk_a": 0.75, "hk_b": 0.75},
            times={"hk_a": 1.0, "hk_b": 2.0},
        )
        assert set(result.selected_hotkeys) == {"hk_a", "hk_b"}, (
            "an owner was collapsed for resembling another — similarity is not identity"
        )

    def test_the_function_takes_no_config_or_token_argument(self):
        """If a token map ever comes back, cross-owner collapse came with it."""
        import inspect
        params = set(inspect.signature(normalize_candidates).parameters)
        assert not {p for p in params if "token" in p or "config" in p}


class TestRejectsMalformedInput:
    @pytest.mark.parametrize("score", [0.0, -0.1, 1.5, float("nan"), float("inf"), "x", None])
    def test_an_unusable_score_is_refused(self, score):
        with pytest.raises(CandidateNormalizationError):
            _run(["hk_a"], {"hk_a": OWNER_A}, {"hk_a": score})

    def test_a_missing_owner_is_refused(self):
        with pytest.raises(CandidateNormalizationError, match="missing owner"):
            _run(["hk_a"], {}, {"hk_a": 0.5})

    def test_duplicate_candidates_are_refused(self):
        with pytest.raises(CandidateNormalizationError, match="duplicates"):
            _run(["hk_a", "hk_a"], {"hk_a": OWNER_A}, {"hk_a": 0.5})

    def test_a_missing_round_id_is_refused(self):
        with pytest.raises(CandidateNormalizationError, match="round_id"):
            normalize_candidates(
                round_id="", candidate_hotkeys=["hk_a"],
                owner_by_hotkey={"hk_a": OWNER_A},
                selection_score_by_hotkey={"hk_a": 0.5},
            )

    def test_an_empty_field_is_not_an_error(self):
        result = _run([], {}, {})
        assert result.selected_hotkeys == ()
        assert result.audit_digest


class TestTheAuditRecord:
    def test_the_digest_is_stable_for_the_same_decision(self):
        args = (["hk_a", "hk_b"], {"hk_a": OWNER_A, "hk_b": OWNER_A},
                {"hk_a": 0.5, "hk_b": 0.5}, {"hk_a": 1.0, "hk_b": 2.0})
        assert _run(*args).audit_digest == _run(*args).audit_digest

    def test_a_different_decision_changes_the_digest(self):
        base = _run(["hk_a", "hk_b"], {"hk_a": OWNER_A, "hk_b": OWNER_A},
                    {"hk_a": 0.5, "hk_b": 0.5}, {"hk_a": 1.0, "hk_b": 2.0})
        split = _run(["hk_a", "hk_b"], {"hk_a": OWNER_A, "hk_b": OWNER_B},
                     {"hk_a": 0.5, "hk_b": 0.5}, {"hk_a": 1.0, "hk_b": 2.0})
        assert base.audit_digest != split.audit_digest

    def test_a_dropped_alternate_gets_a_bounded_receipt_reason(self):
        result = _run(["hk_a", "hk_b"], {"hk_a": OWNER_A, "hk_b": OWNER_A},
                      {"hk_a": 0.5, "hk_b": 0.5}, {"hk_a": 1.0, "hk_b": 2.0})
        dropped = [hk for hk in ("hk_a", "hk_b") if hk not in result.selected_hotkeys][0]
        assert result.decisions[dropped].receipt_reason == "OWNER_REWARD_SLOT_NOT_DESIGNATED"
