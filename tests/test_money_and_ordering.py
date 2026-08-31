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
import json
import pathlib
import stat
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
# every "never pay twice" test silently passing against a stale value.
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
        """Proof identity used to be whole-JSON equality, so two payments that
        agreed on every field read as the same proof and the second could be
        discarded as already spent."""
        compat = FakeCompat([(True, "0xsame"), (True, "0xsame")])
        policy = SubmissionPolicy(USABLE_POLICY)

        first = _pay(compat, policy, ledger)
        ledger.mark_spent("r1", "hk1", first)
        second = _pay(compat, policy, ledger)

        assert second is not None
        assert second["proof_id"] != first["proof_id"]


class TestProofIsUsable:
    def test_a_proof_without_an_on_chain_reference_is_refused(self, ledger, paying_on):
        """Nobody can verify it, so it is not a proof — and crucially the fee has
        already been spent, so this must not become a retry."""
        compat = FakeCompat([(True, ""), (True, "0xdef")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None
        assert compat.calls == 1, "paid twice after an unprovable transfer"

    def test_payment_survives_a_ledger_write_failure(self, ledger, paying_on, monkeypatch):
        """MONEY HAS ALREADY MOVED at this point. The docstring forbids "pays and
        gets nothing", so a ledger failure must not swallow the proof."""
        compat = FakeCompat([(True, "0xabc")])

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(PaymentLedger, "record_proof", boom)
        proof = _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger)

        assert proof is not None, "the caller must still be able to spend it"
        assert proof["reference"] == "0xabc"


class TestOptInIsHonoured:
    def test_disabling_payment_stops_a_stored_proof_being_reused(self, ledger, monkeypatch):
        """Turning the feature off has to actually turn it off. The stored proof
        was previously returned before any policy check ran."""
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
        """nan parses fine and every comparison against it is False, so the cap
        silently became "no cap" — worse than having none."""
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", raw)
        assert max_fee_tao() == submission_payment.DEFAULT_MAX_FEE_TAO

    def test_a_fee_over_the_ceiling_is_refused_not_clamped(self, monkeypatch):
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")
        monkeypatch.setenv("MINOS_MAX_RESUBMISSION_FEE_TAO", "0.01")
        policy = SubmissionPolicy({**USABLE_POLICY, "resubmission_fee_tao": 5.0})
        assert policy.usable is False


class TestPolicyParsing:
    @pytest.mark.parametrize("raw,expected", [
        ({"free_submissions_per_round": None}, 1),
        ({"free_submissions_per_round": "3"}, 3),
        ({"free_submissions_per_round": "abc"}, 1),
        ({"free_submissions_per_round": -5}, 1),
        ({}, 1),
    ])
    def test_allowance_is_parsed_defensively(self, raw, expected):
        """It arrives in an unauthenticated body and decides when spending
        starts; a raise here must not be what stops a submission."""
        assert SubmissionPolicy(raw).free_submissions == expected


class TestLedgerDurability:
    def test_secrets_are_never_world_readable(self, tmp_path):
        led = PaymentLedger(path=tmp_path / "nested" / "p.jsonl")
        led.record_intent("r1", "hk1", 0.001, "dest")
        assert stat.S_IMODE(led.path.stat().st_mode) == 0o600

    def test_a_corrupt_line_does_not_take_down_the_payment_path(self, tmp_path):
        """A line parsing to a list rather than an object used to raise on
        .get(), turning one bad byte into a total payment outage."""
        led = PaymentLedger(path=tmp_path / "p.jsonl")
        led.record_intent("r1", "hk1", 0.001, "dest")
        with open(led.path, "a") as fh:
            fh.write("[1,2,3]\n")
            fh.write("not json at all\n")
            fh.write('"a string"\n')
        assert led.unspent_proof("r1", "hk1") is None
        assert led.unresolved_intent("r1", "hk1") is True


class TestTransferAmbiguity:
    def test_none_is_reported_as_ambiguous_not_as_failure(self):
        """The caller's failure path is "try again", so reading an unreadable
        SDK result as failure buys a second transfer for one submission."""
        class Bal:
            @staticmethod
            def from_tao(v):
                return v

        m = _load_bt_compat(Balance=Bal)

        class SDK:
            def transfer(self, **kw):
                return None

        ok, ref, locator = m.transfer(SDK(), wallet=None, dest="d", amount_tao=0.001)
        assert ok is False
        assert ref == m.AMBIGUOUS
        assert locator is None

    def test_success_with_no_receipt_yields_no_locator(self):
        """A bare True says the money moved and nothing more.

        ok and locator answer different questions: ok is "did the TAO move",
        locator is "can we prove which extrinsic moved it". The shim reports
        both honestly and the CALLER refuses to build a proof without a locator
        — see TestUnlocatablePaymentDoesNotDoublePay, which pins that no second
        transfer follows. Collapsing an unprovable success into ok=False would
        report a completed payment as failed."""
        class Bal:
            @staticmethod
            def from_tao(v):
                return v

        m = _load_bt_compat(Balance=Bal)

        class SDK:
            def transfer(self, **kw):
                return True

        ok, _ref, locator = m.transfer(SDK(), wallet=None, dest="d", amount_tao=0.001)
        assert ok is True, "the transfer did succeed; saying otherwise is a lie about money"
        assert locator is None, "nothing identifies the extrinsic, so there is no proof"

    def test_ambiguous_sentinel_matches_the_shim(self):
        m = _load_bt_compat()
        assert m.AMBIGUOUS == AMBIGUOUS


class TestNeverPayAFeeThatWillBeRefused:
    """The platform verifies that the signing coldkey owned the hotkey. That
    check runs after the money has moved, so only the miner side can stop a fee
    being spent on a submission that is then refused."""

    def test_does_not_pay_when_the_coldkey_does_not_own_the_hotkey(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc")])
        proof = _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger,
                     subtensor=_subtensor(owner="5SomeoneElse"))
        assert proof is None
        assert compat.calls == 0, "paid a fee the platform would refuse"

    def test_does_not_pay_when_the_hotkey_has_no_owner(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger,
                    subtensor=_subtensor(owner=None)) is None
        assert compat.calls == 0

    def test_does_not_pay_when_the_chain_read_fails(self, ledger, paying_on):
        """Refusing to pay is recoverable; paying blind is not."""
        def boom(_hk):
            raise ConnectionError("no rpc")
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger,
                    subtensor=types.SimpleNamespace(get_hotkey_owner=boom)) is None
        assert compat.calls == 0

    def test_does_not_pay_when_the_wallet_coldkey_is_unreadable(self, ledger, paying_on):
        class NoColdkey:
            @property
            def coldkeypub(self):
                raise FileNotFoundError("coldkeypub missing")
        compat = FakeCompat([(True, "0xabc")])
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger,
                    wallet=NoColdkey()) is None
        assert compat.calls == 0


