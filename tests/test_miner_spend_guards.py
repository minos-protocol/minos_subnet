"""Tests for the two guards on the miner's spending path.

Both protect the same thing: the miner must not spend for a submission that
cannot be accepted.

  1. the round deadline is re-checked immediately before anything is spent, not
     only when the round was picked up — a multi-GB BAM download and a long
     calling run sit in between, so the window can have closed by then;

  2. an advertised ``free_submissions_per_round`` of 0 does not make a round's
     first submission chargeable; it is treated as 1 unless the operator opts
     in.

The miner half is exercised on a bare ``Miner`` instance with the spending
collaborators replaced by spies: the point being pinned is the ORDER of the
window check against the payment and commitment calls, which needs none of the
wallet, subtensor or docker machinery a real ``Miner`` builds in __init__.
"""

import importlib.util
import sys
import types

import pytest

from utils import submission_payment as sp


def _load_miner_module():
    """Load neurons/miner.py with the two SDK imports it does not need here.

    bittensor and python-dotenv are not installed in the test environment and
    nothing in the code under test touches either beyond a no-op logger, so they
    are stubbed the way tests/test_bt_compat.py stubs the SDK. Pre-existing
    modules are restored afterwards so a stub cannot leak into another test file.
    """
    saved = {name: sys.modules.get(name) for name in ("bittensor", "dotenv")}
    try:
        if "bittensor" not in sys.modules:
            bt = types.ModuleType("bittensor")

            class _Logging:
                def __getattr__(self, _name):
                    return lambda *a, **k: None

            bt.logging = _Logging()
            # bt_compat refuses to load against an SDK missing the core
            # methods, so the stub has to model a supported one.
            bt.__version__ = "10.3.0"
            bt.Subtensor = type("Subtensor", (), {
                "set_weights": lambda self: None,
                "metagraph": lambda self: None,
            })
            sys.modules["bittensor"] = bt
        if "dotenv" not in sys.modules:
            dotenv = types.ModuleType("dotenv")
            dotenv.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = dotenv

        spec = importlib.util.spec_from_file_location(
            "minos_miner_under_test", "neurons/miner.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


miner_mod = _load_miner_module()

ROUND_ID = "2026-01-21T12:00:00+00:00"


class Spy:
    """Records that it was called, and with what."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    @property
    def called(self):
        return bool(self.calls)


class AsyncSpy(Spy):
    async def __call__(self, *args, **kwargs):  # type: ignore[override]
        self.calls.append((args, kwargs))
        return self.result


def make_miner(remaining=None):
    """A Miner with only the state the submit path reads.

    ``remaining`` is seconds left in the submission window; None records no
    deadline at all, which is the "we never learned one" case.
    """
    m = miner_mod.Miner.__new__(miner_mod.Miner)
    m.submitted_rounds = set()
    m.round_submit_counts = {}
    m._submit_order = []
    m._round_deadlines = {}
    if remaining is not None:
        m._round_deadlines[ROUND_ID] = miner_mod.time.monotonic() + remaining
    m.variant_caller = "gatk"
    m.demo = False
    m._submission_payment = AsyncSpy((None, False))
    m._make_commitment = Spy((None, None, None))  # (commitment, block, nonce)
    m.platform_client = types.SimpleNamespace(
        submit_config=AsyncSpy({"success": True, "submission_id": "abc"})
    )
    m._payment_ledger = None  # only touched when a payment proof exists
    return m


def submit(m):
    import asyncio
    return asyncio.run(m._submit_result(ROUND_ID, {"tool": "gatk"}, 1000, 12.0))


class TestClosedRoundIsNotPaidFor:
    def test_a_window_that_closed_during_variant_calling_spends_nothing(self):
        """The window check runs before the fee and the commitment, not after
        submit_config reports the round closed."""
        m = make_miner(remaining=-60)

        assert submit(m) is False
        assert not m._submission_payment.called, "paid a fee into a closed round"
        assert not m._make_commitment.called, "burned the commitment slot"
        assert not m.platform_client.submit_config.called

    def test_too_little_window_left_for_the_extrinsics_spends_nothing(self):
        """Both the transfer and the chain commitment wait on a block, so a
        window shorter than the margin cannot be met even though it is open."""
        m = make_miner(remaining=miner_mod.MIN_SPEND_TIME_SECONDS - 1)

        assert submit(m) is False
        assert not m._submission_payment.called
        assert not m._make_commitment.called

    def test_an_open_window_still_submits(self):
        m = make_miner(remaining=miner_mod.MIN_SPEND_TIME_SECONDS + 5)

        assert submit(m) is True
        assert m._submission_payment.called
        assert m.platform_client.submit_config.called
        assert ROUND_ID in m.submitted_rounds
        # The commitment is gated on the platform asking for it, and this
        # miner's platform advertises nothing — so submitting must still work
        # without one. See TestTheCommitmentIsBehindThePlatformSwitch.
        assert not m._make_commitment.called

    def test_an_unknown_deadline_does_not_block_the_submission(self):
        """A missing deadline is not evidence the round closed, so an incomplete
        record lets the submission proceed rather than skipping it."""
        m = make_miner(remaining=None)

        assert submit(m) is True
        assert m.platform_client.submit_config.called

    def test_a_blocked_payment_still_stops_the_submission(self):
        """The pre-existing guard must survive the new one."""
        m = make_miner(remaining=3600)
        m._submission_payment = AsyncSpy((None, True))

        assert not submit(m)
        assert not m._make_commitment.called
        assert not m.platform_client.submit_config.called


class TestSubmitOnlyRoundPath:
    @staticmethod
    def _miner(*, submit_only=True, remaining=None):
        m = miner_mod.Miner.__new__(miner_mod.Miner)
        m.submit_only = submit_only
        m.config = types.SimpleNamespace(resubmit=False)
        m.submitted_rounds = set()
        m._hotkey_submissions_used = {}
        m._quoted_fee_tao = {}
        m._round_deadlines = {}
        m.variant_caller = "gatk"
        m.demo = False
        m.platform_client = types.SimpleNamespace(
            get_round_status=AsyncSpy({
                "has_active_round": True,
                "round_id": ROUND_ID,
                "status": "open",
                "region": "chr20:1-1000",
                "time_remaining_seconds": (
                    remaining if remaining is not None
                    else miner_mod.MIN_SPEND_TIME_SECONDS + 5
                ),
                "has_submitted": False,
            })
        )
        m._get_tool_config = Spy(
            {"tool": "gatk", "version": "4.5.0.0", "gatk_options": {}}
        )
        m._download_bam = Spy(None)
        m._run_variant_calling = AsyncSpy((123, 4.5))
        m._submit_result = AsyncSpy(True)
        return m

    @staticmethod
    def _process(m):
        import asyncio
        return asyncio.run(m.process_round())

    def test_submits_without_downloading_or_running_the_caller(self):
        m = self._miner()

        assert self._process(m) is True
        assert not m._download_bam.called
        assert not m._run_variant_calling.called
        assert m._submit_result.called
        args, kwargs = m._submit_result.calls[0]
        assert args[0] == ROUND_ID
        assert kwargs == {"variant_count": None, "elapsed": None}

    def test_uses_spend_margin_instead_of_ten_minute_calling_margin(self):
        m = self._miner(remaining=miner_mod.MIN_SPEND_TIME_SECONDS + 1)

        assert self._process(m) is True
        assert m._submit_result.called

    def test_normal_mode_keeps_the_ten_minute_guard(self):
        m = self._miner(
            submit_only=False,
            remaining=miner_mod.MIN_SUBMISSION_TIME_SECONDS - 1,
        )

        assert self._process(m) is False
        assert not m._download_bam.called
        assert not m._submit_result.called


DEST = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


@pytest.fixture(autouse=True)
def _paying_on(monkeypatch):
    """usable is False without the opt-in, which would make payment_required
    False for every reason at once and prove nothing about the allowance."""
    monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", "1")
    monkeypatch.delenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", raising=False)
    monkeypatch.delenv("MINOS_MAX_RESUBMISSION_FEE_TAO", raising=False)


def policy(**kw):
    raw = {
        "resubmission_fee_enabled": True,
        "resubmission_fee_tao": 0.001,
        "resubmission_payment_address": DEST,
        "free_submissions_per_round": 1,
    }
    raw.update(kw)
    return sp.SubmissionPolicy(raw)


class TestZeroFreeAllowance:
    def test_an_advertised_zero_does_not_charge_the_first_submission(self):
        """An advertised 0 would charge for the submission the feature
        documents as free, so it reads as the default allowance instead."""
        p = policy(free_submissions_per_round=0)

        assert p.free_submissions == sp.DEFAULT_FREE_SUBMISSIONS
        assert p.payment_required(0) is False

    def test_the_clamped_allowance_still_charges_the_resubmission(self):
        """Clamping must not turn the fee off — only protect the first one."""
        assert policy(free_submissions_per_round=0).payment_required(1) is True

    @pytest.mark.parametrize("advertised", [0, -5, None, "abc", "0"])
    def test_every_unusable_allowance_lands_on_the_same_default(self, advertised):
        """0 lands on the same documented fallback as a negative or missing
        allowance: one rule for every unusable value, not a separate one for 0."""
        p = policy(free_submissions_per_round=advertised)

        assert p.free_submissions == sp.DEFAULT_FREE_SUBMISSIONS
        assert p.payment_required(0) is False

    def test_a_real_allowance_is_untouched(self):
        p = policy(free_submissions_per_round=3)

        assert p.free_submissions == 3
        assert p.payment_required(2) is False
        assert p.payment_required(3) is True


class TestZeroAllowanceOptIn:
    def test_zero_is_honoured_when_the_operator_opts_in(self, monkeypatch):
        """Charging for the first submission stays a reachable policy — it is
        the operator who turns it on."""
        monkeypatch.setenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", "1")

        p = policy(free_submissions_per_round=0)

        assert p.free_submissions == 0
        assert p.payment_required(0) is True

    def test_the_opt_in_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", raising=False)
        assert sp.zero_free_allowance_permitted() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_the_opt_in_accepts_the_same_spellings_as_the_paying_opt_in(
        self, monkeypatch, value
    ):
        monkeypatch.setenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", value)
        assert sp.zero_free_allowance_permitted() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "maybe"])
    def test_anything_else_leaves_the_first_submission_free(self, monkeypatch, value):
        monkeypatch.setenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", value)
        assert policy(free_submissions_per_round=0).payment_required(0) is False

    @pytest.mark.parametrize("optin", ["0", ""])
    def test_zero_allowance_cannot_pay_without_an_opt_in(self, monkeypatch, optin):
        """A zero allowance must not become a way to pay on a miner that never
        opted in -- neither an explicit no nor an absent setting."""
        monkeypatch.setenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", "1")
        monkeypatch.setenv("MINER_PAY_FOR_RESUBMISSIONS", optin)

        assert policy(free_submissions_per_round=0).payment_required(0) is False


class TestTheCommitmentIsBehindThePlatformSwitch:
    """Committing costs an extrinsic per submission, so it is gated on network
    configuration. An absent setting reads as disabled, so an older deployment
    and a disabled one behave the same, and unreadable network configuration
    does not start extrinsic spending."""

    @staticmethod
    def _miner(network_config, *, demo=False):
        import types
        m = miner_mod.Miner.__new__(miner_mod.Miner)
        m.demo = demo
        m.subtensor = object()
        m.wallet = object()

        async def get_network_config():
            if isinstance(network_config, Exception):
                raise network_config
            return network_config

        m.platform_client = types.SimpleNamespace(get_network_config=get_network_config)
        return m

    @staticmethod
    def _enabled(miner):
        import asyncio
        return asyncio.run(miner._config_commitment_enabled())

    def test_enabled_only_when_the_platform_says_so(self):
        assert self._enabled(self._miner({"config_commitment_enabled": True})) is True

    def test_absent_means_disabled(self):
        assert self._enabled(self._miner({"burn_rate": 0.5})) is False

    def test_explicitly_false_means_disabled(self):
        assert self._enabled(self._miner({"config_commitment_enabled": False})) is False

    def test_a_non_boolean_does_not_enable_it(self):
        """`is True` rather than truthiness: the string "false" is truthy."""
        for value in ("true", "false", 1, "1", [], {}):
            assert self._enabled(self._miner({"config_commitment_enabled": value})) is False

    def test_an_unreachable_platform_does_not_enable_it(self):
        assert self._enabled(self._miner(ConnectionError("down"))) is False

    def test_a_malformed_response_does_not_enable_it(self):
        assert self._enabled(self._miner("not a dict")) is False

    def test_demo_mode_never_commits(self):
        miner = self._miner({"config_commitment_enabled": True}, demo=True)
        assert self._enabled(miner) is False

    def test_no_wallet_or_subtensor_never_commits(self):
        miner = self._miner({"config_commitment_enabled": True})
        miner.wallet = None
        assert self._enabled(miner) is False
        miner.wallet = object()
        miner.subtensor = None
        assert self._enabled(miner) is False


class TestConfigFlagIsTakenBackFromBittensor:
    """bittensor builds its logging config at IMPORT time and reads --config
    straight out of sys.argv, expecting YAML. The side modes use --config for
    the variant-caller .conf, so `--practice --config configs/gatk.conf` would
    otherwise fail inside `import bittensor` with a YAML parse error naming
    neither this program nor the flag.

    The value is lifted out of argv ahead of that import. These drive the lift
    itself, since the import-time behaviour cannot be re-triggered in-process.
    """

    @staticmethod
    def _lift(argv):
        """The scrub at the top of neurons/miner.py, applied to one argv."""
        side_flags = ("--score", "--practice", "--demo")
        lifted = None
        if not any(f in argv for f in side_flags):
            return argv, None
        kept, i = [], 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--config" and i + 1 < len(argv):
                lifted = argv[i + 1]
                i += 2
                continue
            if arg.startswith("--config="):
                lifted = arg.split("=", 1)[1]
                i += 1
                continue
            kept.append(arg)
            i += 1
        return kept, lifted

    def test_the_start_script_invocation_leaves_no_config_in_argv(self):
        argv = ["--practice", "--sample_id", "abc", "--config", "configs/gatk.conf"]
        kept, lifted = self._lift(argv)
        assert "--config" not in kept, "bittensor would still try to YAML-parse it"
        assert lifted == "configs/gatk.conf"
        assert kept == ["--practice", "--sample_id", "abc"]

    def test_the_equals_form_is_handled(self):
        kept, lifted = self._lift(["--demo", "--config=configs/bcftools.conf"])
        assert lifted == "configs/bcftools.conf"
        assert kept == ["--demo"]

    def test_other_arguments_survive_untouched(self):
        argv = ["--score", "--bam", "x.bam", "--config", "c.conf", "--region", "chr20:1-2"]
        kept, _ = self._lift(argv)
        assert kept == ["--score", "--bam", "x.bam", "--region", "chr20:1-2"]

    def test_the_full_miner_keeps_bittensors_meaning_of_config(self):
        """No side mode means this is the real miner, which has no --config of
        its own. Taking it there would break bittensor's documented flag."""
        argv = ["--netuid", "107", "--config", "bt.yaml"]
        kept, lifted = self._lift(argv)
        assert kept == argv and lifted is None

    def test_a_trailing_config_with_no_value_is_left_alone(self):
        """Malformed input must not silently swallow the flag."""
        kept, lifted = self._lift(["--demo", "--config"])
        assert kept == ["--demo", "--config"] and lifted is None
