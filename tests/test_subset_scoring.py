"""Tests for subset-scoring helpers (deadline gate and per-job budget)."""

from datetime import datetime, timedelta, timezone

from utils.subset_scoring import (
    per_job_wall_clock_budget,
    should_stop_secondary_scoring,
)


def _deadline(seconds_from_now: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)


class TestShouldStopSecondaryScoring:
    def test_no_deadline_continues(self):
        assert should_stop_secondary_scoring(None) is False

    def test_far_deadline_continues(self):
        assert should_stop_secondary_scoring(_deadline(3600)) is False

    def test_near_deadline_stops(self):
        assert should_stop_secondary_scoring(_deadline(60), buffer_seconds=180) is True


class TestPerJobWallClockBudget:
    def test_no_deadline_returns_full_timeout(self):
        budget = per_job_wall_clock_budget(
            None, num_jobs=10, concurrency=2, max_job_seconds=3600
        )
        assert budget == 3600

    def test_plenty_of_time_returns_full_timeout(self):
        # 2h left for 1 batch of 2 jobs: (7200 - 180) / 1 > cap -> capped
        budget = per_job_wall_clock_budget(
            _deadline(7200), num_jobs=2, concurrency=2, max_job_seconds=3600
        )
        assert budget == 3600

    def test_pressure_splits_remaining_time_across_batches(self):
        # 40 min left, 4 jobs at concurrency 2 -> 2 batches of ~(2400-180)/2
        budget = per_job_wall_clock_budget(
            _deadline(2400), num_jobs=4, concurrency=2, max_job_seconds=3600
        )
        assert 1100 <= budget <= 1110

    def test_tight_deadline_floored_at_minimum(self):
        # 20 min left, 8 jobs at concurrency 2 -> 4 batches of ~(1200-180)/4,
        # which is below the floor
        budget = per_job_wall_clock_budget(
            _deadline(1200), num_jobs=8, concurrency=2, max_job_seconds=3600
        )
        assert budget == 300

    def test_past_deadline_returns_floor(self):
        budget = per_job_wall_clock_budget(
            _deadline(-60), num_jobs=4, concurrency=2, max_job_seconds=3600
        )
        assert budget == 300

    def test_zero_jobs_treated_as_one(self):
        budget = per_job_wall_clock_budget(
            None, num_jobs=0, concurrency=2, max_job_seconds=3600
        )
        assert budget == 3600

    def test_zero_concurrency_treated_as_one(self):
        # 1 worker -> 4 batches of ~(2400-180)/4
        budget = per_job_wall_clock_budget(
            _deadline(2400), num_jobs=4, concurrency=0, max_job_seconds=3600
        )
        assert 550 <= budget <= 555
