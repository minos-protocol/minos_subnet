"""Tests for round-only winner weighting."""

import pytest

from utils.weight_tracking import (
    ScoreTracker,
    DEFAULT_BURN_RATE,
    DEFAULT_WINNER_WEIGHT,
    DEFAULT_DUST_TOP_N,
    DEFAULT_DUST_DECAY,
    CANONICAL_TIEBREAK_TOLERANCE,
    MIN_PARTICIPATION_ROUNDS,
    PARTICIPATION_WINDOW,
)


DEFAULT_REWARD_POLICY = {
    "burn_rate": DEFAULT_BURN_RATE,
    "winner_weight": DEFAULT_WINNER_WEIGHT,
    "dust_top_n": DEFAULT_DUST_TOP_N,
    "dust_decay": DEFAULT_DUST_DECAY,
}


def _record_scores(tracker: ScoreTracker, round_id: str, scores: dict):
    for hotkey, score in scores.items():
        tracker.update(hotkey, score)
    tracker.record_round(round_id, list(scores))


def _seed_participation(tracker: ScoreTracker, hotkeys, count: int):
    tracker.recover_from_platform_state(
        [],
        [
            {"round_id": f"seed_{idx}", "scored_hotkeys": list(hotkeys)}
            for idx in range(count)
        ],
    )


class TestRoundOnlyState:
    def test_update_records_latest_round_score_directly(self, score_tracker):
        assert score_tracker.update("hk_a", 1.0) == pytest.approx(1.0)
        assert score_tracker.update("hk_a", 0.5) == pytest.approx(0.5)
        assert score_tracker.round_scores["hk_a"] == pytest.approx(0.5)
        assert score_tracker.last_raw_scores["hk_a"] == pytest.approx(0.5)

    def test_update_rejects_invalid_round_scores(self, score_tracker):
        for bad_score in (None, "nan", -0.1, 0.0, 1.1):
            with pytest.raises(ValueError):
                score_tracker.update("hk_bad", bad_score)
        assert "hk_bad" not in score_tracker.round_scores

    def test_below_min_participation_is_not_eligible(self, score_tracker):
        _seed_participation(score_tracker, ["hk_a"], MIN_PARTICIPATION_ROUNDS - 1)

        assert score_tracker.get_participation_count("hk_a") == MIN_PARTICIPATION_ROUNDS - 1
        assert not score_tracker.is_eligible("hk_a")

    def test_at_min_participation_is_eligible(self, score_tracker):
        _seed_participation(score_tracker, ["hk_a"], MIN_PARTICIPATION_ROUNDS)

        assert score_tracker.get_participation_count("hk_a") == MIN_PARTICIPATION_ROUNDS
        assert score_tracker.is_eligible("hk_a")

    def test_window_slide_can_drop_eligibility(self, score_tracker):
        round_history = [
            {
                "round_id": f"round_{idx}",
                "scored_hotkeys": ["hk_a"] if idx < MIN_PARTICIPATION_ROUNDS else ["hk_b"],
            }
            for idx in range(PARTICIPATION_WINDOW + 1)
        ]

        score_tracker.recover_from_platform_state([], round_history)

        assert score_tracker.get_participation_count("hk_a") == MIN_PARTICIPATION_ROUNDS - 1
        assert not score_tracker.is_eligible("hk_a")

    def test_current_round_counts_toward_window_eligibility(self, score_tracker):
        _seed_participation(score_tracker, ["hk_a"], MIN_PARTICIPATION_ROUNDS - 1)
        _record_scores(score_tracker, "r1", {"hk_a": 0.9, "hk_b": 0.7})

        assert score_tracker.is_eligible("hk_a")
        assert not score_tracker.is_eligible("hk_b")
        assert score_tracker.get_participation_count("hk_a") == MIN_PARTICIPATION_ROUNDS
        assert score_tracker.get_participation_count("hk_b") == 1
        assert score_tracker.get_participation_count("hk_unknown") == 0
        assert not score_tracker.is_eligible("hk_unknown")

    def test_record_round_drops_stale_scores_but_keeps_recent_counts(self, score_tracker):
        _record_scores(score_tracker, "r1", {"hk_a": 0.9, "hk_b": 0.7})
        _record_scores(score_tracker, "r2", {"hk_b": 0.8, "hk_c": 0.6})

        assert set(score_tracker.round_scores) == {"hk_b", "hk_c"}
        assert score_tracker.get_participation_count("hk_a") == 1
        assert score_tracker.get_participation_count("hk_b") == 2

    def test_recovery_ignores_historical_scores_but_loads_recent_rounds(self, score_tracker):
        score_tracker.recover_from_platform_state(
            [{
                "miner_hotkey": "hk_old",
                "ema_score": 0.99,
                "participation_count": MIN_PARTICIPATION_ROUNDS,
            }],
            [
                {"round_id": f"old_{idx}", "scored_hotkeys": ["hk_old"]}
                for idx in range(MIN_PARTICIPATION_ROUNDS)
            ],
        )

        assert score_tracker.round_scores == {}
        assert score_tracker.last_raw_scores == {}
        assert len(score_tracker.round_history) == MIN_PARTICIPATION_ROUNDS
        assert score_tracker.get_participation_count("hk_old") == MIN_PARTICIPATION_ROUNDS
        assert score_tracker.is_eligible("hk_old")

    def test_recovery_trims_to_recent_window(self, score_tracker):
        # Rounds beyond the participation window that recovery must drop.
        trimmed = 5
        total = PARTICIPATION_WINDOW + trimmed
        # hk_old only appears in the oldest rounds, so after the window trims the
        # first `trimmed` it lands one round below the gate; hk_recent fills the
        # rest. Driven off the constants so it holds for any gate value.
        old_round_span = trimmed + (MIN_PARTICIPATION_ROUNDS - 1)
        round_history = [
            {
                "round_id": f"old_{idx}",
                "scored_hotkeys": ["hk_old"] if idx < old_round_span else ["hk_recent"],
            }
            for idx in range(total)
        ]

        score_tracker.recover_from_platform_state([], round_history)

        assert len(score_tracker.round_history) == PARTICIPATION_WINDOW
        assert score_tracker.get_participation_count("hk_old") == MIN_PARTICIPATION_ROUNDS - 1
        assert score_tracker.get_participation_count("hk_recent") == (
            PARTICIPATION_WINDOW - (MIN_PARTICIPATION_ROUNDS - 1)
        )
        assert not score_tracker.is_eligible("hk_old")
        assert score_tracker.is_eligible("hk_recent")


