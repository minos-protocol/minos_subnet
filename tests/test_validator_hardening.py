"""Tests for three validator behaviours that decide a round's outcome.

* ``ScoreTracker.refresh_participation_window`` — rebuilds the eligibility
  window without discarding the round in progress.
* tie ordering — ``submission_times`` must reach ``_sort_by_round_score`` or
  tied miners rank arbitrarily.
* the round-file retry budget — retrying must not consume the scoring window
  the deadline guard is there to protect.
"""

import importlib.util

import pytest

from utils.weight_tracking import (
    ScoreTracker,
    PARTICIPATION_WINDOW,
    parse_submitted_at,
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRefreshParticipationWindow:
    """This is deliberately distinct from recover_from_platform_state: it
    rebuilds the eligibility window on its own, so a restart whose startup
    recovery could not reach the network still scores the round in progress."""

    def test_rebuilds_window_from_snapshot(self, score_tracker):
        ok = score_tracker.refresh_participation_window([
            {"round_id": "r1", "scored_hotkeys": ["a", "b"]},
            {"round_id": "r2", "scored_hotkeys": ["a"]},
        ])
        assert ok is True
        assert len(score_tracker.round_history) == 2
        assert score_tracker.get_participation_count("a") == 2
        assert score_tracker.get_participation_count("b") == 1

    def test_does_not_clear_the_current_round_scores(self, score_tracker):
        """recover_from_platform_state clears both; this must not."""
        score_tracker.update("a", 0.87)
        score_tracker.refresh_participation_window(
            [{"round_id": "r1", "scored_hotkeys": ["a"]}]
        )
        assert score_tracker.round_scores["a"] == pytest.approx(0.87)

    def test_empty_snapshot_keeps_the_existing_window(self, score_tracker):
        """Returning False lets the caller keep known-good state rather than
        zeroing eligibility because one platform call came back empty."""
        score_tracker.refresh_participation_window(
            [{"round_id": "r1", "scored_hotkeys": ["a"]}]
        )
        before = list(score_tracker.round_history)

        assert score_tracker.refresh_participation_window([]) is False
        assert score_tracker.refresh_participation_window(None) is False
        assert score_tracker.round_history == before

    def test_skips_malformed_entries(self, score_tracker):
        ok = score_tracker.refresh_participation_window([
            {"round_id": "good", "scored_hotkeys": ["a"]},
            {"scored_hotkeys": ["b"]},          # no round_id
            "not-a-dict",
            {"round_id": "ids", "scored_hotkeys": ["c", "", None, 7]},
        ])
        assert ok is True
        assert [e["round_id"] for e in score_tracker.round_history] == ["good", "ids"]
        assert score_tracker.get_participation_count("c") == 1
        assert score_tracker.get_participation_count("") == 0
        assert score_tracker.get_participation_count(7) == 0

    def test_truncates_to_the_participation_window(self, score_tracker):
        ok = score_tracker.refresh_participation_window([
            {"round_id": f"r{i}", "scored_hotkeys": ["a"]}
            for i in range(PARTICIPATION_WINDOW + 5)
        ])
        assert ok is True
        assert len(score_tracker.round_history) == PARTICIPATION_WINDOW
        # keeps the most recent, not the oldest
        assert score_tracker.round_history[-1]["round_id"] == f"r{PARTICIPATION_WINDOW + 4}"


class TestTieOrdering:
    """Scores tie constantly — 5-8 miners to 9+ decimal places in a typical
    round — so the ordering of tied miners decides the reported winner."""

    def _tie(self, tracker, hotkeys, score=0.6949980248603699):
        for hk in hotkeys:
            tracker.update(hk, score)
        tracker.record_round("r1", list(hotkeys))

    def test_tied_miners_rank_by_earliest_submission(self, score_tracker_low_threshold):
        t = score_tracker_low_threshold
        hotkeys = ["late", "early", "middle"]
        self._tie(t, hotkeys)
        ranked = t._sort_by_round_score(
            hotkeys, submission_times={"late": 300.0, "early": 100.0, "middle": 200.0}
        )
        assert ranked == ["early", "middle", "late"]

    def test_without_submission_times_ties_order_deterministically(
        self, score_tracker_low_threshold
    ):
        """An exact score tie with no timestamps must NOT depend on input order.

        Falling through to whatever order the caller passed would use
        per-validator round_scores insertion order, which is not shared state.
        Hotkeys are unique and identical everywhere, so they make the ordering
        total and reproducible from the same inputs.
        """
        t = score_tracker_low_threshold
        hotkeys = ["c", "a", "b"]
        self._tie(t, hotkeys)
        assert t._sort_by_round_score(hotkeys) == ["a", "b", "c"]
        assert t._sort_by_round_score(["a", "b", "c"]) == ["a", "b", "c"]
        assert t._sort_by_round_score(["b", "c", "a"]) == ["a", "b", "c"]

    def test_get_rankings_forwards_submission_times(self, score_tracker_low_threshold):
        """get_rankings must receive submission_times, or the rank reported to
        the platform can contradict the winner actually chosen."""
        t = score_tracker_low_threshold
        hotkeys = ["late", "early"]
        self._tie(t, hotkeys)
        for _ in range(3):
            t.record_round(f"seed{_}", hotkeys)

        rankings = t.get_rankings(hotkeys, submission_times={"late": 500.0, "early": 1.0})
        assert rankings["early"] == 1
        assert rankings["late"] == 2

    def test_build_weight_history_forwards_submission_times(
        self, score_tracker_low_threshold
    ):
        t = score_tracker_low_threshold
        hotkeys = ["late", "early"]
        self._tie(t, hotkeys)
        for _ in range(3):
            t.record_round(f"seed{_}", hotkeys)

        entries = t.build_weight_history(
            "r1", "validator", hotkeys, {"late": 0.0, "early": 0.9},
            submission_times={"late": 500.0, "early": 1.0},
        )
        by_hk = {e["miner_hotkey"]: e for e in entries}
        assert by_hk["early"]["rank"] == 1
        assert by_hk["late"]["rank"] == 2

    def test_a_clearly_higher_score_still_beats_an_earlier_submission(
        self, score_tracker_low_threshold
    ):
        """Submission time is a tiebreak, never an override."""
        t = score_tracker_low_threshold
        t.update("early_but_worse", 0.10)
        t.update("late_but_better", 0.90)
        t.record_round("r1", ["early_but_worse", "late_but_better"])
        ranked = t._sort_by_round_score(
            ["early_but_worse", "late_but_better"],
            submission_times={"early_but_worse": 1.0, "late_but_better": 999.0},
        )
        assert ranked == ["late_but_better", "early_but_worse"]


class TestRoundFileRetryBudget:
    """process_round retries missing round files 10x at 2 min (~20 min). On a
    72 min scoring window that must not run past the point where scoring is
    still possible — the deadline guards run AFTER the download, so unguarded
    the retry loop causes the very overrun they exist to prevent."""

    RETRY_BUFFER_SECONDS = 900  # must match the call in neurons/validator.py

    @pytest.fixture
    def subset(self):
        return _load("subset_scoring", "utils/subset_scoring.py")

    def test_fallback_mode_none_deadline_never_abandons(self, subset):
        """No assignment means no deadline; retries must behave as before."""
        assert subset.should_stop_secondary_scoring(
            None, buffer_seconds=self.RETRY_BUFFER_SECONDS
        ) is False

    @pytest.mark.parametrize(
        "minutes_left,expect_abandon",
        [(60, False), (20, False), (16, False), (14, True), (5, True), (-5, True)],
    )
    def test_abandons_only_inside_the_buffer(self, subset, minutes_left, expect_abandon):
        from datetime import datetime, timedelta, timezone

        deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes_left)
        assert subset.should_stop_secondary_scoring(
            deadline, buffer_seconds=self.RETRY_BUFFER_SECONDS
        ) is expect_abandon

    def test_retry_buffer_exceeds_one_retry_interval(self):
        """The buffer must be larger than the 120s sleep, or the loop can sleep
        straight through the deadline it just checked."""
        assert self.RETRY_BUFFER_SECONDS > 120

    def test_validator_uses_this_buffer(self):
        """Pin the constant to the source so the two cannot drift apart."""
        src = open("neurons/validator.py").read()
        assert (
            f"should_stop_secondary_scoring(scoring_deadline, buffer_seconds={self.RETRY_BUFFER_SECONDS})"
            in src
        )


