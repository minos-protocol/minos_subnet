"""Platform deadlines must parse identically on every validator.

Same class of bug as parse_submitted_at, one layer up. datetime.fromisoformat
accepts a trailing Z only from Python 3.11 while install.sh still accepts a
python3.10 interpreter, and each deadline site spelled the Z workaround
differently or not at all. A naive deadline was worse than inconsistent: one site
compared it against a local-time now (19800s of skew on a +05:30 host, from
identical data), another against an aware now, which raised TypeError. Two honest
validators reading the same assignment therefore disagreed about how much of the
scoring window was left.
"""

import datetime as dt

import pytest

from utils.subset_scoring import seconds_until_deadline, should_stop_secondary_scoring
from utils.weight_tracking import parse_deadline


REFERENCE = dt.datetime(2026, 2, 15, 12, 0, tzinfo=dt.timezone.utc)


class TestParseDeadline:
    def test_z_suffix_parses_on_every_supported_python(self):
        """Bare fromisoformat raises on this before 3.11, and install.sh will
        still take a python3.10 interpreter."""
        assert parse_deadline("2026-02-15T12:00:00Z") == REFERENCE

    def test_utc_offset_parses(self):
        assert parse_deadline("2026-02-15T12:00:00+00:00") == REFERENCE

    def test_naive_is_read_as_utc_not_local_time(self):
        """The whole point: the result must not depend on the host's TZ."""
        parsed = parse_deadline("2026-02-15T12:00:00")
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == dt.timedelta(0)
        assert parsed == REFERENCE
        # Explicitly not the local-time reading, on any host that is not UTC.
        local = dt.datetime(2026, 2, 15, 12, 0).astimezone()
        if local.utcoffset() != dt.timedelta(0):
            assert parsed != local

    def test_all_three_encodings_agree(self):
        z = parse_deadline("2026-02-15T12:00:00Z")
        off = parse_deadline("2026-02-15T12:00:00+00:00")
        naive = parse_deadline("2026-02-15T12:00:00")
        assert z == off == naive

    def test_non_utc_offset_is_honoured_not_stripped(self):
        """A +05:30 deadline is 06:30Z, not 12:00Z — dropping the offset would
        hand the validator 5.5 extra hours of window it does not have."""
        parsed = parse_deadline("2026-02-15T12:00:00+05:30")
        assert parsed == dt.datetime(2026, 2, 15, 6, 30, tzinfo=dt.timezone.utc)
        assert parsed != REFERENCE

    def test_datetime_input_passes_through_and_is_made_aware(self):
        assert parse_deadline(REFERENCE) is REFERENCE
        assert parse_deadline(dt.datetime(2026, 2, 15, 12, 0)) == REFERENCE

    @pytest.mark.parametrize("absent", [None, ""])
    def test_absent_deadline_is_none(self, absent):
        assert parse_deadline(absent) is None

    @pytest.mark.parametrize(
        "bad", ["not a date", "2026-13-45T99:99:99Z", "2026-02-15T12:00:00ZZ", 12345.0, {}]
    )
    def test_garbage_raises_rather_than_looking_like_no_deadline(self, bad):
        """None means "no deadline" and the guards fail open on it, so a
        malformed deadline must not be folded into None — the round would run
        unguarded with nothing in the log."""
        with pytest.raises(ValueError):
            parse_deadline(bad)


class TestDeadlineArithmetic:
    """Downstream of the parse: a naive deadline used to raise TypeError here,
    because `datetime.now(scoring_end_time.tzinfo or tz)` returned an aware now
    while the deadline itself stayed naive."""

    def test_naive_deadline_does_not_raise(self):
        naive = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=30)
        assert seconds_until_deadline(naive) > 0

    def test_naive_and_utc_encodings_give_the_same_remaining_time(self):
        aware = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
        naive = aware.replace(tzinfo=None)
        assert abs(seconds_until_deadline(aware) - seconds_until_deadline(naive)) < 1.0

    def test_z_suffixed_deadline_reaches_the_guard_unchanged(self):
        """The string the platform actually sends, through parse to guard."""
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
        deadline = parse_deadline(soon.isoformat().replace("+00:00", "Z"))
        assert should_stop_secondary_scoring(deadline, buffer_seconds=180) is True
        assert should_stop_secondary_scoring(deadline, buffer_seconds=30) is False


class TestEveryDeadlineSiteRoutesThroughTheHelper:
    """The bug was three private parses drifting apart, so pin that there is one.
    Source-level because importing neurons.validator pulls in bittensor."""

    @pytest.mark.parametrize(
        "path", ["neurons/validator.py", "utils/subset_scoring.py"]
    )
    def test_no_bare_fromisoformat_left(self, path):
        src = open(path).read()
        assert "fromisoformat" not in src, (
            f"{path} parses a timestamp itself; route it through parse_deadline "
            f"so the Z suffix and naive-means-UTC rule stay in one place"
        )

    def test_validator_parses_both_platform_deadlines_with_it(self):
        src = open("neurons/validator.py").read()
        assert 'parse_deadline(assignment.get("scoring_deadline"))' in src
        assert "parse_deadline(next_scoring_window)" in src