class TestRoundOnlyWeights:
    def test_top_ten_distribution_uses_current_round_scores(self, score_tracker):
        scores = {f"hk_{i}": 1.0 - (i * 0.01) for i in range(1, 12)}
        _seed_participation(score_tracker, scores, MIN_PARTICIPATION_ROUNDS - 1)
        _record_scores(score_tracker, "r1", scores)

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            list(scores), **DEFAULT_REWARD_POLICY
        )

        assert weights["hk_1"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert weights["hk_11"] == pytest.approx(0.0)

        dust_pool = 1.0 - DEFAULT_BURN_RATE - DEFAULT_WINNER_WEIGHT
        dust_raw = [DEFAULT_DUST_DECAY ** i for i in range(DEFAULT_DUST_TOP_N - 1)]
        dust_total = sum(dust_raw)
        for rank in range(2, 11):
            expected = dust_pool * dust_raw[rank - 2] / dust_total
            assert weights[f"hk_{rank}"] == pytest.approx(expected)

        assert sum(weights.values()) == pytest.approx(1.0 - DEFAULT_BURN_RATE)

    def test_single_positive_winner_keeps_winner_weight_only(self, score_tracker):
        _seed_participation(score_tracker, ["hk_a"], MIN_PARTICIPATION_ROUNDS - 1)
        _record_scores(score_tracker, "r1", {"hk_a": 0.9})

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a"], **DEFAULT_REWARD_POLICY
        )

        assert weights["hk_a"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert sum(weights.values()) == pytest.approx(DEFAULT_WINNER_WEIGHT)

    def test_no_current_scores_return_zero_weights(self, score_tracker):
        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"], **DEFAULT_REWARD_POLICY
        )

        assert weights == {"hk_a": 0.0, "hk_b": 0.0}

    def test_earliest_submission_breaks_exact_score_tie(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.9, "hk_b": 0.9})

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"],
            submission_times={"hk_a": 200.0, "hk_b": 100.0},
            **DEFAULT_REWARD_POLICY,
        )

        assert weights["hk_b"] == pytest.approx(DEFAULT_WINNER_WEIGHT)

    def test_ineligible_high_score_cannot_win(self, score_tracker):
        _seed_participation(score_tracker, ["hk_b"], MIN_PARTICIPATION_ROUNDS - 1)
        _record_scores(score_tracker, "r1", {"hk_a": 1.0, "hk_b": 0.7})

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"], **DEFAULT_REWARD_POLICY
        )

        assert not score_tracker.is_eligible("hk_a")
        assert score_tracker.is_eligible("hk_b")
        assert weights["hk_a"] == pytest.approx(0.0)
        assert weights["hk_b"] == pytest.approx(DEFAULT_WINNER_WEIGHT)