class TestUnlocatablePaymentDoesNotDoublePay:
    """A transfer that lands but cannot be located yields no usable proof. The
    money is gone, so the one thing that must not happen is paying again."""

    def test_no_proof_when_the_extrinsic_cannot_be_located(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc")], locator=_UNLOCATABLE)
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None
        assert compat.calls == 1, "the transfer did happen"

    def test_a_second_attempt_does_not_transfer_again(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc"), (True, "0xdef")], locator=_UNLOCATABLE)
        _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger)
        _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger)
        assert compat.calls == 1, "paid twice for one submission slot"


class TestProofCarriesTheOnChainLocator:
    def test_proof_names_the_extrinsic_the_platform_will_verify(self, ledger, paying_on):
        proof = _pay(FakeCompat([(True, "0xabc")]), SubmissionPolicy(USABLE_POLICY), ledger)
        assert proof["block_hash"] == "0xabc"
        assert proof["block_number"] == 42
        assert proof["extrinsic_index"] == 3


class TestDailySpendCeiling:
    """The per-submission cap bounds one payment. This bounds a bad day.

    It matters more now that spending follows the platform's advertised policy
    rather than a local opt-in: an operator who auto-updates and never reads a
    release note still has a bounded worst case."""

    def test_spending_stops_once_the_daily_ceiling_is_reached(self, ledger, paying_on, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", "0.0025")
        policy = SubmissionPolicy(USABLE_POLICY)  # 0.001 TAO per submission
        compat = FakeCompat([(True, f"0x{i:02x}") for i in range(10)])

        paid = 0
        for n in range(6):
            proof = _pay(compat, policy, ledger, round_id=f"r{n}")
            if proof is None:
                break
            ledger.mark_spent(f"r{n}", "hk1", proof)
            paid += 1

        assert paid == 2, f"paid {paid} times; 0.0025 TAO ceiling allows 2 x 0.001"
        assert compat.calls == 2, "transferred after the ceiling was reached"

    def test_only_completed_payments_count_toward_the_ceiling(self, ledger, paying_on, monkeypatch):
        """An intent may never have moved money. Counting it would lock a miner
        out over a payment that never happened."""
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", "0.0015")
        ledger.record_intent("r0", "hk1", 0.001, USABLE_POLICY["resubmission_payment_address"])
        assert ledger.spend_since(0) == 0.0
        assert _pay(FakeCompat([(True, "0xabc")]), SubmissionPolicy(USABLE_POLICY), ledger) is not None

    def test_spend_outside_the_window_does_not_count(self, ledger, paying_on, monkeypatch):
        import time as _time
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", "0.0015")
        ledger.record_proof("old", "hk1", {
            "amount_tao": 0.001, "paid_at": int(_time.time()) - 86400 * 3, "proof_id": "x",
        })
        assert ledger.spend_since(int(_time.time()) - 86400) == 0.0
        assert _pay(FakeCompat([(True, "0xabc")]), SubmissionPolicy(USABLE_POLICY), ledger) is not None

    def test_an_unreadable_ledger_fails_toward_spending_less(self, ledger, paying_on):
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text("not json\n{\"kind\":\"proof\"}\n\n", encoding="utf-8")
        assert ledger.spend_since(0) == 0.0, "must not raise on a corrupt ledger"

    @pytest.mark.parametrize("bad", ["", "abc", "-1", "nan", "inf"])
    def test_a_bad_ceiling_override_falls_back_to_the_default(self, monkeypatch, bad):
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", bad)
        assert submission_payment.max_daily_tao() == submission_payment.DEFAULT_MAX_DAILY_TAO


class TestUnprovableSpendCountsTowardTheCeiling:
    """Money that leaves the wallet without yielding a proof is still spent.

    A ceiling that counts only provable payments is blind to exactly the
    failures that cost the most — the ones where the fee went and nothing came
    back — so a miner in that state could burn its balance a fee at a time while
    the ceiling read zero."""

    def test_an_unprovable_transfer_is_counted(self, ledger, paying_on):
        compat = FakeCompat([(True, "0xabc")], locator=_UNLOCATABLE)
        assert _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger) is None
        assert ledger.spend_since(0) == pytest.approx(0.001), "spend went unrecorded"

    def test_it_is_not_offered_back_as_a_reusable_proof(self, ledger, paying_on):
        """There is nothing here the platform could verify, so it must never be
        attached to a submission."""
        compat = FakeCompat([(True, "0xabc")], locator=_UNLOCATABLE)
        _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger)
        assert ledger.unspent_proof("r1", "hk1") is None

    def test_the_ceiling_engages_on_unprovable_spends(self, ledger, paying_on, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", "0.0025")
        compat = FakeCompat([(True, f"0x{i:02x}") for i in range(10)], locator=_UNLOCATABLE)
        for n in range(6):
            _pay(compat, SubmissionPolicy(USABLE_POLICY), ledger, round_id=f"r{n}")
        assert compat.calls == 2, (
            f"made {compat.calls} unprovable transfers; the 0.0025 ceiling allows 2"
        )


class TestTwoProcessesCannotBothPay:
    """The guards (unspent proof, unresolved intent, 24h ceiling) are all reads
    of the ledger followed by a transfer. Two processes on one wallet interleave
    those and both spend."""

    def test_the_spend_lock_actually_serialises_processes(self, tmp_path):
        """Driven with real processes, because an in-process test cannot show a
        cross-process race."""
        import subprocess
        import sys
        import textwrap

        ledger_path = tmp_path / "payments.jsonl"
        script = textwrap.dedent(f'''
            import sys, time, json
            sys.path.insert(0, {str(pathlib.Path.cwd())!r})
            from utils.submission_payment import _wallet_spend_lock, PaymentLedger
            led = PaymentLedger(path={str(ledger_path)!r})
            with _wallet_spend_lock(led):
                # Read-modify-write with a gap wide enough that an unlocked run
                # would interleave.
                n = sum(1 for _ in open(led.path)) if led.path.exists() else 0
                time.sleep(0.35)
                with open(led.path, "a") as fh:
                    fh.write(json.dumps({{"seen": n}}) + "\\n")
        ''')
        procs = [
            subprocess.Popen([sys.executable, "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(3)
        ]
        for p in procs:
            p.wait(timeout=30)

        lines = [json.loads(l) for l in open(ledger_path) if l.strip()]
        assert len(lines) == 3, "a writer was lost"
        seen = sorted(e["seen"] for e in lines)
        assert seen == [0, 1, 2], (
            f"processes interleaved: each saw {seen}, meaning they read the same "
            f"state and would each have transferred"
        )



class TestTheCeilingIsPerHotkey:
    """One host, one ledger, several hotkeys. An unfiltered ceiling makes an
    operator running K hotkeys hit it K times sooner and silently stop
    submitting — the allowance is per hotkey, so its guard must be too."""

    def test_one_hotkeys_spending_does_not_block_another(self, ledger, paying_on, monkeypatch):
        monkeypatch.setenv("MINOS_MAX_DAILY_RESUBMISSION_TAO", "0.0015")
        policy = SubmissionPolicy(USABLE_POLICY)  # 0.001 per submission

        first = _pay(FakeCompat([(True, "0xaaa")]), policy, ledger, hotkey="hk_a")
        assert first is not None
        ledger.mark_spent("r1", "hk_a", first)

        # hk_a is now at 0.001 of a 0.0015 ceiling and cannot pay again.
        assert _pay(FakeCompat([(True, "0xbbb")]), policy, ledger,
                    round_id="r2", hotkey="hk_a") is None

        # hk_b has spent nothing and must be unaffected.
        other = _pay(FakeCompat([(True, "0xccc")]), policy, ledger,
                     round_id="r1", hotkey="hk_b")
        assert other is not None, "one hotkey's spending blocked another"

    def test_spend_since_filters_by_hotkey(self, ledger):
        import time as _t
        now = int(_t.time())
        for hk, amt in (("hk_a", 0.003), ("hk_b", 0.007)):
            ledger.record_proof("r1", hk, {"amount_tao": amt, "paid_at": now, "proof_id": hk})
        assert ledger.spend_since(0, hotkey="hk_a") == pytest.approx(0.003)
        assert ledger.spend_since(0, hotkey="hk_b") == pytest.approx(0.007)
        assert ledger.spend_since(0) == pytest.approx(0.010), "unfiltered still totals the host"


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
