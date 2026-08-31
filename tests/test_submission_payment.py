"""Tests for paid resubmission.

This code moves real TAO, so the tests that matter are the ones where something
goes wrong: a policy we cannot act on, a transfer that fails, a submission that
fails after payment succeeded. Paying and getting nothing is the failure mode
that would make an operator refuse to run this.
"""

import json
import os
import stat
import types

import pytest

from utils import submission_payment as sp


# A real-shaped ss58 address; "5Dest" is correctly rejected by validation.
DEST = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def _opt_in(monkeypatch):
    """Spending is opt-in, so every test that expects a payment must enable it.
    The tests asserting the DEFAULT (off) clear it themselves."""
    monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")
    monkeypatch.delenv("MINOS_MAX_RESUBMISSION_FEE_TAO", raising=False)


def policy(**kw):
    base = dict(resubmission_fee_enabled=True, free_submissions_per_round=1,
                resubmission_fee_tao=0.005, resubmission_payment_address=DEST)
    base.update(kw)
    return sp.SubmissionPolicy(base)


class TestPolicy:
    def test_first_submission_is_free(self):
        assert policy().payment_required(0) is False

    def test_second_submission_requires_payment(self):
        assert policy().payment_required(1) is True
        assert policy().payment_required(9) is True

    def test_allowance_is_configurable(self):
        p = policy(free_submissions_per_round=3)
        assert [p.payment_required(n) for n in (0, 1, 2, 3)] == [False, False, False, True]

    def test_absent_policy_never_charges(self):
        """Older platform, or feature off — miner must behave exactly as before."""
        p = sp.SubmissionPolicy(None)
        assert p.usable is False and p.payment_required(99) is False

    @pytest.mark.parametrize("kw", [
        dict(resubmission_fee_enabled=False),
        dict(resubmission_payment_address=None),
        dict(resubmission_payment_address="5Dest"),          # malformed
        dict(resubmission_payment_address="not an address"),
        dict(resubmission_fee_tao=1000.0),                   # absurd fee
        dict(resubmission_fee_tao=None),
        dict(resubmission_fee_tao=0),
        dict(resubmission_fee_tao="not-a-number"),
    ])
    def test_incomplete_policy_is_treated_as_off(self, kw):
        """Never guess a fee or a destination: a guessed destination is an
        irrecoverable transfer to the wrong account."""
        p = policy(**kw)
        assert p.usable is False
        assert p.payment_required(5) is False


class TestLedger:
    def test_proof_round_trips_and_is_reusable(self, tmp_path):
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        proof = {"reference": "block:42", "amount_tao": 0.005}
        led.record_proof("r1", "hk", proof)
        assert led.unspent_proof("r1", "hk") == proof

    def test_spent_proof_is_not_offered_again(self, tmp_path):
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        proof = {"reference": "block:42"}
        led.record_proof("r1", "hk", proof)
        led.mark_spent("r1", "hk", proof)
        assert led.unspent_proof("r1", "hk") is None

    def test_scoped_to_round_and_hotkey(self, tmp_path):
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        led.record_proof("r1", "hk_a", {"reference": "a"})
        assert led.unspent_proof("r2", "hk_a") is None
        assert led.unspent_proof("r1", "hk_b") is None

    def test_survives_a_corrupt_line(self, tmp_path):
        path = tmp_path / "p.jsonl"
        led = sp.PaymentLedger(path)
        led.record_proof("r1", "hk", {"reference": "good"})
        with open(path, "a") as fh:
            fh.write("{broken\n")
        assert led.unspent_proof("r1", "hk")["reference"] == "good"

    def test_ledger_is_not_world_readable(self, tmp_path):
        path = tmp_path / "p.jsonl"
        sp.PaymentLedger(path).record_proof("r1", "hk", {"reference": "x"})
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0

    def test_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.jsonl"
        monkeypatch.setenv("MINOS_PAYMENT_LEDGER", str(target))
        sp.PaymentLedger().record_proof("r", "hk", {"reference": "x"})
        assert target.exists()


COLDKEY = "5CoLdOwNeR"


def _wallet(coldkey=COLDKEY):
    return types.SimpleNamespace(coldkeypub=types.SimpleNamespace(ss58_address=coldkey))


