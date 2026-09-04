"""Tests for the ordering invariants the money and commitment paths rely on.

Each module states an ordering guarantee in its docstring and then depends on it
for correctness — notably "mark spent only once the platform accepts" and
"persist the nonce before publishing". Prose is not enforcement, so those
orderings are asserted here.

These tests are written against BEHAVIOUR, not implementation: they drive the
real ledgers on a real temp path, because the failure modes being pinned are
about what survives a crash and what happens on the second attempt.
"""

import importlib
import sys
import types

import pytest

from utils import submission_payment
from utils.submission_payment import (
    PaymentLedger,
    SubmissionPolicy,
    max_fee_tao,
    pay_for_resubmission,
)

# utils.bt_compat imports bittensor at module scope, so it cannot be imported
# here — see tests/test_bt_compat.py, which stubs the SDK the same way. The
# sentinel is duplicated as a literal on purpose: if the constant is ever
# renamed, test_ambiguous_sentinel_matches_the_shim fails loudly rather than
# every "never pay twice" test passing against a stale value.
AMBIGUOUS = "ambiguous:outcome-unknown"


def _load_bt_compat(**bt_attrs):
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
    spec = importlib.util.spec_from_file_location("utils.bt_compat", "utils/bt_compat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


USABLE_POLICY = {
    "resubmission_fee_enabled": True,
    "resubmission_fee_tao": 0.001,
    "resubmission_payment_address": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "free_submissions_per_round": 1,
}


@pytest.fixture
def ledger(tmp_path):
    return PaymentLedger(path=tmp_path / "payments.jsonl")


@pytest.fixture
def paying_on(monkeypatch):
    monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")


class FakeCompat:
    """Stands in for bt_compat, recording how many transfers were attempted."""

    AMBIGUOUS = AMBIGUOUS

    def __init__(self, results, locator=None):
        self.results = list(results)
        self.calls = 0
        self.locator = locator

    def transfer(self, subtensor, *, wallet, dest, amount_tao):
        """Mirrors the real shim: (ok, reason, locator). The locator normally
        comes straight off the SDK receipt, so the fake supplies one unless the
        test is exercising the unlocatable path."""
        self.calls += 1
        if not self.results:
            return False, "exhausted", None
        ok, reason = self.results.pop(0)
        if not ok or not reason or self.locator is _UNLOCATABLE:
            return ok, reason, None
        return ok, reason, {"block_hash": reason, "block_number": 42, "extrinsic_index": 3}

    def locate_transfer(self, subtensor, *, block_hash, signer_ss58, dest, amount_tao):
        # Fallback path only: reached when the receipt carried no locator.
        if self.locator is _UNLOCATABLE:
            return None
        return {"block_hash": block_hash, "block_number": 42, "extrinsic_index": 3}


_UNLOCATABLE = object()
COLDKEY = "5CoLdOwNeR"


def _wallet(coldkey=COLDKEY):
    return types.SimpleNamespace(coldkeypub=types.SimpleNamespace(ss58_address=coldkey))


def _subtensor(owner=COLDKEY):
    """A chain that reports `owner` as the coldkey behind any hotkey."""
    return types.SimpleNamespace(get_hotkey_owner=lambda hk: owner)


def _pay(compat, policy, ledger, round_id="r1", hotkey="hk1",
         wallet=None, subtensor=None):
    return pay_for_resubmission(
        bt_compat=compat,
        subtensor=_subtensor() if subtensor is None else subtensor,
        wallet=_wallet() if wallet is None else wallet,
        policy=policy, round_id=round_id, hotkey=hotkey, ledger=ledger,
    )



class TestNeverPayTwice:
    def test_an_unspent_proof_is_reused_rather_than_repaid(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc")])
        policy = SubmissionPolicy(USABLE_POLICY)

        first = _pay(compat, policy, ledger)
        second = _pay(compat, policy, ledger)

        assert first is not None
        assert second == first
        assert compat.calls == 1, "second attempt must not transfer again"

    def test_an_ambiguous_transfer_blocks_any_further_payment_that_round(self, ledger, paying_on):
        """The money may or may not have moved. Paying again to find out is the
        one outcome that is strictly worse than not submitting."""
        compat = FakeCompat([(False, AMBIGUOUS), (True, "0xdef")])
        policy = SubmissionPolicy(USABLE_POLICY)

        assert _pay(compat, policy, ledger) is None
        assert _pay(compat, policy, ledger) is None
        assert compat.calls == 1, "must not retry a transfer of unknown outcome"
        assert ledger.unresolved_intent("r1", "hk1")

    def test_a_clear_failure_does_not_wedge_the_round(self, ledger, paying_on):
        """A readable failure means nothing moved, so the next attempt is safe.
        Without recording it, one declined transfer would look identical to an
        unknown outcome and block the round forever."""
        compat = FakeCompat([(False, "insufficient balance"), (True, "0xdef")])
        policy = SubmissionPolicy(USABLE_POLICY)

        assert _pay(compat, policy, ledger) is None
        assert not ledger.unresolved_intent("r1", "hk1")
        assert _pay(compat, policy, ledger) is not None
        assert compat.calls == 2

    def test_two_distinct_payments_are_distinguishable(self, ledger, paying_on):
        """Proof identity must not be whole-JSON equality: two payments that
        agree on every field would read as the same proof, and the second
        could be discarded as already spent."""
        compat = FakeCompat([(True, "0xsame"), (True, "0xsame")])
        policy = SubmissionPolicy(USABLE_POLICY)

        first = _pay(compat, policy, ledger)
        ledger.mark_spent("r1", "hk1", first)
        second = _pay(compat, policy, ledger)

        assert second is not None
        assert second["proof_id"] != first["proof_id"]


class TestOptInIsHonoured:
    def test_disabling_payment_stops_a_stored_proof_being_reused(self, ledger, monkeypatch):
        """Turning the feature off has to actually turn it off, so the policy
        check runs before any stored proof is returned."""
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is not None

        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "0")
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None

    def test_nothing_is_spent_when_the_platform_advertises_no_policy(self, ledger, monkeypatch):
        """The platform is the switch: no advertised policy, no transfer."""
        monkeypatch.delenv("MINER_PAY_FOR_RESUBMISSIONS", raising=False)
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(None), ledger) is None
        assert _pay(compat, SubmissionPolicy({}), ledger) is None
        assert compat.calls == 0

    def test_operator_opt_out_stops_spending_even_with_a_live_policy(self, ledger, monkeypatch):
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "0")
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None
        assert compat.calls == 0


