"""Which scoring formula a validator applies, and what happens when it cannot ask.

v1 and v2 are different SCALES. A fleet split across them produces a meaningless
ranking, and ranking is what pays — so the choice belongs to the platform, not
to each operator's environment.

The interesting case is the failure one. Defaulting to v1 whenever the platform
is unreachable would be actively harmful during a v2 rollout: every validator
that lost the platform for one round would drop back to v1 and diverge from the
fleet exactly when nobody is watching. So a resolved version is remembered.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from utils import scoring_version as sv  # noqa: E402


@pytest.fixture
def state(tmp_path):
    return tmp_path / "scoring_version.json"


class TestNormalising:
    @pytest.mark.parametrize("raw,expected", [
        ("v1", "v1"), ("v2", "v2"), ("V2", "v2"), ("  v2  ", "v2"),
    ])
    def test_recognised_values(self, raw, expected):
        assert sv.normalise(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "v3", "2", "latest", 2, {}, "v1 or v2"])
    def test_anything_else_is_none_not_a_default(self, raw):
        """None means 'the platform said nothing usable', which must not be
        confused with 'the platform said v1' — one keeps the current version,
        the other is an instruction to change."""
        assert sv.normalise(raw) is None


class TestThePlatformDecides:
    def test_it_follows_what_the_platform_advertises(self, state):
        assert sv.resolve({"scoring_version": "v2"}, path=state) == "v2"
        assert sv.resolve({"scoring_version": "v1"}, path=state) == "v1"

    def test_the_choice_is_remembered(self, state):
        sv.resolve({"scoring_version": "v2"}, path=state)
        assert json.loads(state.read_text())["scoring_version"] == "v2"


class TestWhenThePlatformCannotBeReached:
    """The reason this module exists."""

    def test_an_unreachable_platform_keeps_the_last_version(self, state):
        sv.resolve({"scoring_version": "v2"}, path=state)      # fleet moved to v2
        assert sv.resolve(None, path=state) == "v2", (
            "dropped back to v1 while the fleet is on v2 — divergence"
        )

    def test_a_response_missing_the_field_also_keeps_it(self, state):
        sv.resolve({"scoring_version": "v2"}, path=state)
        assert sv.resolve({"burn_rate": 0.5}, path=state) == "v2"

    def test_an_unparseable_value_keeps_it_rather_than_resetting(self, state):
        sv.resolve({"scoring_version": "v2"}, path=state)
        assert sv.resolve({"scoring_version": "garbage"}, path=state) == "v2"

    def test_a_validator_that_never_reached_the_platform_uses_v1(self, state):
        """v1 is the live formula and the safe assumption for a node that has
        never been told otherwise."""
        assert sv.resolve(None, path=state) == "v1"
        assert sv.resolve({}, path=state) == "v1"

    def test_a_corrupt_state_file_does_not_crash_scoring(self, state):
        state.write_text("{ this is not json")
        assert sv.read_last_used(state) is None
        assert sv.resolve(None, path=state) == "v1"

    def test_a_state_file_holding_a_bad_version_is_ignored(self, state):
        state.write_text(json.dumps({"scoring_version": "v9"}))
        assert sv.read_last_used(state) is None


class TestPersistence:
    def test_the_write_is_atomic(self, state):
        """A crash mid-write must not leave a truncated file, which would read
        as 'never resolved' and silently drop the validator back to v1."""
        sv.record_used("v2", state)
        leftovers = list(state.parent.glob("*.tmp"))
        assert not leftovers, f"temp files left behind: {leftovers}"
        assert json.loads(state.read_text())["scoring_version"] == "v2"

    def test_an_unwritable_location_does_not_break_scoring(self, tmp_path):
        """Losing the memory costs a restart's worth of stickiness. Raising here
        would cost the round."""
        sv.record_used("v2", tmp_path / "nope" / "deep" / "x.json")

    def test_a_bad_version_is_never_persisted(self, state):
        sv.record_used("v9", state)
        assert not state.exists()

    def test_the_state_path_is_overridable(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.json"
        monkeypatch.setenv("MINOS_SCORING_VERSION_STATE", str(target))
        sv.record_used("v2")
        assert target.exists()


class TestSwitchingVersions:
    def test_a_change_is_followed(self, state):
        assert sv.resolve({"scoring_version": "v1"}, path=state) == "v1"
        assert sv.resolve({"scoring_version": "v2"}, path=state) == "v2"
        assert sv.resolve(None, path=state) == "v2", "must stick to the NEW one"

    def test_a_change_is_announced(self, state):
        seen = []
        logger = type("L", (), {"warning": lambda self, m: seen.append(m),
                                "info": lambda self, m: None})()
        sv.resolve({"scoring_version": "v1"}, path=state, logger=logger)
        sv.resolve({"scoring_version": "v2"}, path=state, logger=logger)
        assert any("v1 -> v2" in m for m in seen), (
            "a scale change must be logged; scores before and after are not comparable"
        )


class TestTheScorerLabel:
    def test_it_follows_the_version_that_ran(self):
        """A v1 score labelled AdvancedV2 makes two incomparable scales
        indistinguishable to whoever reads the record. v1's label is
        "Advanced", the name the deployed fleet already writes, because v1 is
        that same formula."""
        assert sv.scorer_name(sv.V2) == "AdvancedV2"
        assert sv.scorer_name(sv.V1) == "Advanced"

    @pytest.mark.parametrize("unknown", ["", None, "v3", "V2 ", "advancedv2"])
    def test_anything_unresolved_reads_as_v1(self, unknown):
        """Never silently as v2: labelling a v1 number AdvancedV2 is the
        confusion the label exists to prevent."""
        assert sv.scorer_name(unknown) == "Advanced"


class TestTheRoundPinBeatsTheLiveConfig:
    """A round carries the version it is scored on. Validators resolve the live
    config at slightly different moments, so a change landing between two of
    them would put both scales in one round's ranking. The pin removes that.
    """

    def _validator(self, pinned, advertised):
        import types
        from neurons.validator import Validator

        v = object.__new__(Validator)
        v._round_pinned_version = None
        v._scoring_version_cache = None
        v.platform_client = types.SimpleNamespace(
            get_network_config=_const({"scoring_version": advertised})
        )
        v._adopt_round_pin("r1", pinned)
        return v

    @pytest.mark.parametrize("pinned,advertised,expected", [
        ("v2", "v1", "v2"),   # the pin wins
        ("v1", "v2", "v1"),   # in both directions
        (None, "v2", "v2"),   # no pin: fall back to the live config
        ("nonsense", "v1", "v1"),  # an unrecognised pin is not an instruction
    ])
    def test_the_pin_decides(self, pinned, advertised, expected):
        import asyncio
        v = self._validator(pinned, advertised)
        assert asyncio.run(v._scoring_version("r1")) == expected

    def test_a_pin_drops_a_cache_resolved_before_it_was_known(self):
        """The version can be resolved before the pin arrives. If they
        disagree the cache is wrong, so it is dropped rather than kept."""
        import asyncio
        v = self._validator(None, "v1")
        assert asyncio.run(v._scoring_version("r1")) == "v1"
        v._adopt_round_pin("r1", "v2")
        assert asyncio.run(v._scoring_version("r1")) == "v2"


def _const(value):
    async def _f(*a, **k):
        return value
    return _f
