"""Pin the payment path to the installed bittensor SDK.

The rest of the payment tests use hand-written doubles, which can only show that
the code agrees with itself. These derive everything from the SDK instead: the
signature comes from `inspect.signature(Subtensor.transfer)`, and results are the
SDK's own `ExtrinsicResponse`. A renamed parameter or a restructured receipt
therefore fails here rather than on a live node.

Two SDK facts these depend on, both of which a future version could change:
  * the transfer destination is declared as `destination_ss58`
  * `submit_extrinsic` builds its receipt WITHOUT `block_number`; only
    `get_extrinsic_identifier()` populates it

Skipped rather than failed when bittensor is absent, so the suite still runs in
environments without the chain stack.
"""
import importlib
import inspect
import sys
import types

import pytest

bittensor = pytest.importorskip("bittensor", reason="pinned bittensor SDK not installed")


def _load_bt_compat():
    sys.modules.pop("utils.bt_compat", None)
    spec = importlib.util.spec_from_file_location("utils.bt_compat", "utils/bt_compat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def transfer_params():
    from bittensor import Subtensor
    return set(inspect.signature(Subtensor.transfer).parameters)


class TestTheSignatureWeActuallyCallAgainst:
    def test_sdk_names_the_destination_parameter_destination_ss58(self, transfer_params):
        """If this fails the SDK has renamed it, and the shim's dual-name call
        needs a third name — not this test relaxed."""
        assert "destination_ss58" in transfer_params

    def test_the_shim_supplies_every_required_parameter(self, transfer_params):
        """Every required parameter of the real signature must be supplied.

        The kwargs are captured from an actual call through bt_compat.transfer
        rather than restated here, so the binding is what is under test. A
        literal written in the test would pass even if the shim stopped sending
        the destination.
        """
        import importlib
        from bittensor import Subtensor

        bt_compat = _load_bt_compat()
        seen = {}

        def transfer(**kwargs):
            seen.update(kwargs)
            return (True, "0xabc")

        bt_compat.transfer(
            types.SimpleNamespace(transfer=transfer),
            wallet="W", dest="5DEST", amount_tao=0.001,
        )

        sig = inspect.signature(Subtensor.transfer)
        required = {
            name for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        }
        # The shim passes both destination names; only the declared one survives
        # its filter, so intersect with what this SDK actually declares.
        supplied = {k for k in seen if k in sig.parameters}
        missing = required - supplied
        assert not missing, (
            f"the shim never supplies {sorted(missing)}; the SDK declares them as "
            f"required, so the call fails at runtime. It passed {sorted(seen)}."
        )
        assert seen.get("destination_ss58") == "5DEST"

class TestExtrinsicResponseIsReadCorrectly:
    """ExtrinsicResponse is tuple-LIKE but is not a tuple, and carries no
    block_hash of its own — the block details live on extrinsic_receipt, and
    `.message` is a human-readable status, never chain data."""

    def _response(self, success=True, message="Success", receipt=None):
        from bittensor.core.types import ExtrinsicResponse
        kwargs = {}
        for name in inspect.signature(ExtrinsicResponse.__init__).parameters:
            if name == "self":
                continue
            kwargs[name] = None
        kwargs.update(success=success, message=message)
        if "extrinsic_receipt" in kwargs:
            kwargs["extrinsic_receipt"] = receipt
        return ExtrinsicResponse(**kwargs)

    def _receipt(self, block_hash="0x" + "ab" * 32, block_number=None, idx=3,
                 resolvable_height=4242):
        """A receipt shaped like the one submit_extrinsic builds.

        It constructs ExtrinsicReceipt(substrate, extrinsic_hash, block_hash,
        finalized) — with no block_number, which only get_extrinsic_identifier()
        ever fills in. Hence the None default: a fake supplying a height the
        transfer path never supplies would hide the code failing to derive it.
        """
        substrate = types.SimpleNamespace(
            get_block_number=lambda _h: resolvable_height
        )
        return types.SimpleNamespace(
            block_hash=block_hash, block_number=block_number,
            extrinsic_idx=idx, substrate=substrate,
        )

    def test_success_is_read_from_the_dataclass_not_from_tuple_position(self):
        bt_compat = _load_bt_compat()
        ok, reason = bt_compat._read_transfer_outcome(self._response(success=True))
        assert ok is True

    def test_failure_is_reported_as_failure(self):
        bt_compat = _load_bt_compat()
        ok, _ = bt_compat._read_transfer_outcome(self._response(success=False, message="no funds"))
        assert ok is False

    def test_locator_is_completed_when_the_receipt_omits_the_height(self):
        """The transfer path leaves block_number None, so it is derived from the
        hash. Reading the attribute alone yields no locator for any payment."""
        bt_compat = _load_bt_compat()
        loc = bt_compat._locator_from_result(self._response(receipt=self._receipt()))
        assert loc == {
            "block_hash": "0x" + "ab" * 32,
            "block_number": 4242,
            "extrinsic_index": 3,
        }

    def test_locator_uses_a_height_the_receipt_already_carries(self):
        bt_compat = _load_bt_compat()
        loc = bt_compat._locator_from_result(
            self._response(receipt=self._receipt(block_number=77, resolvable_height=999))
        )
        assert loc["block_number"] == 77, "an existing height must not be re-fetched"

    def test_no_locator_when_the_height_cannot_be_derived(self):
        bt_compat = _load_bt_compat()
        receipt = types.SimpleNamespace(
            block_hash="0x" + "ab" * 32, block_number=None, extrinsic_idx=3,
            substrate=None,
        )
        assert bt_compat._locator_from_result(self._response(receipt=receipt)) is None

    def test_the_reference_is_the_block_hash_not_the_status_message(self):
        """The reference is handed to a block lookup, so it must be a hash.
        `.message` is the literal string "Success" on this SDK."""
        bt_compat = _load_bt_compat()
        ref = bt_compat._reference_from_result(
            self._response(message="Success", receipt=self._receipt()), "Success"
        )
        assert ref == "0x" + "ab" * 32
        assert ref != "Success"

    def test_the_message_is_never_used_as_a_block_hash(self):
        """`.message` is "Success" on the happy path. Treating it as a block
        reference yields a proof naming a block that does not exist."""
        bt_compat = _load_bt_compat()
        loc = bt_compat._locator_from_result(self._response(message="Success", receipt=None))
        assert loc is None, "a status message was accepted as a block reference"

    def test_incomplete_receipt_yields_no_locator(self):
        bt_compat = _load_bt_compat()
        for bad in (self._receipt(block_hash=None), self._receipt(idx=None)):
            assert bt_compat._locator_from_result(self._response(receipt=bad)) is None

    def test_a_receipt_that_raises_on_read_yields_no_locator(self):
        """extrinsic_idx is a property that can query the chain."""
        bt_compat = _load_bt_compat()

        class RaisesOnRead:
            block_hash = "0x" + "ab" * 32
            block_number = 1
            @property
            def extrinsic_idx(self):
                raise ConnectionError("rpc down")

        assert bt_compat._locator_from_result(self._response(receipt=RaisesOnRead())) is None


class TestCommitAgainstTheRealSdk:
    """10.3 removed Subtensor.commit and exposes only set_commitment, whose wait
    flags default to blocking until finalization."""

    def test_the_sdk_exposes_set_commitment_not_commit(self):
        from bittensor import Subtensor
        assert not hasattr(Subtensor, "commit")
        assert hasattr(Subtensor, "set_commitment")

    def test_the_shim_does_not_wait_for_finalization(self):
        """Left to default this blocks the miner ~15-30s before every
        submission. Inclusion is enough: the commitment is readable at its block
        from then on, and the block number is what the proof needs."""
        bt_compat = _load_bt_compat()
        from bittensor.core.types import ExtrinsicResponse
        seen = {}

        def set_commitment(wallet=None, netuid=None, data=None, **kw):
            seen.update(kw)
            fields = {
                n: None for n in inspect.signature(ExtrinsicResponse.__init__).parameters
                if n != "self"
            }
            fields.update(success=True, message="Success")
            return ExtrinsicResponse(**fields)

        ok, _, _blk = bt_compat.commit(
            types.SimpleNamespace(set_commitment=set_commitment),
            wallet="W", netuid=107, data="m1:abc:dead",
        )
        assert ok is True
        assert seen.get("wait_for_finalization") is False
        assert seen.get("wait_for_inclusion") is True

    def test_the_payload_reaches_the_sdk_intact(self):
        bt_compat = _load_bt_compat()
        seen = {}
        bt_compat.commit(
            types.SimpleNamespace(
                set_commitment=lambda **kw: seen.update(kw) or (True, "")),
            wallet="W", netuid=107, data="m1:4ca39057:deadbeef",
        )
        assert seen["data"] == "m1:4ca39057:deadbeef"


class TestTransferPassesTheDestinationThrough:
    def test_destination_reaches_a_10_3_style_transfer(self):
        """End-to-end through transfer(), against a fake declaring the SDK's own
        parameter names."""
        bt_compat = _load_bt_compat()
        from bittensor.core.types import ExtrinsicResponse

        seen = {}
        receipt = types.SimpleNamespace(
            block_hash="0x" + "cd" * 32, block_number=None, extrinsic_idx=1,
            substrate=types.SimpleNamespace(get_block_number=lambda _h: 77),
        )

        def transfer(*, wallet, destination_ss58, amount, **kw):
            seen.update(destination_ss58=destination_ss58, amount=amount)
            fields = {
                n: None for n in inspect.signature(ExtrinsicResponse.__init__).parameters
                if n != "self"
            }
            fields.update(success=True, message="Success")
            if "extrinsic_receipt" in fields:
                fields["extrinsic_receipt"] = receipt
            return ExtrinsicResponse(**fields)

        sub = types.SimpleNamespace(transfer=transfer)
        ok, _reason, locator = bt_compat.transfer(
            sub, wallet="W", dest="5DEST", amount_tao=0.001
        )
        assert ok is True
        assert seen["destination_ss58"] == "5DEST", "destination never reached the SDK"
        assert locator == {"block_hash": "0x" + "cd" * 32, "block_number": 77, "extrinsic_index": 1}

    def test_amount_is_a_balance_not_a_bare_float(self):
        """Some builds read a bare float as RAO, which would send 1e9x too little."""
        bt_compat = _load_bt_compat()
        captured = {}

        def transfer(*, wallet, destination_ss58, amount, **kw):
            captured["amount"] = amount
            return (True, "ok")

        bt_compat.transfer(
            types.SimpleNamespace(transfer=transfer), wallet="W", dest="5D", amount_tao=0.001
        )
        assert not isinstance(captured["amount"], float)
        assert int(getattr(captured["amount"], "rao", -1)) == 1_000_000


class TestUnsupportedSdkFailsAtImport:
    """A neuron on an SDK this shim cannot bridge must say so at startup.

    bittensor 11 replaced the method-per-operation API with a compose/execute
    builder. Without the guard a neuron starts fine and then fails unevenly:
    the subtensor constructs, current_block silently returns None, and
    get_metagraph raises an unrelated TypeError somewhere far from the cause.
    """

    def _shim_with(self, fake_bt):
        """Load the shim against a stand-in bittensor module."""
        import importlib
        real = sys.modules.get("bittensor")
        sys.modules["bittensor"] = fake_bt
        sys.modules.pop("utils.bt_compat", None)
        try:
            spec = importlib.util.spec_from_file_location("utils.bt_compat", "utils/bt_compat.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            if real is not None:
                sys.modules["bittensor"] = real
            else:
                sys.modules.pop("bittensor", None)
            sys.modules.pop("utils.bt_compat", None)

    def _fake(self, subtensor_attrs, version="11.1.0", **extra):
        mod = types.ModuleType("bittensor")
        mod.__version__ = version
        mod.logging = types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        mod.Subtensor = type("Subtensor", (), {a: (lambda self: None) for a in subtensor_attrs})
        for k, v in extra.items():
            setattr(mod, k, v)
        return mod

    def test_a_subtensor_without_the_core_methods_is_refused(self):
        with pytest.raises(RuntimeError) as caught:
            self._shim_with(self._fake([]))
        message = str(caught.value)
        assert "not supported" in message
        assert "set_weights" in message and "metagraph" in message
        assert "10.3.1" in message, "the message must say what to install"

    def test_a_supported_shape_loads(self):
        module = self._shim_with(self._fake(["set_weights", "metagraph"], version="10.3.0"))
        assert module is not None

    def test_no_subtensor_class_at_all_is_refused(self):
        mod = types.ModuleType("bittensor")
        mod.__version__ = "99.0.0"
        mod.logging = types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        with pytest.raises(RuntimeError, match="no Subtensor class"):
            self._shim_with(mod)

    def test_a_module_under_the_lowercase_name_is_not_mistaken_for_the_class(self):
        """bittensor 11 exposes bt.metagraph and bt.config as MODULES while the
        classes keep the capitalised names. A module is truthy, so preferring
        the lowercase spelling would pick it and callers would get 'module
        object is not callable' far from the cause."""
        fake_module = types.ModuleType("metagraph")
        module = self._shim_with(self._fake(
            ["set_weights", "metagraph"], version="10.3.0",
            metagraph=fake_module,
            Metagraph=type("Metagraph", (), {}),
        ))
        assert module._factory("metagraph", "Metagraph") is not fake_module
        assert inspect.isclass(module._factory("metagraph", "Metagraph"))