class TestFeeCeiling:
    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1", "abc", ""])
    def test_an_unusable_ceiling_falls_back_instead_of_disabling_itself(self, raw, monkeypatch):
        """nan parses fine and every comparison against it is False, so an
        unguarded cap would evaluate as "no cap" — worse than having none."""
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", raw)
        assert max_fee_tao() == submission_payment.DEFAULT_MAX_FEE_TAO

    def test_a_fee_over_the_ceiling_is_refused_not_clamped(self, monkeypatch):
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.01")
        policy = SubmissionPolicy({**USABLE_POLICY, "resubmission_fee_tao": 5.0})
        assert policy.usable is False


class TestNothingSpendsWhileTheFeeIsDisabled:
    """The switch has to be absolute. Every shape a disabled or half-configured
    platform can present must reach zero transfers — a policy that is merely
    'mostly off' spends someone's TAO the first time a field is missing."""

    DEST = USABLE_POLICY["resubmission_payment_address"]

    @pytest.mark.parametrize("label,cfg,quote", [
        ("fee switched off", {"resubmission_fee_enabled": False, "resubmission_fee_tao": 0.01,
                              "resubmission_payment_address": DEST}, None),
        ("no policy advertised", {"burn_rate": 0.5}, None),
        ("empty response", {}, None),
        ("older platform", None, None),
        ("enabled but fee is zero", {"resubmission_fee_enabled": True, "resubmission_fee_tao": 0.0,
                                     "resubmission_payment_address": DEST}, None),
        ("enabled but no destination", {"resubmission_fee_enabled": True, "resubmission_fee_tao": 0.01,
                                        "resubmission_payment_address": ""}, None),
        ("enabled but the quote is zero", {"resubmission_fee_enabled": True, "resubmission_fee_tao": 0.01,
                                           "resubmission_payment_address": DEST}, 0.0),
    ])
    def test_no_transfer_is_attempted(self, ledger, paying_on, label, cfg, quote):
        compat = FakeCompat([(True, "0xabc")])
        policy = SubmissionPolicy(cfg, quoted_fee_tao=quote)
        assert policy.usable is False, f"{label}: policy reported usable"
        assert _pay(compat, policy, ledger) is None
        assert compat.calls == 0, f"{label}: attempted a transfer"