class TestParseSubmittedAt:
    """All three call sites must parse this identically. datetime.fromisoformat
    accepts a trailing Z only from Python 3.11, while setup.py declares 3.10+
    and install.sh will select python3.10 — so a per-site parse would drop
    timestamps on some interpreters and keep them on others. Missing timestamps
    fall to inf in the tie comparator, so validators on different Python minors
    could order a tie differently from identical data."""

    def test_all_three_encodings_agree(self):
        z = parse_submitted_at("2026-02-15T12:00:00Z")
        off = parse_submitted_at("2026-02-15T12:00:00+00:00")
        naive = parse_submitted_at("2026-02-15T12:00:00")
        assert z is not None
        assert z == off == naive, "encodings must not change the ordering"

    def test_naive_is_read_as_utc_not_local_time(self):
        """Otherwise the epoch depends on the validator's TZ setting."""
        import datetime as _dt
        expected = _dt.datetime(2026, 2, 15, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
        assert parse_submitted_at("2026-02-15T12:00:00") == expected

    def test_ordering_is_preserved(self):
        early = parse_submitted_at("2026-02-15T12:00:00Z")
        late = parse_submitted_at("2026-02-15T12:30:00Z")
        assert early < late

    @pytest.mark.parametrize("bad", [None, "", "not a date", "2026-13-45T99:99:99Z", 12345.0])
    def test_unparseable_returns_none_rather_than_raising(self, bad):
        if isinstance(bad, float):
            pytest.skip("numeric input is not a documented shape")
        assert parse_submitted_at(bad) is None
