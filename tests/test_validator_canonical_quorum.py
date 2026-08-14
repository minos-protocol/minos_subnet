"""Regression tests for the canonical-tiebreak quorum gates (audit finding F-7).

Close rounds (local rank-1 gap <= CANONICAL_TIEBREAK_TOLERANCE) defer winner
selection to the platform's stake-weighted canonical ranking. The old gate
accepted that ranking with a quorum of only 2 validators holding
CANONICAL_MIN_VALIDATOR_STAKE each, and it never compared the ranking's
stake coverage against the subnet's active validator stake. Because the rank-1
miner receives ~90% of the round's miner budget, two colluding validators with
sufficient stake misreporting scores for one miner could steer the canonical
winner of any close round, and pushing a score within 0.001 of rank 1 was
enough to trigger platform arbitration.

The fix raises the default quorum to 3 validators and requires the canonical
ranking to represent a stake-weighted supermajority
(CANONICAL_STAKE_SUPERMAJORITY, default two-thirds) of the active validator
stake the validator sees in its own metagraph before the canonical ranking may
override a local close-call winner. Coverage shortfalls fail closed: the
validator skips the weight submission instead of selecting a low-quorum
winner. Winner/dust curve smoothing and publishing per-validator score inputs
behind the canonical ranking are platform-side policy (network-config and
server disclosure), not validator-code changes.
"""

import asyncio
import importlib
import sys
import types

from utils import weight_tracking
from utils.weight_tracking import (
    MIN_PARTICIPATION_ROUNDS,
    DEFAULT_BURN_RATE,
    DEFAULT_WINNER_WEIGHT,
    CANONICAL_MIN_VALIDATOR_COUNT,
)


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


def _network_config(**extra):
    """Complete network reward policy; tests append canonical knobs via extra."""
    cfg = {
        "burn_rate": DEFAULT_BURN_RATE,
        "burn_uid": 0,
        "winner_weight": DEFAULT_WINNER_WEIGHT,
        "dust_top_n": 20,
        "dust_decay": 0.8,
    }
    cfg.update(extra)
    return cfg


def _canonical_response(total_stake_considered, validator_count=3, entry_validator_count=None):
    """Round-pinned canonical ranking payload favoring hk_b over local rank-1 hk_a."""
    entry_count = validator_count if entry_validator_count is None else entry_validator_count
    return {
        "round_id": "round_f7",
        "validator_count": validator_count,
        "total_stake_considered": total_stake_considered,
        "ranking": [
            {"miner_hotkey": "hk_b", "validator_count": entry_count},
            {"miner_hotkey": "hk_a", "validator_count": entry_count},
        ],
    }


class _StubPlatformClient:
    def __init__(self, network_cfg, canonical_response):
        self._network_cfg = network_cfg
        self._canonical_response = canonical_response
        self.weight_history_calls = []

    async def get_network_config(self):
        return self._network_cfg

    async def get_canonical_ranking(self, round_id=None, top_n=10):
        return self._canonical_response

    async def submit_weight_history(self, round_id, validator_hotkey, entries):
        self.weight_history_calls.append({"round_id": round_id, "entries": entries})


# Metagraph with 3 permit validators holding 10k TAO each (30k active
# validator stake) plus miner uids holding stake that must NOT count toward
# the supermajority denominator.
_METAGRAPH = dict(
    hotkeys=["hk_burn", "hk_a", "hk_b", "hk_v1", "hk_v2", "hk_v3"],
    validator_permit=[False, False, False, True, True, True],
    S=[0.0, 5000.0, 5000.0, 10000.0, 10000.0, 10000.0],
)


def _run_set_weights(validator_module, canonical_response, network_cfg):
    """Drive _set_weights_after_round over a close-call round (0.7000 vs 0.6995).

    The 0.0005 gap is inside CANONICAL_TIEBREAK_TOLERANCE, so the canonical
    ranking is fetched and its hk_b top entry would override local rank-1 hk_a
    if the coverage gates allow it.
    """
    tracker = validator_module.ScoreTracker()
    tracker.recover_from_platform_state(
        [],
        [
            {"round_id": f"seed_{idx}", "scored_hotkeys": ["hk_a", "hk_b"]}
            for idx in range(MIN_PARTICIPATION_ROUNDS)
        ],
    )
    tracker.update("hk_a", 0.7000)
    tracker.update("hk_b", 0.6995)

    client = _StubPlatformClient(network_cfg, canonical_response)
    validator = types.SimpleNamespace(
        score_tracker=tracker,
        platform_client=client,
        metagraph=types.SimpleNamespace(**_METAGRAPH),
        my_subnet_uid=None,
        wallet=types.SimpleNamespace(
            hotkey=types.SimpleNamespace(ss58_address="hk_self_validator")
        ),
        is_registered=False,
    )
    result = asyncio.run(
        validator_module.Validator._set_weights_after_round(validator, round_id="round_f7")
    )
    return result, client