def _subtensor(owner=COLDKEY):
    return types.SimpleNamespace(get_hotkey_owner=lambda hk: owner)


class _BT:
    """Stand-in for bt_compat.transfer with a recorded outcome."""
    def __init__(self, ok=True, ref="block:7", locatable=True):
        self.ok, self.ref, self.calls = ok, ref, []
        self.locatable = locatable
    def transfer(self, subtensor, *, wallet, dest, amount_tao):
        """Mirrors the real shim: (ok, reason, locator)."""
        self.calls.append(dict(dest=dest, amount_tao=amount_tao))
        if not self.ok or not self.locatable:
            return self.ok, self.ref, None
        return self.ok, self.ref, {
            "block_hash": self.ref, "block_number": 42, "extrinsic_index": 3
        }
    def locate_transfer(self, subtensor, *, block_hash, signer_ss58, dest, amount_tao):
        if not self.locatable:
            return None
        return {"block_hash": block_hash, "block_number": 42, "extrinsic_index": 3}


class TestPayForResubmission:
    def test_pays_and_returns_a_proof(self, tmp_path):
        bt = _BT()
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        proof = sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                        policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        assert proof["reference"] == "block:7"
        assert proof["amount_tao"] == 0.005 and proof["destination"] == DEST
        assert bt.calls == [dict(dest=DEST, amount_tao=0.005)]

    def test_records_intent_before_transferring(self, tmp_path):
        """An interrupted payment must leave a trace, or money moves with no record."""
        path = tmp_path / "p.jsonl"
        led = sp.PaymentLedger(path)
        sp.pay_for_resubmission(bt_compat=_BT(), subtensor=_subtensor(), wallet=_wallet(),
                                policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        kinds = [json.loads(l)["kind"] for l in open(path) if l.strip()]
        assert kinds.index("intent") < kinds.index("proof")

    def test_reuses_an_unspent_proof_instead_of_paying_twice(self, tmp_path):
        """The common case: payment succeeded, submission failed for other reasons."""
        bt = _BT()
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        first = sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                        policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        again = sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                        policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        assert again == first
        assert len(bt.calls) == 1, "paid twice for one submission slot"

    def test_pays_again_once_the_previous_proof_was_spent(self, tmp_path):
        bt = _BT()
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        p1 = sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                     policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        led.mark_spent("r1", "hk", p1)
        sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        assert len(bt.calls) == 2

    def test_failed_transfer_returns_none(self, tmp_path):
        bt = _BT(ok=False, ref="insufficient balance")
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        assert sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                       policy=policy(), round_id="r1", hotkey="hk",
                                       ledger=led) is None

    def test_failed_transfer_leaves_no_reusable_proof(self, tmp_path):
        """A failed payment must not look like a paid slot on the next attempt."""
        led = sp.PaymentLedger(tmp_path / "p.jsonl")
        sp.pay_for_resubmission(bt_compat=_BT(ok=False), subtensor=_subtensor(), wallet=_wallet(),
                                policy=policy(), round_id="r1", hotkey="hk", ledger=led)
        assert led.unspent_proof("r1", "hk") is None

    def test_unusable_policy_does_not_transfer(self, tmp_path):
        bt = _BT()
        sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                policy=sp.SubmissionPolicy(None), round_id="r1",
                                hotkey="hk", ledger=sp.PaymentLedger(tmp_path / "p.jsonl"))
        assert bt.calls == []


