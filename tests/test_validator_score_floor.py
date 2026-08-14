"""Regression tests for the validator round-score floor (audit finding F-6).

A zero-input or near-empty junk VCF earns no F1 credit but still collects
baseline AdvancedScorer component credit (coverage, FP-size term, ratio
defaults) and lands around 0.17-0.25 combined_final. The old gate only
rejected the EXACT zero-input fingerprint (both F1s zero AND combined_final
within [0.24999, 0.25001]), so adding a couple of off-target junk FP calls
moved combined_final just outside the band, evaded the fingerprint, and the
positive score was banked: any score > 0 feeds ScoreTracker.record_round and
the 5-of-20 eligibility gate, keeping junk hotkeys perpetually eligible for
winner/dust selection without ever running a real pipeline.

Every intake path (local scoring, platform submission, restart recovery,
peer backfill) funnels through _valid_round_score before
ScoreTracker.update, so the floor lives there: anything below
MIN_VALID_ROUND_SCORE is "no valid score" and earns no participation credit.
"""

import asyncio
import importlib
import sys
import types

from utils.scoring import AdvancedScorer


def _noop(*args, **kwargs):
    return None


def _import_validator_with_runtime_stubs(monkeypatch):
    """Import neurons.validator without requiring a live Bittensor runtime."""
    logging_stub = types.SimpleNamespace(
        debug=_noop,
        error=_noop,
        info=_noop,
        warning=_noop,
        set_debug=_noop,
        set_trace=_noop,
    )
    bittensor_stub = types.SimpleNamespace(
        Config=object,
        Subtensor=object,
        Wallet=object,
        config=lambda parser=None: types.SimpleNamespace(),
        logging=logging_stub,
        subtensor=object,
        wallet=object,
    )
    httpx_stub = types.SimpleNamespace(
        AsyncClient=object,
        TimeoutException=Exception,
        ConnectError=Exception,
        ReadError=Exception,
    )

    monkeypatch.setitem(sys.modules, "bittensor", bittensor_stub)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", types.SimpleNamespace(Keypair=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=_noop))
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    sys.modules.pop("neurons.validator", None)
    return importlib.import_module("neurons.validator")


def _junk_happy_metrics(zero_happy_metrics):
    """Zero-input metrics plus two off-target junk SNP FPs (audit sim S9).

    The exact zero-input case scores 0.25 and hit the old fingerprint; the
    junk FPs move combined_final off the fingerprint band while it stays a
    positive "participation" score.
    """
    metrics = dict(zero_happy_metrics)
    metrics["query_total_snp"] = 2
    metrics["fp_snp"] = 2
    return metrics


def test_zero_input_score_rejected(monkeypatch, zero_happy_metrics):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)
    combined_final = AdvancedScorer.compute_advanced_score(zero_happy_metrics) / 100.0

    assert validator_module._valid_round_score(
        combined_final, label="zero-input probe"
    ) is None

    sys.modules.pop("neurons.validator", None)


def test_near_empty_junk_score_rejected(monkeypatch, zero_happy_metrics):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)
    combined_final = (
        AdvancedScorer.compute_advanced_score(_junk_happy_metrics(zero_happy_metrics))
        / 100.0
    )

    # The junk score is positive (old gate counted it as participation) but
    # sits OUTSIDE the old fingerprint band [0.24999, 0.25001] — that is the
    # evasion this regression test pins down.
    assert 0.0 < combined_final
    assert not (0.24999 <= combined_final <= 0.25001)

    assert validator_module._valid_round_score(
        combined_final, label="junk probe"
    ) is None

    sys.modules.pop("neurons.validator", None)


def test_honest_scores_pass_floor(monkeypatch, sample_happy_metrics):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)
    combined_final = AdvancedScorer.compute_advanced_score(sample_happy_metrics) / 100.0

    assert combined_final > validator_module.MIN_VALID_ROUND_SCORE
    assert validator_module._valid_round_score(
        combined_final, label="honest probe"
    ) == combined_final

    # The floor rejects only strictly-below values; the boundary itself is
    # the smallest valid score.
    floor = validator_module.MIN_VALID_ROUND_SCORE
    assert validator_module._valid_round_score(floor, label="floor probe") == floor
    assert validator_module._valid_round_score(
        floor - 1e-9, label="just below floor probe"
    ) is None

    sys.modules.pop("neurons.validator", None)


def test_existing_range_rules_unchanged(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    for value in (0.0, -0.2, 1.0001, float("nan"), float("inf"), "abc", None):
        assert validator_module._valid_round_score(value, label="range probe") is None
    assert validator_module._valid_round_score(1.0, label="max probe") == 1.0

    sys.modules.pop("neurons.validator", None)


def test_backfill_junk_earns_no_participation_credit(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)
    tracker = validator_module.ScoreTracker()

    backfill_scores = [
        {
            # Audit sim S9 value: near-empty junk that evaded the old
            # fingerprint band and was banked as participation.
            "miner_hotkey": "hk_junk",
            "combined_final": 0.200004,
            "primary_validator_hotkey": "hk_peer_validator",
            "submitted_at": "2026-08-14T00:00:00Z",
        },
        {
            "miner_hotkey": "hk_honest",
            "combined_final": 0.924434,
            "primary_validator_hotkey": "hk_peer_validator",
            "submitted_at": "2026-08-14T00:00:01Z",
        },
    ]

    class _StubPlatformClient:
        async def get_network_config(self):
            return {}

        async def get_backfill_scores(self, round_id, scored_miner_hotkeys):
            return {
                "backfill_scores": backfill_scores,
                "overlap_deltas": [],
                "unscored_miner_hotkeys": [],
            }

    set_weights_called = []

    async def set_weights_after_round(*args, **kwargs):
        set_weights_called.append(True)
        return True

    validator = types.SimpleNamespace(
        platform_client=_StubPlatformClient(),
        score_tracker=tracker,
        _set_weights_after_round=set_weights_after_round,
    )

    result = asyncio.run(
        validator_module.Validator._finalize_round_scores(
            validator,
            round_id="round_f6",
            scored_hotkeys=[],
            submission_times={},
            scoring_deadline=None,
        )
    )

    assert result is True
    assert set_weights_called
    # Junk below the floor earns no score and no participation credit; the
    # honest backfill passes through unchanged.
    assert "hk_junk" not in tracker.round_scores
    assert tracker.round_scores == {"hk_honest": 0.924434}
    assert tracker.get_participation_count("hk_junk") == 0
    assert tracker.get_participation_count("hk_honest") == 1
    assert tracker.is_eligible("hk_junk") is False

    sys.modules.pop("neurons.validator", None)
