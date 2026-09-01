"""Tests for the bittensor SDK compatibility shim (``utils.bt_compat``).

The shim imports ``bittensor`` at module scope, so every test here installs a
stub SDK into ``sys.modules`` first and reloads the shim against it. That keeps
the suite runnable without the real SDK and — more usefully — lets a single test
present the several shapes of one call that the shim has to normalize: the 10.3
form and the variants other builds return.
"""

import importlib
import sys
import types

import pytest


def _load_bt_compat(**bt_attrs):
    """Install a stub ``bittensor`` module and (re)import the shim against it."""
    stub = types.ModuleType("bittensor")
    stub.logging = types.SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None
    )
    # The shim refuses to load against an SDK missing the core methods (see
    # _assert_supported_sdk). A stub has to model a SUPPORTED SDK or every test
    # here fails at import for the wrong reason; individual tests still override
    # Subtensor when they are exercising a specific shape.
    stub.__version__ = "10.3.0"
    stub.Subtensor = type(
        "Subtensor", (), {"set_weights": lambda self: None, "metagraph": lambda self: None}
    )
    for name, value in bt_attrs.items():
        setattr(stub, name, value)
    sys.modules["bittensor"] = stub
    sys.modules.pop("utils.bt_compat", None)
    spec = importlib.util.spec_from_file_location(
        "utils.bt_compat", "utils/bt_compat.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, stub


@pytest.fixture(autouse=True)
def _restore_bittensor():
    saved = sys.modules.get("bittensor")
    yield
    if saved is not None:
        sys.modules["bittensor"] = saved
    else:
        sys.modules.pop("bittensor", None)
    sys.modules.pop("utils.bt_compat", None)


def _supported_subtensor(tag=None):
    """A Subtensor stand-in that satisfies the shim's supported-SDK check."""
    return type("Subtensor", (), {
        "set_weights": lambda self: None,
        "metagraph": lambda self: None,
        "tag": tag,
    })


class TestNamespaceAliasing:
    """v9 shipped lowercase factory aliases; v10 dropped them."""

    def test_restores_lowercase_aliases_when_only_capitalized_exist(self):
        sub, wal, cfg = _supported_subtensor("SUB"), object(), object()
        _, stub = _load_bt_compat(Subtensor=sub, Wallet=wal, Config=cfg)
        assert stub.subtensor is sub
        assert stub.wallet is wal
        assert stub.config is cfg

    def test_does_not_clobber_an_existing_lowercase_alias(self):
        original = _supported_subtensor("ORIGINAL")
        _, stub = _load_bt_compat(
            subtensor=original, Subtensor=_supported_subtensor("CAPITALIZED")
        )
        assert stub.subtensor is original


class TestCallAccepted:
    """Signature inspection, not exception catching — the module's core rule."""

    def test_drops_kwargs_the_target_does_not_declare(self):
        bt_compat, _ = _load_bt_compat()

        def target(a, b):
            return (a, b)

        # version_key is what a forward SDK removed
        assert bt_compat._call_accepted(target, a=1, b=2, version_key=99) == (1, 2)

    def test_passes_everything_through_when_target_takes_kwargs(self):
        bt_compat, _ = _load_bt_compat()

        def target(**kwargs):
            return kwargs

        assert bt_compat._call_accepted(target, a=1, zzz=2) == {"a": 1, "zzz": 2}

    def test_does_not_swallow_errors_raised_inside_the_call(self):
        """A real runtime failure must propagate, not look like a version skew."""
        bt_compat, _ = _load_bt_compat()

        def target(a):
            raise ValueError("chain unreachable")

        with pytest.raises(ValueError, match="chain unreachable"):
            bt_compat._call_accepted(target, a=1)

    def test_builtin_without_introspectable_signature_passes_through(self):
        bt_compat, _ = _load_bt_compat()
        # len() has no introspectable signature on some builds; dict() accepts kwargs
        assert bt_compat._call_accepted(dict, a=1) == {"a": 1}


class TestSetWeightsNormalization:
    """10.3 returns (success, msg); some builds return an object or a bool."""

    def _subtensor(self, result, capture=None):
        def set_weights(**kwargs):
            if capture is not None:
                capture.update(kwargs)
            return result

        return types.SimpleNamespace(set_weights=set_weights)

    def test_normalizes_10_3_tuple(self):
        bt_compat, _ = _load_bt_compat()
        ok, msg = bt_compat.set_weights(
            self._subtensor((True, "included")),
            wallet="w", netuid=1, uids=[0], weights=[1.0],
        )
        assert ok is True and msg == "included"

    def test_normalizes_object_with_success_attribute(self):
        bt_compat, _ = _load_bt_compat()
        result = types.SimpleNamespace(success=False, message="bad weights")
        ok, msg = bt_compat.set_weights(
            self._subtensor(result), wallet="w", netuid=1, uids=[0], weights=[1.0],
        )
        assert ok is False and msg == "bad weights"

    def test_normalizes_object_using_error_message_fallback(self):
        bt_compat, _ = _load_bt_compat()
        result = types.SimpleNamespace(success=False, error_message="rate limited")
        ok, msg = bt_compat.set_weights(
            self._subtensor(result), wallet="w", netuid=1, uids=[0], weights=[1.0],
        )
        assert ok is False and msg == "rate limited"

    def test_normalizes_bare_bool(self):
        bt_compat, _ = _load_bt_compat()
        ok, msg = bt_compat.set_weights(
            self._subtensor(True), wallet="w", netuid=1, uids=[0], weights=[1.0],
        )
        assert ok is True and msg == ""

    def test_drops_version_key_when_the_sdk_removed_it(self):
        bt_compat, _ = _load_bt_compat()
        captured = {}

        def set_weights(wallet, netuid, uids, weights,
                        wait_for_inclusion=True, wait_for_finalization=False):
            captured.update(locals())
            return (True, "ok")

        sub = types.SimpleNamespace(set_weights=set_weights)
        ok, _ = bt_compat.set_weights(
            sub, wallet="w", netuid=1, uids=[0], weights=[1.0], version_key=42,
        )
        assert ok is True
        assert "version_key" not in captured

    def test_real_failure_inside_set_weights_propagates(self):
        bt_compat, _ = _load_bt_compat()

        def set_weights(**kwargs):
            raise ConnectionError("subtensor down")

        sub = types.SimpleNamespace(set_weights=set_weights)
        with pytest.raises(ConnectionError):
            bt_compat.set_weights(sub, wallet="w", netuid=1, uids=[0], weights=[1.0])


class TestRegisterNormalization:
    def test_normalizes_bool(self):
        bt_compat, _ = _load_bt_compat()
        sub = types.SimpleNamespace(register=lambda **k: True)
        assert bt_compat.register(sub, wallet="w", netuid=1) is True

    def test_normalizes_extrinsic_response_object(self):
        bt_compat, _ = _load_bt_compat()
        sub = types.SimpleNamespace(
            register=lambda **k: types.SimpleNamespace(success=False)
        )
        assert bt_compat.register(sub, wallet="w", netuid=1) is False


class TestSyncMetagraph:
    """Regression tests: this helper must not swallow TypeError, and must never
    sync without a subtensor (that would hit the SDK default network)."""

    def test_passes_subtensor_through_on_the_10_3_shape(self):
        bt_compat, _ = _load_bt_compat()
        seen = {}

        class MG:
            def sync(self, subtensor=None):
                seen["subtensor"] = subtensor
                return "synced"

        assert bt_compat.sync_metagraph(MG(), "SUBTENSOR") == "synced"
        assert seen["subtensor"] == "SUBTENSOR"

    def test_type_error_raised_inside_sync_propagates(self):
        """Previously three nested handlers turned this into a silent retry."""
        bt_compat, _ = _load_bt_compat()

        class MG:
            def sync(self, subtensor=None):
                raise TypeError("unhashable type inside sync")

        with pytest.raises(TypeError, match="unhashable type inside sync"):
            bt_compat.sync_metagraph(MG(), "SUBTENSOR")

    def test_refuses_to_sync_when_signature_has_no_subtensor(self):
        """Must fail loudly rather than sync against the SDK default network."""
        bt_compat, _ = _load_bt_compat()
        called = []

        class MG:
            def sync(self):
                called.append(True)
                return "wrong-network"

        with pytest.raises(TypeError, match="default network"):
            bt_compat.sync_metagraph(MG(), "SUBTENSOR")
        assert called == [], "must not have executed the subtensor-less sync"


class TestChainReads:
    def test_current_block_prefers_10_3_getter(self):
        bt_compat, _ = _load_bt_compat()
        sub = types.SimpleNamespace(get_current_block=lambda: 123, block=999)
        assert bt_compat.current_block(sub) == 123

    def test_current_block_falls_back_to_11_property(self):
        bt_compat, _ = _load_bt_compat()
        assert bt_compat.current_block(types.SimpleNamespace(block=456)) == 456

    def test_missing_chain_reads_return_none_not_raise(self):
        """Callers already treat None as 'unknown'."""
        bt_compat, _ = _load_bt_compat()
        empty = types.SimpleNamespace()
        assert bt_compat.commit_reveal_enabled(empty, 1) is None
        assert bt_compat.blocks_since_last_update(empty, 1, 0) is None
        assert bt_compat.weights_rate_limit(empty, 1) is None


class TestCommit:
    """Publishing a commitment is best-effort by design: the chain being
    unreachable or rate-limited must never cost a miner its round, so this
    helper returns (ok, reason) instead of raising."""

    def test_uses_commit_on_10_3(self):
        bt_compat, _ = _load_bt_compat()
        seen = {}

        def commit(wallet=None, netuid=None, data=None):
            seen.update(wallet=wallet, netuid=netuid, data=data)
            return None  # several builds return None on success

        ok, _, _blk = bt_compat.commit(
            types.SimpleNamespace(commit=commit), wallet="W", netuid=107, data="payload"
        )
        assert ok is True
        assert seen == {"wallet": "W", "netuid": 107, "data": "payload"}

    def test_falls_back_to_set_commitment(self):
        bt_compat, _ = _load_bt_compat()
        sub = types.SimpleNamespace(set_commitment=lambda **k: True)
        ok, _, _blk = bt_compat.commit(sub, wallet="W", netuid=1, data="d")
        assert ok is True

    def test_reports_when_the_sdk_has_neither(self):
        bt_compat, _ = _load_bt_compat()
        ok, reason, _blk = bt_compat.commit(types.SimpleNamespace(), wallet="W", netuid=1, data="d")
        assert ok is False and "neither" in reason

    def test_rate_limit_is_reported_not_raised(self):
        """Rate limiting is normal — subtensor enforces a minimum block interval
        between commitments from one hotkey. It must not propagate."""
        bt_compat, _ = _load_bt_compat()

        def commit(**k):
            raise Exception("CommitmentSetRateLimitExceeded")

        ok, reason, _blk = bt_compat.commit(
            types.SimpleNamespace(commit=commit), wallet="W", netuid=1, data="d"
        )
        assert ok is False and "RateLimit" in reason

    def test_normalizes_tuple_and_object_results(self):
        bt_compat, _ = _load_bt_compat()
        tup = types.SimpleNamespace(commit=lambda **k: (False, "too soon"))
        assert bt_compat.commit(tup, wallet="W", netuid=1, data="d") == (False, "too soon", None)

        obj = types.SimpleNamespace(
            commit=lambda **k: types.SimpleNamespace(success=True, message="in block")
        )
        assert bt_compat.commit(obj, wallet="W", netuid=1, data="d") == (True, "in block", None)

    def test_handles_positional_only_signatures(self):
        bt_compat, _ = _load_bt_compat()

        def commit(wallet, netuid, data, /):
            return True

        ok, _, _blk = bt_compat.commit(
            types.SimpleNamespace(commit=commit), wallet="W", netuid=1, data="d"
        )
        assert ok is True


class _Balance:
    def __init__(self, tao): self.tao = tao
    @classmethod
    def from_tao(cls, t): return cls(t)
    def __eq__(self, o): return isinstance(o, _Balance) and o.tao == self.tao
    def __repr__(self): return "Balance(%s)" % self.tao


class TestTransfer:
    """Moves real TAO — the refusals matter more than the happy path."""

    def test_amount_is_passed_as_a_balance_not_a_float(self):
        """Several SDKs read a bare float as RAO. A 1e9x transfer is unrecoverable."""
        bt_compat, _ = _load_bt_compat(Balance=_Balance)
        seen = {}

        # An OLDER SDK naming the parameter `dest`. transfer() passes both names
        # and the shim keeps whichever this signature declares.
        def transfer(wallet=None, dest=None, amount=None,
                     wait_for_inclusion=True, wait_for_finalization=False):
            seen.update(dest=dest, amount=amount)
            return True, "0xabc"

        ok, ref, _loc = bt_compat.transfer(
            types.SimpleNamespace(transfer=transfer),
            wallet="W", dest="5Dest", amount_tao=0.005)
        assert ok is True and ref == "0xabc"
        assert seen["dest"] == "5Dest", "destination did not reach the SDK"
        assert seen["amount"] == _Balance(0.005), "amount must be a Balance, not a float"

    def test_refuses_when_the_sdk_has_no_balance_type(self):
        """Refuse rather than guess TAO vs RAO."""
        bt_compat, _ = _load_bt_compat()
        called = []
        sub = types.SimpleNamespace(transfer=lambda **k: called.append(1) or (True, ""))
        ok, reason, _loc = bt_compat.transfer(sub, wallet="W", dest="5D", amount_tao=1.0)
        assert ok is False and "TAO vs RAO" in reason
        assert called == [], "must not have transferred with an ambiguous amount"

    def test_missing_transfer_is_reported_not_raised(self):
        bt_compat, _ = _load_bt_compat(Balance=_Balance)
        ok, reason, _loc = bt_compat.transfer(types.SimpleNamespace(), wallet="W",
                                        dest="5D", amount_tao=1.0)
        assert ok is False and "no transfer()" in reason

    def test_chain_error_is_reported_not_raised(self):
        """A failed payment must surface as 'not paid', never take the miner down."""
        bt_compat, _ = _load_bt_compat(Balance=_Balance)

        def transfer(**k): raise ConnectionError("subtensor unreachable")

        ok, reason, _loc = bt_compat.transfer(types.SimpleNamespace(transfer=transfer),
                                        wallet="W", dest="5D", amount_tao=1.0)
        assert ok is False and "ConnectionError" in reason

    def test_normalizes_object_and_bool_results(self):
        bt_compat, _ = _load_bt_compat(Balance=_Balance)
        # A result object carrying .success. The locator does NOT come from a
        # block_hash attribute on the response — 10.3 has none, it lives on
        # extrinsic_receipt — so this yields success with no locator.
        obj = types.SimpleNamespace(
            transfer=lambda **k: types.SimpleNamespace(success=True, message="Success"))
        ok, _reason, loc = bt_compat.transfer(obj, wallet="W", dest="5D", amount_tao=1.0)
        assert ok is True
        assert loc is None, "a status message must never become a block reference"

        plain = types.SimpleNamespace(transfer=lambda **k: False)
        ok, _r, _loc = bt_compat.transfer(plain, wallet="W", dest="5D", amount_tao=1.0)
        assert ok is False

    def test_bare_true_succeeds_but_yields_no_locator(self):
        """A bare True says the money moved and nothing else. There is no
        receipt, so there is no provable locator — the caller must treat that as
        paid-but-unprovable rather than invent a reference."""
        bt_compat, _ = _load_bt_compat(Balance=_Balance)
        sub = types.SimpleNamespace(transfer=lambda **k: True, get_current_block=lambda: 991)
        ok, _ref, loc = bt_compat.transfer(sub, wallet="W", dest="5D", amount_tao=1.0)
        assert ok is True
        assert loc is None
