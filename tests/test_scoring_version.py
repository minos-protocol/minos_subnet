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


class TestTheVersionIsKnownBeforeScoring:
    """The resolver has to be consulted BEFORE a round is scored.

    Reading it from a config cached during finalization means a fresh validator
    scores its first round on the fallback rather than on what the platform
    advertises — and that first round is submitted on a scale the rest of the
    fleet is not using.
    """

    def test_the_validator_resolves_before_it_scores(self):
        """Structural: the resolve call must precede the scorer selection."""
        import ast
        import pathlib

        src = pathlib.Path("neurons/validator.py").read_text()
        tree = ast.parse(src)

        target = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_scoring_version"
        )
        body = ast.unparse(target)
        assert "get_network_config" in body, (
            "_scoring_version reads a cache instead of asking the platform; the "
            "cache is refreshed during finalization, after this round was scored"
        )

    def test_the_scorer_label_follows_the_version_that_ran(self):
        """A v1 score labelled AdvancedV2 makes two incomparable scales
        indistinguishable to anyone reading the record."""
        import ast
        import pathlib

        src = pathlib.Path("neurons/validator.py").read_text()
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_build_advanced_metrics_payload"
        )
        body = ast.unparse(fn)
        assert "AdvancedV1" in body and "scoring_version" in body, (
            "the scorer name is hardcoded and no longer reflects what ran"
        )

    def test_scoring_version_is_a_parameter_not_a_closure(self):
        """It is used in a nested function defined in a different scope from
        where it is resolved, so it has to be passed explicitly."""
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("neurons/validator.py").read_text())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_submit_miner_score"
        )
        names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        assert "scoring_version" in names


class TestTheCacheExpiresWithTheRound:
    """The version is cached per round so a platform change cannot land
    mid-round and score some miners on one scale and the rest on another.

    The cache key has to be the round. Keying it on something that never
    changes freezes the version for the life of the process, so a platform flip
    never reaches a running validator — which is most of the point of putting
    the switch on the platform.
    """

    def test_the_resolver_takes_the_round_id_as_a_parameter(self):
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("neurons/validator.py").read_text())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_scoring_version"
        )
        names = [a.arg for a in fn.args.args]
        assert "round_id" in names, (
            "the cache key does not come from the caller, so it cannot change "
            "between rounds"
        )

    def test_it_does_not_key_on_an_attribute_nothing_assigns(self):
        """`current_round_id` was read but never set anywhere in the repo, so
        the key was always None and the version never expired."""
        import pathlib

        sources = list(pathlib.Path("neurons").rglob("*.py")) + list(
            pathlib.Path("utils").rglob("*.py")
        )
        assignments = [
            p for p in sources
            if "current_round_id =" in p.read_text() or "current_round_id=" in p.read_text()
        ]
        reads = [p for p in sources if "current_round_id" in p.read_text()]
        assert not reads or assignments, (
            f"current_round_id is read in {[p.name for p in reads]} but assigned nowhere"
        )

    def test_the_call_site_passes_the_round(self):
        import pathlib

        src = pathlib.Path("neurons/validator.py").read_text()
        assert "_scoring_version(round_id)" in src