class TestCanonicalTiebreak:
    def test_canonical_tiebreak_window_is_one_tenth_percent(self):
        assert CANONICAL_TIEBREAK_TOLERANCE == pytest.approx(0.001)

    def test_canonical_needed_for_close_current_round_scores(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.7000, "hk_b": 0.6995})
        assert score_tracker.needs_canonical_tiebreak(["hk_a", "hk_b"])

    def test_canonical_not_needed_for_clear_current_round_winner(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.700, "hk_b": 0.698})
        assert not score_tracker.needs_canonical_tiebreak(["hk_a", "hk_b"])

    def test_canonical_used_when_within_tolerance(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.7000, "hk_b": 0.6995})

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"],
            canonical_top="hk_b",
            **DEFAULT_REWARD_POLICY,
        )

        assert weights["hk_b"] == pytest.approx(DEFAULT_WINNER_WEIGHT)

    def test_canonical_ignored_when_outside_tolerance(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(
            score_tracker,
            "r1",
            {"hk_a": 0.700, "hk_b": 0.700 - CANONICAL_TIEBREAK_TOLERANCE - 0.001},
        )

        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"],
            canonical_top="hk_b",
            **DEFAULT_REWARD_POLICY,
        )

        assert weights["hk_a"] == pytest.approx(DEFAULT_WINNER_WEIGHT)


class TestRankingsAndHistory:
    def test_get_rankings(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b", "hk_c"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.90, "hk_b": 0.70})

        rankings = score_tracker.get_rankings(["hk_a", "hk_b", "hk_c", "hk_unknown"])

        assert rankings["hk_a"] == 1
        assert rankings["hk_b"] == 2
        assert rankings["hk_c"] is None
        assert rankings["hk_unknown"] is None

    def test_build_weight_history_structure(self, score_tracker):
        _seed_participation(
            score_tracker, ["hk_a", "hk_b"], MIN_PARTICIPATION_ROUNDS - 1
        )
        _record_scores(score_tracker, "r1", {"hk_a": 0.90, "hk_b": 0.70})
        weights = score_tracker.get_winner_heavy_pruning_dust_weights(
            ["hk_a", "hk_b"], **DEFAULT_REWARD_POLICY
        )

        entries = score_tracker.build_weight_history(
            round_id="r1",
            validator_hotkey="val_hk",
            miner_hotkeys=["hk_a", "hk_b"],
            weights=weights,
        )

        assert len(entries) == 2
        keys = {"miner_hotkey", "raw_score", "ema_score", "rank",
                "weight", "eligible", "participation_count"}
        for entry in entries:
            assert set(entry.keys()) == keys

        winner = [e for e in entries if e["miner_hotkey"] == "hk_a"][0]
        assert winner["raw_score"] == pytest.approx(0.90)
        assert winner["ema_score"] is None
        assert winner["weight"] == pytest.approx(DEFAULT_WINNER_WEIGHT)
        assert winner["eligible"] is True
        assert winner["participation_count"] == MIN_PARTICIPATION_ROUNDS
        assert winner["rank"] == 1
