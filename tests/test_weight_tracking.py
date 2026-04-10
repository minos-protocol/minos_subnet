"""Comprehensive tests for utils.weight_tracking.ScoreTracker."""

import pytest

from utils.weight_tracking import (
    ScoreTracker,
    PARTICIPATION_WINDOW,
    WARMUP_WEIGHTS,
    SCORE_EPSILON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_eligible(tracker: ScoreTracker, hotkey: str, score: float = 0.80):
    """Shortcut: update + record enough rounds to make *hotkey* eligible.

    WARNING: calling this for multiple hotkeys on the same tracker causes
    decay and window-trim side-effects. Use _make_eligible_group instead.
    """
    for i in range(tracker.min_rounds):
        tracker.update(hotkey, score)
        tracker.record_round(f"elig-{hotkey}-{i}", [hotkey])


def _make_eligible_group(
    tracker: ScoreTracker,
    hotkey_scores: dict,
):
    """Make several miners eligible in the *same* rounds (avoids cross-decay).

    Args:
        hotkey_scores: {hotkey: raw_score, ...}
    """
    hotkeys = list(hotkey_scores.keys())
    for i in range(tracker.min_rounds):
        for hk, score in hotkey_scores.items():
            tracker.update(hk, score)
        tracker.record_round(f"elig-group-{i}", hotkeys)


def _make_active(tracker: ScoreTracker, hotkey: str, rounds: int = 1,
                 score: float = 0.80):
    """Record *rounds* rounds for *hotkey* (not necessarily eligible)."""
    for i in range(rounds):
        tracker.update(hotkey, score)
        tracker.record_round(f"active-{hotkey}-{i}", [hotkey])


# =========================================================================
# TestScoreTrackerInit
# =========================================================================

class TestScoreTrackerInit:
    """Verify constructor defaults and env-var overrides."""

    def test_default_values(self):
        tracker = ScoreTracker(alpha=0.1, decay_factor=0.95)
        assert tracker.alpha == 0.1
        assert tracker.decay_factor == 0.95
        assert tracker.min_rounds == 10  # module-level default

    def test_custom_values(self):
        tracker = ScoreTracker(alpha=0.25, min_rounds=5, decay_factor=0.90)
        assert tracker.alpha == 0.25
        assert tracker.min_rounds == 5
        assert tracker.decay_factor == 0.90

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("EMA_ALPHA", "0.42")
        monkeypatch.setenv("EMA_DECAY_FACTOR", "0.88")
        tracker = ScoreTracker(alpha=None, decay_factor=None)
        assert tracker.alpha == pytest.approx(0.42)
        assert tracker.decay_factor == pytest.approx(0.88)


# =========================================================================
# TestUpdate
# =========================================================================

class TestUpdate:
    """EMA update semantics."""

    def test_first_update_from_zero(self, score_tracker):
        """First update: EMA starts at 0, so new = alpha * score."""
        ema = score_tracker.update("hk_a", 1.0)
        assert ema == pytest.approx(0.1 * 1.0)

    def test_second_update(self, score_tracker):
        """Second update applies the full EMA formula."""
        score_tracker.update("hk_a", 1.0)
        ema2 = score_tracker.update("hk_a", 0.5)
        expected = (1 - 0.1) * 0.1 + 0.1 * 0.5  # 0.09 + 0.05 = 0.14
        assert ema2 == pytest.approx(expected)

    def test_converges_toward_repeated_score(self, score_tracker):
        """Repeated identical scores should make EMA converge to that value."""
        for _ in range(200):
            ema = score_tracker.update("hk_a", 0.75)
        assert ema == pytest.approx(0.75, abs=1e-4)

    def test_score_of_zero_decays_ema(self, score_tracker):
        """Feeding score=0 should shrink the EMA toward 0."""
        score_tracker.update("hk_a", 1.0)  # EMA = 0.1
        ema = score_tracker.update("hk_a", 0.0)
        assert ema == pytest.approx(0.9 * 0.1)
        assert ema < 0.1

    def test_last_raw_scores_set(self, score_tracker):
        """update() must record the raw score for reporting."""
        score_tracker.update("hk_a", 0.77)
        assert score_tracker.last_raw_scores["hk_a"] == pytest.approx(0.77)
        score_tracker.update("hk_a", 0.55)
        assert score_tracker.last_raw_scores["hk_a"] == pytest.approx(0.55)


# =========================================================================
# TestRecordRound
# =========================================================================

class TestRecordRound:
    """Round recording, decay, dedup, and window trimming."""

    def test_single_round_participation(self, score_tracker):
        score_tracker.update("hk_a", 0.5)
        score_tracker.record_round("r1", ["hk_a"])
        assert score_tracker.get_participation_count("hk_a") == 1

    def test_duplicate_round_is_idempotent(self, score_tracker):
        score_tracker.update("hk_a", 0.5)
        score_tracker.record_round("r1", ["hk_a"])
        score_tracker.record_round("r1", ["hk_a"])  # same id
        assert score_tracker.get_participation_count("hk_a") == 1
        assert len(score_tracker.round_history) == 1

    def test_decay_applied_to_absent_miners(self, score_tracker):
        score_tracker.update("hk_a", 1.0)  # EMA = 0.1
        score_tracker.update("hk_b", 1.0)  # EMA = 0.1
        # Only hk_a scored in this round → hk_b should decay
        score_tracker.record_round("r1", ["hk_a"])
        assert score_tracker.ema_scores["hk_b"] == pytest.approx(0.1 * 0.95)
        # hk_a should be unchanged
        assert score_tracker.ema_scores["hk_a"] == pytest.approx(0.1)

    def test_miner_removed_when_ema_below_threshold(self, score_tracker):
        """Miners whose EMA decays below 1e-6 get pruned."""
        score_tracker.ema_scores["hk_ghost"] = 1e-6  # just at boundary
        score_tracker.record_round("r1", [])  # hk_ghost absent
        assert "hk_ghost" not in score_tracker.ema_scores

    def test_window_trimming_at_21_rounds(self, score_tracker):
        """After 21 rounds, only the last 20 should remain."""
        for i in range(21):
            score_tracker.update("hk_a", 0.5)
            score_tracker.record_round(f"r{i}", ["hk_a"])
        assert len(score_tracker.round_history) == PARTICIPATION_WINDOW

    def test_participation_recalculated_after_trim(self, score_tracker):
        """Miner present only in the oldest round loses that count after trim."""
        # hk_b scored only in round 0
        score_tracker.update("hk_b", 0.5)
        score_tracker.update("hk_a", 0.5)
        score_tracker.record_round("r0", ["hk_a", "hk_b"])
        # Rounds 1..20 only have hk_a
        for i in range(1, 21):
            score_tracker.update("hk_a", 0.5)
            score_tracker.record_round(f"r{i}", ["hk_a"])
        # r0 has been trimmed; hk_b should have 0 participation
        assert score_tracker.get_participation_count("hk_b") == 0
        assert score_tracker.get_participation_count("hk_a") == PARTICIPATION_WINDOW

    def test_decay_factor_one_means_no_decay(self):
        tracker = ScoreTracker(alpha=0.1, min_rounds=10, decay_factor=1.0)
        tracker.update("hk_a", 1.0)  # EMA = 0.1
        tracker.record_round("r1", [])  # hk_a absent
        assert tracker.ema_scores["hk_a"] == pytest.approx(0.1)

    def test_empty_scored_hotkeys_decays_all(self, score_tracker):
        score_tracker.update("hk_a", 1.0)
        score_tracker.update("hk_b", 1.0)
        score_tracker.record_round("r1", [])
        assert score_tracker.ema_scores["hk_a"] == pytest.approx(0.1 * 0.95)
        assert score_tracker.ema_scores["hk_b"] == pytest.approx(0.1 * 0.95)


# =========================================================================
# TestEligibility
# =========================================================================

class TestEligibility:
    """Participation-based eligibility gating (min_rounds=10)."""

    def test_below_threshold(self, score_tracker):
        for i in range(9):
            score_tracker.update("hk_a", 0.5)
            score_tracker.record_round(f"r{i}", ["hk_a"])
        assert not score_tracker.is_eligible("hk_a")

    def test_at_threshold(self, score_tracker):
        for i in range(10):
            score_tracker.update("hk_a", 0.5)
            score_tracker.record_round(f"r{i}", ["hk_a"])
        assert score_tracker.is_eligible("hk_a")

    def test_above_threshold(self, score_tracker):
        for i in range(15):
            score_tracker.update("hk_a", 0.5)
            score_tracker.record_round(f"r{i}", ["hk_a"])
        assert score_tracker.is_eligible("hk_a")

    def test_unknown_hotkey(self, score_tracker):
        assert score_tracker.get_participation_count("hk_unknown") == 0
        assert not score_tracker.is_eligible("hk_unknown")


# =========================================================================
# TestWinnerTakesAllNormal
# =========================================================================

class TestWinnerTakesAllNormal:
    """Normal mode: at least one eligible miner exists."""

    def test_single_eligible_miner(self, score_tracker):
        _make_eligible(score_tracker, "hk_a", score=0.80)
        weights = score_tracker.get_winner_takes_all_weights(["hk_a"])
        assert weights["hk_a"] == pytest.approx(1.0)

    def test_higher_ema_wins(self, score_tracker):
        _make_eligible_group(score_tracker, {"hk_a": 0.90, "hk_b": 0.70})
        weights = score_tracker.get_winner_takes_all_weights(["hk_a", "hk_b"])
        assert weights["hk_a"] == pytest.approx(1.0)
        assert weights["hk_b"] == pytest.approx(0.0)

    def test_tie_ema_earlier_submission_wins(self, score_tracker):
        _make_eligible_group(score_tracker, {"hk_a": 0.80, "hk_b": 0.80})
        times = {"hk_a": 1000.0, "hk_b": 999.0}  # hk_b submitted earlier
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b"], submission_times=times
        )
        assert weights["hk_b"] == pytest.approx(1.0)
        assert weights["hk_a"] == pytest.approx(0.0)

    def test_all_eligible_zero_ema_returns_all_zero(self, score_tracker):
        """Fail-closed: if every eligible miner has EMA=0, all weights are 0."""
        # Manually make eligible without actual scoring (force 0 EMA)
        for i in range(10):
            score_tracker.record_round(f"r{i}", ["hk_a", "hk_b"])
        # Participation is now 10, but no update() was ever called → EMA=0
        weights = score_tracker.get_winner_takes_all_weights(["hk_a", "hk_b"])
        assert weights["hk_a"] == pytest.approx(0.0)
        assert weights["hk_b"] == pytest.approx(0.0)

    def test_winner_gets_one_others_zero(self, score_tracker):
        _make_eligible_group(
            score_tracker, {"hk_a": 0.90, "hk_b": 0.70, "hk_c": 0.50}
        )
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b", "hk_c"]
        )
        assert weights["hk_a"] == pytest.approx(1.0)
        assert weights["hk_b"] == pytest.approx(0.0)
        assert weights["hk_c"] == pytest.approx(0.0)

    def test_non_eligible_miners_always_zero(self, score_tracker):
        _make_eligible(score_tracker, "hk_a", score=0.90)
        # hk_b has only 1 round
        _make_active(score_tracker, "hk_b", rounds=1, score=0.99)
        weights = score_tracker.get_winner_takes_all_weights(["hk_a", "hk_b"])
        assert weights["hk_a"] == pytest.approx(1.0)
        assert weights["hk_b"] == pytest.approx(0.0)

    def test_empty_miner_list(self, score_tracker):
        weights = score_tracker.get_winner_takes_all_weights([])
        assert weights == {}

    def test_no_submission_times_still_works(self, score_tracker):
        _make_eligible(score_tracker, "hk_a", score=0.80)
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a"], submission_times=None
        )
        assert weights["hk_a"] == pytest.approx(1.0)


