"""A tie is a real outcome, and someone still has to be paid for the round.

Two miners can genuinely produce the same score — that is exactly when the
canonical tiebreak runs. If an unusable canonical voided the round instead, the
miners would have done the work and the emission would simply be lost. So every
failure mode DEGRADES to the local ordering rather than aborting.

The second half matters as much: these tests execute the real parse block
rather than reading it, so the canonical cannot fall out of use while every
degrade path still behaves correctly.
"""
import pathlib
import textwrap

import pytest


def _parse_block():
    """The canonical parse block, lifted verbatim out of the validator."""
    src = pathlib.Path("neurons/validator.py").read_text()
    start = src.index("                if canonical_response is None:")
    end = src.index("                # A tie is a legitimate outcome", start)
    return textwrap.dedent(src[start:end])


def _run(response, *, min_count=2, min_stake=5000.0):
    ns = {
        "canonical_response": response,
        "canonical_ranking": [],
        "canonical_parse_failed": False,
        "canonical_low_coverage": False,
        "canonical_min_validator_count": min_count,
        "canonical_min_validator_stake": min_stake,
    }
    exec(_parse_block(), {"isinstance": isinstance}, ns)
    return ns


def _resp(*hotkeys, validators=5, stake=400000.0):
    return {
        "ranking": [{"miner_hotkey": h, "validator_count": 5} for h in hotkeys],
        "validator_count": validators,
        "total_stake_considered": stake,
    }


class TestTheRankingIsActuallyParsed:
    def test_a_healthy_response_yields_the_ranking_in_order(self):
        assert _run(_resp("5B", "5A"))["canonical_ranking"] == ["5B", "5A"]

    def test_surrounding_whitespace_is_trimmed(self):
        """Without changing ss58 casing."""
        assert _run(_resp(" 5B ", "5A"))["canonical_ranking"] == ["5B", "5A"]

    def test_an_entry_below_quorum_is_skipped_not_fatal(self):
        r = _run({
            "ranking": [{"miner_hotkey": "5A", "validator_count": 5},
                        {"miner_hotkey": "5Low", "validator_count": 1}],
            "validator_count": 5, "total_stake_considered": 400000.0,
        })
        assert r["canonical_ranking"] == ["5A"]
        assert r["canonical_parse_failed"] is False


class TestEveryFailureDegrades:
    """None of these may abort the round — the tie is still real."""

    @pytest.mark.parametrize("response,flag", [
        (None,                                   None),            # unreachable
        ("garbage",                              "parse_failed"),  # not a dict
        ({"ranking": "nope", "validator_count": 5,
          "total_stake_considered": 400000.0},   "parse_failed"),  # not a list
        ({"ranking": [{"miner_hotkey": 1, "validator_count": 5}],
          "validator_count": 5,
          "total_stake_considered": 400000.0},   "parse_failed"),  # bad hotkey
        ({"ranking": [], "validator_count": 5,
          "total_stake_considered": 400000.0},   "low_coverage"),  # empty
    ])
    def test_it_sets_a_degrade_flag_and_ranks_nobody(self, response, flag):
        r = _run(response)
        assert r["canonical_ranking"] == []
        if flag:
            assert r[f"canonical_{flag}"] is True

    @pytest.mark.parametrize("kw", [
        {"validators": 1},        # below the validator quorum
        {"stake": 10.0},          # below the stake floor
    ])
    def test_insufficient_coverage_reads_as_low_coverage(self, kw):
        r = _run(_resp("5A", **kw))
        assert r["canonical_low_coverage"] is True
        assert r["canonical_ranking"] == []

    def test_every_entry_below_quorum_is_low_coverage_not_a_parse_failure(self):
        """Parsing nothing is not the same as a malformed response."""
        r = _run({
            "ranking": [{"miner_hotkey": "5A", "validator_count": 1}],
            "validator_count": 5, "total_stake_considered": 400000.0,
        })
        assert r["canonical_low_coverage"] is True
        assert r["canonical_parse_failed"] is False


class TestTheRoundIsNeverVoided:
    def test_the_tiebreak_block_never_returns_false(self):
        """The one structural check worth keeping: a `return False` anywhere in
        this block would mean a tied round pays nobody."""
        src = pathlib.Path("neurons/validator.py").read_text()
        i = src.index("canonical_needed = self.score_tracker.needs_canonical_tiebreak")
        block = src[i:src.index("dust_top_n=dust_top_n", i)]
        assert "return False" not in block


class TestTheLocalFallbackIsDeterministic:
    """Falling back is only safe because every validator applies the same rule
    to the same submission times, with a unique last-resort key."""

    def test_ordering_is_score_then_time_then_hotkey(self):
        from utils.weight_tracking import ScoreTracker

        tr = ScoreTracker()
        for hk in ("hkC", "hkA", "hkB"):
            tr.round_scores[hk] = 0.9          # exact tie
        times = {"hkA": 100.0, "hkB": 100.0, "hkC": 50.0}
        order = tr._sort_by_round_score(list(tr.round_scores), times)
        assert order[0] == "hkC", "earlier submission must win a tied score"
        assert order[1:] == ["hkA", "hkB"], "hotkey breaks a fully equal tie"