class TestSpendingGuards:
    """The fee arrives in an unauthenticated HTTP body and the transfer is signed
    with the COLDKEY, so these guards are the only thing between a platform slip
    and every miner's balance."""

    def test_platform_policy_is_the_switch(self, monkeypatch):
        """The platform decides whether resubmissions cost anything. With no
        operator env set, an advertised policy is honoured."""
        monkeypatch.delenv("MINER_PAY_FOR_RESUBMISSIONS", raising=False)
        assert sp.payment_opted_out() is False
        assert policy().usable is True
        assert policy().payment_required(5) is True

    def test_no_policy_means_no_payment(self, monkeypatch):
        """A platform that advertises nothing must never cause a transfer."""
        monkeypatch.delenv("MINER_PAY_FOR_RESUBMISSIONS", raising=False)
        assert sp.SubmissionPolicy(None).usable is False
        assert sp.SubmissionPolicy({}).usable is False
        assert policy(resubmission_fee_enabled=False).usable is False

    def test_operator_can_opt_out_regardless_of_policy(self, monkeypatch):
        """The escape hatch: an operator who wants no spending at all."""
        for val in ("0", "false", "NO", "off"):
            monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", val)
            assert sp.payment_opted_out() is True
            assert policy().usable is False

    @pytest.mark.parametrize("val", ["", "1", "true", "yes"])
    def test_anything_but_an_opt_out_follows_the_platform(self, monkeypatch, val):
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", val)
        assert sp.payment_opted_out() is False
        assert policy().usable is True

    def test_fee_above_the_cap_is_refused_not_clamped(self, monkeypatch):
        """A units slip (500 for 0.5) must not become a 'capped' payment —
        paying a smaller wrong amount is still paying."""
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.01")
        assert policy(resubmission_fee_tao=0.5).usable is False
        assert policy(resubmission_fee_tao=0.005).usable is True

    def test_default_cap_is_conservative(self, monkeypatch):
        monkeypatch.delenv("MINOS_MAX_RESUBMISSION_FEE_TAO", raising=False)
        assert sp.max_fee_tao() == sp.DEFAULT_MAX_FEE_TAO <= 0.01

    def test_operator_can_raise_the_cap_knowingly(self, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.5")
        assert policy(resubmission_fee_tao=0.4).usable is True

    def test_malformed_cap_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "not-a-number")
        assert sp.max_fee_tao() == sp.DEFAULT_MAX_FEE_TAO

    @pytest.mark.parametrize("addr,ok", [
        (DEST, True),
        ("5Dest", False),
        ("", False),
        ("../../etc/passwd", False),
        ("0xdeadbeef", False),
    ])
    def test_destination_must_look_like_an_address(self, addr, ok):
        """A malformed destination is an irrecoverable transfer to nobody."""
        assert sp._looks_like_ss58(addr) is ok

    def test_over_cap_policy_does_not_transfer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.01")
        bt = _BT()
        sp.pay_for_resubmission(bt_compat=bt, subtensor=_subtensor(), wallet=_wallet(),
                                policy=policy(resubmission_fee_tao=99.0),
                                round_id="r1", hotkey="hk",
                                ledger=sp.PaymentLedger(tmp_path / "p.jsonl"))
        assert bt.calls == [], "transferred despite an over-cap fee"


class TestTheQuotedPriceWins:
    """The fee escalates per coldkey within a round, so the advertised base fee
    is only correct for the first paid submission. Paying it for a later one
    underpays — and the transfer is already on chain when the platform refuses
    it, so the TAO is gone. round-status quotes the real price; it is
    authoritative over the advertised one."""

    ADVERTISED = {
        "resubmission_fee_enabled": True,
        "resubmission_fee_tao": 0.01,          # base
        "resubmission_payment_address": DEST,
        "free_submissions_per_round": 1,
    }

    def test_the_quote_overrides_the_advertised_base_fee(self):
        policy = sp.SubmissionPolicy(self.ADVERTISED, quoted_fee_tao=0.08)
        assert policy.fee_tao == 0.08, "paid the base fee while the platform wanted 0.08"

    def test_no_quote_falls_back_to_the_advertised_fee(self):
        """An older platform sends no quote, and the first paid submission of a
        round is genuinely priced at the base fee."""
        assert sp.SubmissionPolicy(self.ADVERTISED).fee_tao == 0.01
        assert sp.SubmissionPolicy(self.ADVERTISED, quoted_fee_tao=None).fee_tao == 0.01

    def test_the_ceiling_still_applies_to_a_quote(self, monkeypatch):
        """A quote is not a licence to spend: an escalated price above the
        operator's cap is refused like any other."""
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.05")
        policy = sp.SubmissionPolicy(self.ADVERTISED, quoted_fee_tao=1.28)
        assert policy.usable is False

    def test_a_quote_within_the_ceiling_is_usable(self, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.5")
        assert sp.SubmissionPolicy(self.ADVERTISED, quoted_fee_tao=0.08).usable is True
