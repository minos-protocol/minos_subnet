"""Hardening tests for weight computation: NaN-proof dust decay and a
validator-independent winner on exact ties."""

import logging
import math

import pytest

from utils.weight_tracking import (
    ScoreTracker,
    CANONICAL_TIEBREAK_TOLERANCE,
    DEFAULT_BURN_RATE,
    DEFAULT_DUST_TOP_N,
    DEFAULT_WINNER_WEIGHT,
    MIN_PARTICIPATION_ROUNDS,
)


def _tracker(scores: dict) -> ScoreTracker:
    """Tracker whose miners are all eligible, scored in ``scores`` order."""
    tracker = ScoreTracker(min_rounds=MIN_PARTICIPATION_ROUNDS)
    tracker.recover_from_platform_state(
        [],
        [
            {"round_id": f"seed_{idx}", "scored_hotkeys": list(scores)}
            for idx in range(MIN_PARTICIPATION_ROUNDS)
        ],
    )
    for hotkey, score in scores.items():
        tracker.update(hotkey, score)
    return tracker


def _weights(tracker, hotkeys, *, dust_decay, **kwargs):
    return tracker.get_winner_heavy_pruning_dust_weights(
        hotkeys,
        burn_rate=DEFAULT_BURN_RATE,
        winner_weight=DEFAULT_WINNER_WEIGHT,
        dust_top_n=DEFAULT_DUST_TOP_N,
        dust_decay=dust_decay,
        **kwargs,
    )


class TestDustDecayValidation:
    """json.loads accepts a bare Infinity/NaN from /scoring/network-config."""

    @pytest.mark.parametrize(
        "bad_decay",
        [float("inf"), float("-inf"), float("nan"), "Infinity", "NaN", 1.5, -0.1],
    )
    def test_non_finite_or_out_of_range_decay_is_rejected(self, bad_decay):
        tracker = _tracker({"hk_a": 0.9, "hk_b": 0.8, "hk_c": 0.7, "hk_d": 0.6})
        with pytest.raises(ValueError, match="dust_decay"):
            _weights(tracker, list(tracker.round_scores), dust_decay=bad_decay)

    @pytest.mark.parametrize("good_decay", [0.0, 0.5, 0.8, 1.0])
    def test_in_range_decay_is_accepted_and_finite(self, good_decay):
        hotkeys = ["hk_a", "hk_b", "hk_c", "hk_d"]
        tracker = _tracker({"hk_a": 0.9, "hk_b": 0.8, "hk_c": 0.7, "hk_d": 0.6})
        weights = _weights(tracker, hotkeys, dust_decay=good_decay)

        assert all(math.isfinite(w) for w in weights.values())
        assert weights["hk_a"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert sum(weights.values()) == pytest.approx(1.0 - DEFAULT_BURN_RATE)

    def test_infinite_decay_would_have_produced_nan_weights(self):
        """The guard is load-bearing: inf decay makes dust_total inf, and the
        NaN quotients it yields for ranks 3+ pass every downstream comparison."""
        decay = float("inf")
        dust_raw = [decay ** i for i in range(3)]
        dust_total = sum(dust_raw)
        nan_weights = [0.1 * raw / dust_total for raw in dust_raw]

        assert any(math.isnan(w) for w in nan_weights)
        # Why nothing downstream catches it: every NaN comparison is False.
        assert not any(w > 1.0 for w in nan_weights if math.isnan(w))
        assert not any(w < 0.0 for w in nan_weights if math.isnan(w))

        tracker = _tracker({"hk_a": 0.9, "hk_b": 0.8, "hk_c": 0.7, "hk_d": 0.6})
        with pytest.raises(ValueError):
            _weights(tracker, list(tracker.round_scores), dust_decay=decay)


class TestExactTieIsValidatorIndependent:
    """Two honest validators must pick the same winner from identical data."""

    def test_exact_tie_without_timestamps_ignores_insertion_order(self):
        forward = _tracker({"hk_alpha": 0.75, "hk_beta": 0.75, "hk_gamma": 0.75})
        reverse = _tracker({"hk_gamma": 0.75, "hk_beta": 0.75, "hk_alpha": 0.75})
        hotkeys = ["hk_alpha", "hk_beta", "hk_gamma"]

        w_fwd = _weights(forward, hotkeys, dust_decay=0.8)
        w_rev = _weights(reverse, list(reversed(hotkeys)), dust_decay=0.8)

        assert w_fwd == pytest.approx(w_rev)
        assert w_fwd["hk_alpha"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert forward.get_rankings(hotkeys) == reverse.get_rankings(hotkeys)

    def test_submission_time_still_outranks_hotkey_order(self):
        """The hotkey tiebreak is a last resort, not a replacement."""
        tracker = _tracker({"hk_alpha": 0.75, "hk_beta": 0.75})
        weights = _weights(
            tracker,
            ["hk_alpha", "hk_beta"],
            dust_decay=0.8,
            # hk_beta submitted first, so it wins despite sorting after hk_alpha.
            submission_times={"hk_alpha": 200.0, "hk_beta": 100.0},
        )

        assert weights["hk_beta"] == pytest.approx(DEFAULT_WINNER_WEIGHT)

    def test_partial_timestamps_are_order_independent(self):
        """A miner with no submitted_at falls to inf and ties with every other
        timestamp-less miner; that residual tie must not depend on order."""
        forward = _tracker({"hk_alpha": 0.5, "hk_beta": 0.5})
        reverse = _tracker({"hk_beta": 0.5, "hk_alpha": 0.5})

        assert (
            forward.get_rankings(["hk_alpha", "hk_beta"], {})
            == reverse.get_rankings(["hk_beta", "hk_alpha"], {})
        )


class TestCanonicalTiebreakFallbackIsLogged:
    def test_unusable_canonical_candidates_log_a_warning(self, caplog):
        tracker = _tracker({"hk_alpha": 0.9, "hk_beta": 0.5})
        with caplog.at_level(logging.WARNING, logger="utils.weight_tracking"):
            weights = _weights(
                tracker,
                ["hk_alpha", "hk_beta"],
                dust_decay=0.8,
                # hk_zeta is unknown locally, hk_beta is far outside tolerance.
                canonical_ranking=["hk_zeta", "hk_beta"],
            )

        assert weights["hk_alpha"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert "Canonical tiebreak did not apply" in caplog.text

    def test_applied_canonical_tiebreak_does_not_warn(self, caplog):
        gap = CANONICAL_TIEBREAK_TOLERANCE / 2
        tracker = _tracker({"hk_alpha": 0.9, "hk_beta": 0.9 - gap})
        with caplog.at_level(logging.WARNING, logger="utils.weight_tracking"):
            weights = _weights(
                tracker,
                ["hk_alpha", "hk_beta"],
                dust_decay=0.8,
                canonical_ranking=["hk_beta"],
            )

        assert weights["hk_beta"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert "Canonical tiebreak did not apply" not in caplog.text

    def test_canonical_agreeing_with_local_rank_one_does_not_warn(self, caplog):
        tracker = _tracker({"hk_alpha": 0.9, "hk_beta": 0.5})
        with caplog.at_level(logging.WARNING, logger="utils.weight_tracking"):
            _weights(
                tracker,
                ["hk_alpha", "hk_beta"],
                dust_decay=0.8,
                canonical_ranking=["hk_alpha", "hk_beta"],
            )

        assert "Canonical tiebreak did not apply" not in caplog.text