# =========================================================================
# TestWinnerTakesAllWarmup
# =========================================================================

class TestWinnerTakesAllWarmup:
    """Warmup mode: no miner has reached eligibility yet."""

    def test_one_active_gets_full_weight(self, score_tracker):
        """1 active miner → renormalized [0.5] = [1.0]."""
        _make_active(score_tracker, "hk_a", rounds=1, score=0.80)
        weights = score_tracker.get_winner_takes_all_weights(["hk_a"])
        assert weights["hk_a"] == pytest.approx(1.0)

    def test_two_active_renormalized_split(self, score_tracker):
        """2 active miners → renormalized [0.5, 0.3] = [0.625, 0.375]."""
        _make_active(score_tracker, "hk_a", rounds=1, score=0.90)
        _make_active(score_tracker, "hk_b", rounds=1, score=0.70)
        weights = score_tracker.get_winner_takes_all_weights(["hk_a", "hk_b"])
        assert weights["hk_a"] == pytest.approx(0.5 / 0.8)  # 0.625
        assert weights["hk_b"] == pytest.approx(0.3 / 0.8)  # 0.375

    def test_three_active_exact_warmup_weights(self, score_tracker):
        """3 active miners → 50/30/20 split."""
        _make_active(score_tracker, "hk_a", rounds=1, score=0.90)
        _make_active(score_tracker, "hk_b", rounds=1, score=0.70)
        _make_active(score_tracker, "hk_c", rounds=1, score=0.50)
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b", "hk_c"]
        )
        assert weights["hk_a"] == pytest.approx(0.50)
        assert weights["hk_b"] == pytest.approx(0.30)
        assert weights["hk_c"] == pytest.approx(0.20)

    def test_no_active_miners_all_zero(self, score_tracker):
        """No one has participated → all weights zero."""
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b"]
        )
        assert weights["hk_a"] == pytest.approx(0.0)
        assert weights["hk_b"] == pytest.approx(0.0)

    def test_all_active_zero_ema_equal_split(self, score_tracker):
        """All active miners have zero EMA → equal split among active."""
        # Make active via record_round but never call update()
        score_tracker.record_round("r0", ["hk_a", "hk_b"])
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b"]
        )
        assert weights["hk_a"] == pytest.approx(0.5)
        assert weights["hk_b"] == pytest.approx(0.5)

    def test_tiebreak_by_submission_time_in_warmup(self, score_tracker):
        """Within SCORE_EPSILON, earlier submission wins higher position."""
        # Both get EMA within epsilon of each other
        _make_active(score_tracker, "hk_a", rounds=1, score=0.80)
        _make_active(score_tracker, "hk_b", rounds=1, score=0.80)
        times = {"hk_a": 500.0, "hk_b": 100.0}  # hk_b is earlier
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_b"], submission_times=times
        )
        # hk_b should rank first (earlier time), hk_a second
        assert weights["hk_b"] > weights["hk_a"]
        # Renormalized [0.5, 0.3] → 0.625, 0.375
        assert weights["hk_b"] == pytest.approx(0.5 / 0.8)
        assert weights["hk_a"] == pytest.approx(0.3 / 0.8)

    def test_inactive_miners_get_zero_in_warmup(self, score_tracker):
        """Miners with 0 participation get 0 even in warmup."""
        _make_active(score_tracker, "hk_a", rounds=1, score=0.80)
        weights = score_tracker.get_winner_takes_all_weights(
            ["hk_a", "hk_inactive"]
        )
        assert weights["hk_a"] == pytest.approx(1.0)
        assert weights["hk_inactive"] == pytest.approx(0.0)


