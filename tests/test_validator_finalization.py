"""Validator round finalization: a round that finished with no valid scores
must clear tracker state rather than carry the previous round's scores into
weight setting."""

import asyncio
import importlib
import sys
import types


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


def test_finalize_round_with_no_valid_scores_does_not_reuse_previous_round(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)
    tracker = validator_module.ScoreTracker()

    tracker.update("hk_previous", 0.91)
    tracker.record_round("round_previous", ["hk_previous"])
    assert tracker.round_scores == {"hk_previous": 0.91}

    set_weights_called = False

    async def set_weights_after_round(*args, **kwargs):
        nonlocal set_weights_called
        set_weights_called = True
        return True

    validator = types.SimpleNamespace(
        platform_client=None,
        score_tracker=tracker,
        _set_weights_after_round=set_weights_after_round,
    )

    result = asyncio.run(
        validator_module.Validator._finalize_round_scores(
            validator,
            round_id="round_without_scores",
            scored_hotkeys=[],
            submission_times={},
            scoring_deadline=None,
        )
    )

    assert result is False
    assert not set_weights_called
    assert tracker.round_scores == {}
    assert tracker.last_raw_scores == {}

    sys.modules.pop("neurons.validator", None)