def test_canonical_quorum_default_is_three():
    # F-7: a two-validator quorum lets a colluding pair BE the entire
    # tiebreak electorate; the default must require at least three.
    assert CANONICAL_MIN_VALIDATOR_COUNT == 3


def test_canonical_stake_supermajority_is_a_strict_majority():
    # F-7: canonical override must require MORE than half of the active
    # validator stake, so no small stake set can arbitrate close rounds alone.
    assert hasattr(weight_tracking, "CANONICAL_STAKE_SUPERMAJORITY")
    assert 0.5 < weight_tracking.CANONICAL_STAKE_SUPERMAJORITY <= 1.0


def test_close_call_override_blocked_below_stake_supermajority(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # Quorum (3 validators) and the per-validator stake floor (3 x 5000 TAO)
    # both pass at 15000 TAO, but that is below the two-thirds supermajority
    # of the 30000 TAO active validator stake in the metagraph.
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(total_stake_considered=15000.0),
        _network_config(),
    )

    assert result is False
    assert client.weight_history_calls == []

    sys.modules.pop("neurons.validator", None)


def test_nan_stake_total_fails_closed(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # NaN compares False against every bound, so without an explicit
    # finiteness check it would slip through both stake gates.
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(total_stake_considered=float("nan")),
        _network_config(),
    )

    assert result is False
    assert client.weight_history_calls == []

    sys.modules.pop("neurons.validator", None)


def test_close_call_override_applied_at_stake_supermajority(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # 24000 TAO >= two-thirds of 30000 TAO active validator stake: the
    # canonical override is legitimate and must still apply (fail-closed must
    # not over-block). The denominator is permit-validator stake only: miner
    # uids hold 10000 TAO extra that must not count.
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(total_stake_considered=24000.0),
        _network_config(),
    )

    assert result is True
    assert len(client.weight_history_calls) == 1
    entries = client.weight_history_calls[0]["entries"]
    by_hotkey = {entry["miner_hotkey"]: entry for entry in entries}
    # Winner weight is assigned exactly, not approximated (numpy is stubbed
    # in these tests, so no pytest.approx numpy probing). The canonical
    # override redirects the winner WEIGHT; the reported rank stays the local
    # score ranking (hk_a first), which this assertion pins down.
    assert by_hotkey["hk_b"]["weight"] == DEFAULT_WINNER_WEIGHT
    assert by_hotkey["hk_b"]["rank"] == 2

    sys.modules.pop("neurons.validator", None)


def test_old_two_validator_quorum_is_no_longer_sufficient(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # Two validators with ample stake were the entire electorate under the old
    # default quorum; the raised quorum must reject the ranking outright.
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(
            total_stake_considered=24000.0,
            validator_count=2,
            entry_validator_count=2,
        ),
        _network_config(),
    )

    assert result is False
    assert client.weight_history_calls == []

    sys.modules.pop("neurons.validator", None)


def test_network_config_can_raise_the_supermajority(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # 24000 TAO clears the default two-thirds bar but not the stricter 90%
    # policy served by network-config (27000 TAO required).
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(total_stake_considered=24000.0),
        _network_config(canonical_stake_supermajority=0.9),
    )

    assert result is False
    assert client.weight_history_calls == []

    sys.modules.pop("neurons.validator", None)


def test_invalid_network_config_supermajority_fails_closed(monkeypatch):
    validator_module = _import_validator_with_runtime_stubs(monkeypatch)

    # An out-of-range policy value must fail closed like every other invalid
    # reward-policy field, never silently disable the gate.
    result, client = _run_set_weights(
        validator_module,
        _canonical_response(total_stake_considered=24000.0),
        _network_config(canonical_stake_supermajority=1.5),
    )

    assert result is False
    assert client.weight_history_calls == []

    sys.modules.pop("neurons.validator", None)