# =========================================================================
# TestRecoverFromPlatformState
# =========================================================================

class TestRecoverFromPlatformState:
    """State recovery from platform DB."""

    def test_empty_state(self, score_tracker):
        score_tracker.recover_from_platform_state([], [])
        assert score_tracker.ema_scores == {}
        assert score_tracker.round_history == []

    def test_recovery_restores_ema_and_participation(self, score_tracker):
        ema_entries = [
            {"miner_hotkey": "hk_a", "ema_score": 0.55, "participation_count": 5, "eligible": False},
            {"miner_hotkey": "hk_b", "ema_score": 0.30, "participation_count": 3, "eligible": False},
        ]
        round_history = [
            {"round_id": f"r{i}", "scored_hotkeys": ["hk_a"]}
            for i in range(5)
        ] + [
            {"round_id": f"r{i}", "scored_hotkeys": ["hk_a", "hk_b"]}
            for i in range(5, 8)
        ]
        score_tracker.recover_from_platform_state(ema_entries, round_history)
        assert score_tracker.ema_scores["hk_a"] == pytest.approx(0.55)
        assert score_tracker.ema_scores["hk_b"] == pytest.approx(0.30)
        assert score_tracker.get_participation_count("hk_a") == 8
        assert score_tracker.get_participation_count("hk_b") == 3

    def test_recovery_trims_to_window(self, score_tracker):
        """History exceeding PARTICIPATION_WINDOW gets trimmed on recovery."""
        ema_entries = [{"miner_hotkey": "hk_a", "ema_score": 0.5}]
        round_history = [
            {"round_id": f"r{i}", "scored_hotkeys": ["hk_a"]}
            for i in range(25)
        ]
        score_tracker.recover_from_platform_state(ema_entries, round_history)
        assert len(score_tracker.round_history) == PARTICIPATION_WINDOW
        # Only the last 20 rounds survive (r5..r24)
        assert score_tracker.round_history[0]["round_id"] == "r5"
        assert score_tracker.get_participation_count("hk_a") == PARTICIPATION_WINDOW


# =========================================================================
# TestRankingsAndHistory
# =========================================================================

class TestRankingsAndHistory:
    """get_rankings and build_weight_history."""

    def test_get_rankings(self, score_tracker):
        _make_eligible_group(score_tracker, {"hk_a": 0.90, "hk_b": 0.70})
        # hk_c is NOT eligible (only 1 round)
        _make_active(score_tracker, "hk_c", rounds=1, score=0.99)

        rankings = score_tracker.get_rankings(["hk_a", "hk_b", "hk_c"])
        assert rankings["hk_a"] == 1
        assert rankings["hk_b"] == 2
        assert rankings["hk_c"] is None  # ineligible

    def test_build_weight_history_structure(self, score_tracker_low_threshold):
        tracker = score_tracker_low_threshold
        _make_eligible_group(tracker, {"hk_a": 0.90, "hk_b": 0.70})

        weights = tracker.get_winner_takes_all_weights(["hk_a", "hk_b"])
        entries = tracker.build_weight_history(
            round_id="r_final",
            validator_hotkey="val_hk",
            miner_hotkeys=["hk_a", "hk_b"],
            weights=weights,
        )

        assert len(entries) == 2
        keys = {"miner_hotkey", "raw_score", "ema_score", "rank",
                "weight", "eligible", "participation_count"}
        for entry in entries:
            assert set(entry.keys()) == keys

        # Winner entry
        winner = [e for e in entries if e["miner_hotkey"] == "hk_a"][0]
        assert winner["weight"] == pytest.approx(1.0)
        assert winner["eligible"] is True
        assert winner["rank"] == 1

        loser = [e for e in entries if e["miner_hotkey"] == "hk_b"][0]
        assert loser["weight"] == pytest.approx(0.0)
        assert loser["eligible"] is True
        assert loser["rank"] == 2
