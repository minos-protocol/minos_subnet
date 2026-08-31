"""Bittensor SDK compatibility shim.

The supported SDK is 10.3.x (see requirements.txt); this module absorbs the
remaining shape differences so call sites do not have to. 10.3 is the reference
behaviour: each helper takes the 10.3 form and adapts only where that form is
absent or its signature changed — which also covers the 9.x shapes still found
in older environments. Signature differences are resolved by inspecting the
target's parameters and passing only what it accepts, never by broadly catching
exceptions — a real runtime failure (network error, bad weights, ...) must still
propagate rather than be read as a version mismatch.

NOT 11.x. bittensor 11 replaced the method-per-operation API with a compose /
execute builder: Subtensor has no transfer, set_weights, metagraph,
get_hotkey_owner or set_commitment, and operations are top-level classes
instead. Adapting kwargs cannot bridge that, so ``_assert_supported_sdk`` below
refuses at import rather than let a neuron start and then fail unevenly
mid-round: the subtensor constructs, current_block returns None, and
get_metagraph raises an unrelated TypeError far from the cause.
"""
from __future__ import annotations

import inspect
from typing import Any, Optional, Tuple

import bittensor as bt

# --- Namespace ---------------------------------------------------------------
# Some SDK builds expose only the capitalized classes. Importing this module
# restores the lowercase aliases on the shared ``bt`` module, so existing
# ``bt.subtensor`` / ``bt.wallet`` / ``bt.config`` call sites keep working.
for _lower, _upper in (("subtensor", "Subtensor"), ("wallet", "Wallet"), ("config", "Config")):
    if not hasattr(bt, _lower) and hasattr(bt, _upper):
        setattr(bt, _lower, getattr(bt, _upper))

# --- Logging -----------------------------------------------------------------
# ``bt.logging`` has been stable across versions; expose it, with a stdlib
# fallback so a future rename can't take the neurons down at import time.
logging = getattr(bt, "logging", None)
if logging is None:  # pragma: no cover - defensive
    import logging as _std_logging

    logging = _std_logging.getLogger("minos.bt_compat")


# --- Supported-SDK check ---------------------------------------------------
# Capability-based rather than a version comparison: what matters is whether the
# methods exist, and a version string can be a fork, a dev build, or a patch that
# moved something. Runs at import so an unsupported SDK is a startup failure with
# an actionable message, not a confusing error thirty minutes into a round.
_REQUIRED_SUBTENSOR_METHODS = ("set_weights", "metagraph")


def _assert_supported_sdk() -> None:
    version = getattr(bt, "__version__", "unknown")
    factory = None
    for name in ("subtensor", "Subtensor"):
        candidate = getattr(bt, name, None)
        if inspect.isclass(candidate):
            factory = candidate
            break
    if factory is None:
        raise RuntimeError(
            f"bittensor {version} exposes no Subtensor class; this neuron needs "
            f"bittensor 10.3.x (see requirements.txt)."
        )
    missing = [m for m in _REQUIRED_SUBTENSOR_METHODS if not hasattr(factory, m)]
    if missing:
        raise RuntimeError(
            f"bittensor {version} is not supported by this neuron: "
            f"Subtensor is missing {', '.join(missing)}. Version 11 replaced the "
            f"method API with a compose/execute builder and needs a rewrite, not "
            f"a shim. Install the pinned version:\n"
            f"    pip install 'bittensor==10.3.1'"
        )


_assert_supported_sdk()


def _call_accepted(func, /, **kwargs) -> Any:
    """Call ``func`` with only the kwargs its signature declares.

    Drops arguments another SDK version removed (e.g. ``version_key``) instead of
    raising ``TypeError``; errors raised inside the call are not caught. A target
    with no introspectable signature (C/builtin) receives all kwargs unchanged.
    """
    try:
        params = inspect.signature(func).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (ValueError, TypeError):
        pass  # builtin/C callable — pass through
    return func(**kwargs)


def _factory(lower: str, upper: str):
    """The class behind a name, preferring whichever spelling is actually a class.

    Taking the lowercase name first is wrong on SDKs where it is a MODULE: a
    module is truthy, so it wins, and the caller then gets "'module' object is
    not callable" from somewhere far from the cause. bittensor 11 exposes
    ``bt.metagraph`` and ``bt.config`` as modules while the classes live under
    the capitalised names.
    """
    for name in (lower, upper):
        candidate = getattr(bt, name, None)
        if inspect.isclass(candidate):
            return candidate
    # Nothing is a class under either spelling. Return whatever the upper name
    # holds so the caller fails on the real object rather than on None.
    return getattr(bt, upper, None) or getattr(bt, lower, None)


# --- Construction / argparse -------------------------------------------------
def make_config(parser) -> Any:
    return _factory("config", "Config")(parser)


def add_subtensor_args(parser) -> None:
    _factory("subtensor", "Subtensor").add_args(parser)


def add_wallet_args(parser) -> None:
    _factory("wallet", "Wallet").add_args(parser)


def make_wallet(config=None) -> Any:
    return _call_accepted(_factory("wallet", "Wallet"), config=config)


def make_subtensor(config=None, network: Optional[str] = None) -> Any:
    """Construct a subtensor; the 10.3 path is ``subtensor(config=...)``."""
    factory = _factory("subtensor", "Subtensor")
    if config is not None:
        try:
            return factory(config=config)  # 9.x / 10.3 form
        except TypeError:
            logging.warning("bt_compat: subtensor(config=) rejected; falling back to network=")
    if network is None and config is not None:
        network = getattr(getattr(config, "subtensor", None), "network", None)
    return factory(network=network) if network is not None else factory()


# --- Metagraph ---------------------------------------------------------------
def get_metagraph(subtensor, netuid: int) -> Any:
    """10.3 path: ``subtensor.metagraph(netuid)``; fall back to a bound ctor."""
    meta = getattr(subtensor, "metagraph", None)
    if callable(meta):
        try:
            return meta(netuid)
        except TypeError:
            logging.warning("bt_compat: subtensor.metagraph(netuid) sig changed; using module ctor")
    return _call_accepted(_factory("metagraph", "Metagraph"), netuid=netuid, subtensor=subtensor)


def sync_metagraph(metagraph, subtensor) -> None:
    """Sync a metagraph against the configured subtensor.

    The argument is resolved by signature inspection rather than by catching
    ``TypeError`` around a network call. Deliberately never falls back to a bare
    ``metagraph.sync()``, which would sync against the SDK's default network
    instead of the configured one; raises instead.
    """
    if "subtensor" not in inspect.signature(metagraph.sync).parameters:
        raise TypeError(
            "metagraph.sync() does not accept 'subtensor'; refusing to sync "
            "against the SDK default network — update bt_compat for this SDK"
        )
    return metagraph.sync(subtensor=subtensor)


# --- Chain reads (best-effort; callers already treat None as "unknown") ------
def current_block(subtensor) -> Optional[int]:
    if hasattr(subtensor, "get_current_block"):
        return subtensor.get_current_block()  # 10.3
    return getattr(subtensor, "block", None)  # 11 exposes a .block property


def commit_reveal_enabled(subtensor, netuid: int) -> Optional[bool]:
    fn = getattr(subtensor, "commit_reveal_enabled", None)
    return fn(netuid) if callable(fn) else None


def blocks_since_last_update(subtensor, netuid: int, uid: int) -> Optional[int]:
    fn = getattr(subtensor, "blocks_since_last_update", None)
    return _call_accepted(fn, netuid=netuid, uid=uid) if callable(fn) else None


def weights_rate_limit(subtensor, netuid: int) -> Optional[int]:
    fn = getattr(subtensor, "weights_rate_limit", None)
    return _call_accepted(fn, netuid=netuid) if callable(fn) else None


# --- Chain writes ------------------------------------------------------------
def set_weights(
    subtensor,
    *,
    wallet,
    netuid: int,
    uids,
    weights,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
    version_key: int = 0,
) -> Tuple[bool, str]:
    """Set weights and normalize the result to ``(success, message)``.

    10.3 returns ``(success, msg)``; some builds return an object with
    ``.success`` or a bare bool — normalized here so callers keep unpacking.
    """
    result = _call_accepted(
        subtensor.set_weights,
        wallet=wallet,
        netuid=netuid,
        uids=uids,
        weights=weights,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
        version_key=version_key,
    )
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1])
    if hasattr(result, "success"):
        msg = getattr(result, "message", None) or getattr(result, "error_message", "") or ""
        return bool(result.success), str(msg)
    return bool(result), ""


def register(
    subtensor,
    *,
    wallet,
    netuid: int,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = True,
) -> bool:
    """Register a hotkey; normalize the result to a bool across versions."""
    result = _call_accepted(
        subtensor.register,
        wallet=wallet,
        netuid=netuid,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
    )
    return result.success if hasattr(result, "success") else bool(result)


def commit(subtensor, *, wallet, netuid: int, data: str) -> Tuple[bool, str, Optional[int]]:
    """Publish a commitment on chain. Returns ``(ok, reason, block)``.

    ``block`` is the height the commitment was INCLUDED at, read off the
    extrinsic receipt, or None when it cannot be determined. It must not be
    taken from the chain head before submitting: the extrinsic lands a block or
    more later, so a height read beforehand names a block the commitment is not
    in yet, and anything verifying it there finds nothing.

    10.3 exposes ``subtensor.commit(wallet, netuid, data)``; other versions name
    it ``set_commitment``. Resolved by signature inspection, so a renamed kwarg
    is dropped rather than raising.

    Unlike the rest of this module this helper catches: a commitment is
    supplementary evidence, not a precondition for mining, so chain failures are
    returned as ``(False, reason)`` for the caller to log and continue past.

    Note 10.3 exposes only ``set_commitment``; ``commit`` was removed, which the
    lookup above covers.
    """
    fn = getattr(subtensor, "commit", None) or getattr(subtensor, "set_commitment", None)
    if not callable(fn):
        return False, "SDK exposes neither commit() nor set_commitment()", None
    try:
        result = _call_accepted(
            fn,
            wallet=wallet,
            netuid=netuid,
            data=data,
            # Set explicitly rather than inherited. set_commitment defaults to
            # wait_for_finalization=True, which blocks the miner for ~15-30s
            # before every submission. Inclusion is enough: the commitment is
            # readable at its block from that point, and the block number is
            # what the proof needs. A reorg would drop it, which costs an
            # unverifiable commitment rather than a wrong one — and the
            # commitment is evidence, not a precondition.
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
    except TypeError:
        # Some builds take these positionally only.
        try:
            result = fn(wallet, netuid, data)
        except Exception as e:  # noqa: BLE001 - best effort by design
            return False, f"{type(e).__name__}: {e}", None
    except Exception as e:  # noqa: BLE001 - best effort by design
        return False, f"{type(e).__name__}: {e}", None

    block = _included_block_from_result(result)
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1]), block
    if hasattr(result, "success"):
        msg = getattr(result, "message", None) or getattr(result, "error_message", "") or ""
        return bool(result.success), str(msg), block
    # Several SDK builds return None on success and raise on failure.
    return (True, "", block) if result is None else (bool(result), "", block)


def _included_block_from_result(result: Any) -> Optional[int]:
    """The height an extrinsic was included at, from its receipt.

    Read from the result rather than from the chain head, because the head
    moves: a height sampled before submitting names a block the extrinsic is
    not in.
    """
    receipt = getattr(result, "extrinsic_receipt", None)
    if receipt is None:
        return None
    try:
        number = getattr(receipt, "block_number", None)
        if number is None:
            block_hash = getattr(receipt, "block_hash", None)
            substrate = getattr(receipt, "substrate", None)
            getter = getattr(substrate, "get_block_number", None)
            if block_hash and callable(getter):
                number = getter(block_hash)
        return int(number) if number is not None else None
    except Exception as e:  # noqa: BLE001 - these read the chain
        logging.warning(f"bt_compat: could not read the commitment block: {type(e).__name__}: {e}")
        return None


AMBIGUOUS = "ambiguous:outcome-unknown"


def transfer(
    subtensor, *, wallet, dest: str, amount_tao: float
) -> Tuple[bool, str, Optional[dict]]:
    """Transfer TAO. Returns ``(ok, reason, locator)``.

    ``locator`` is ``{block_hash, block_number, extrinsic_index}`` — everything
    the platform needs to verify the payment — or None when it could not be
    determined. ``ok=True`` with ``locator=None`` means the money moved but we
    cannot prove it; callers must NOT retry, since a transfer cannot be undone.

    ``ok=False`` with ``reason == AMBIGUOUS`` means the outcome is unknown.

    The destination is passed under BOTH names: 10.3 declares
    ``destination_ss58``, older builds declare ``dest``. _call_accepted keeps
    only the one the installed signature actually declares, so this is correct on
    both without a version check — and passing only the wrong one is silently
    dropped and then fails as a missing required argument, which is exactly the
    bug this shape prevents.

    The amount is passed as a ``Balance``: some SDK versions read a bare float as
    RAO rather than TAO. Without ``Balance.from_tao`` this refuses rather than
    guess the unit.

    Like commit(), failures are returned rather than raised, so an unpaid
    transfer surfaces as "not paid" instead of taking the miner down mid-round.
    """
    fn = getattr(subtensor, "transfer", None)
    if not callable(fn):
        return False, "SDK exposes no transfer()", None

    balance_cls = getattr(bt, "Balance", None) or getattr(
        getattr(bt, "utils", None), "Balance", None
    )
    if balance_cls is None or not hasattr(balance_cls, "from_tao"):
        return False, "no Balance.from_tao in this SDK; refusing to guess TAO vs RAO units", None
    try:
        amount = balance_cls.from_tao(amount_tao)
    except Exception as e:  # noqa: BLE001
        return False, f"could not build Balance: {type(e).__name__}: {e}", None

    try:
        result = _call_accepted(
            fn,
            wallet=wallet,
            destination_ss58=dest,
            dest=dest,
            amount=amount,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
    except Exception as e:  # noqa: BLE001 - best effort by design
        return False, f"{type(e).__name__}: {e}", None

    ok, reason = _read_transfer_outcome(result)
    if not ok:
        return False, reason, None
    return True, _reference_from_result(result, reason), _locator_from_result(result)


def _read_transfer_outcome(result: Any) -> Tuple[bool, str]:
    """Normalise a transfer result to ``(ok, reason)`` across SDK versions.

    A reported failure is only treated as a CLEAN failure — one the caller may
    settle and move on from — when nothing suggests the extrinsic reached the
    chain. 10.3's transfer_extrinsic wraps its whole body in `except Exception`
    and returns ExtrinsicResponse.from_exception(...), including for calls it
    makes AFTER the extrinsic was already included (it re-reads the block hash
    and the balance). So an RPC hiccup at that moment is reported as
    success=False even though the TAO moved.

    Treating that as a clean failure is what makes the caller record "failed",
    become free to retry, and transfer a second time. So any evidence of
    submission — a receipt, or a signed extrinsic on the response — turns a
    reported failure into AMBIGUOUS instead, which the caller must never retry.
    """
    if hasattr(result, "success"):
        message = getattr(result, "message", None) or getattr(result, "error", None) or ""
        if bool(result.success):
            return True, str(message)
        if _looks_submitted(result):
            logging.warning(
                "bt_compat: transfer reported failure but the extrinsic appears to "
                "have been submitted; treating the outcome as UNKNOWN so it is not "
                f"retried ({message})"
            )
            return False, AMBIGUOUS
        return False, str(message)
    if isinstance(result, tuple) and len(result) >= 1:
        return bool(result[0]), str(result[1]) if len(result) > 1 else ""
    if isinstance(result, bool):
        return result, ""
    if result is None:
        # Documented to return bool, but several builds return None on success:
        # unreadable, so do not guess.
        return False, AMBIGUOUS
    return bool(result), ""


def _looks_submitted(result: Any) -> bool:
    """Whether a failed result still shows signs of having reached the chain.

    A pre-submission failure (locked wallet, insufficient balance, bad address)
    carries neither a receipt nor an extrinsic. One that failed only while
    reading back the result carries at least one of them.
    """
    receipt = getattr(result, "extrinsic_receipt", None)
    if receipt is not None and getattr(receipt, "block_hash", None):
        return True
    return getattr(result, "extrinsic", None) is not None


def _locator_from_result(result: Any) -> Optional[dict]:
    """Pull {block_hash, block_number, extrinsic_index} off a transfer result.

    On 10.3 the transfer path builds its receipt as
    ``ExtrinsicReceipt(substrate, extrinsic_hash, block_hash, finalized)`` — with
    NO block_number. The attribute exists but is None, and is only ever filled in
    by ``get_extrinsic_identifier()``, which nothing on this path calls. So the
    height has to be derived from the hash here; reading the attribute and giving
    up on None yields no locator for any real payment.

    ``extrinsic_idx`` is a property that lazily fetches the block, so it can
    raise or block; both are handled as "no locator" rather than propagating.

    Returns None when it cannot be completed. The caller then treats the payment
    as paid-but-unprovable and refuses to retry, which is correct but costly, so
    every field is worth some effort to recover here.
    """
    receipt = getattr(result, "extrinsic_receipt", None)
    if receipt is None:
        return None
    try:
        block_hash = getattr(receipt, "block_hash", None)
        if not block_hash:
            return None

        block_number = getattr(receipt, "block_number", None)
        if block_number is None:
            # Not populated on the transfer path — derive it from the hash.
            substrate = getattr(receipt, "substrate", None)
            getter = getattr(substrate, "get_block_number", None)
            if callable(getter):
                block_number = getter(block_hash)
            else:
                # Last resort: this populates block_number as a side effect.
                identifier = getattr(receipt, "get_extrinsic_identifier", None)
                if callable(identifier):
                    identifier()
                    block_number = getattr(receipt, "block_number", None)

        index = getattr(receipt, "extrinsic_idx", None)
    except Exception as e:  # noqa: BLE001 - these read the chain and may fail
        logging.warning(
            f"bt_compat: could not complete the extrinsic receipt: {type(e).__name__}: {e}"
        )
        return None

    if block_number is None or index is None:
        logging.warning(
            "bt_compat: receipt is missing block_number or extrinsic_idx; no locator"
        )
        return None
    try:
        return {
            "block_hash": str(block_hash),
            "block_number": int(block_number),
            "extrinsic_index": int(index),
        }
    except (TypeError, ValueError):
        return None


def _reference_from_result(result: Any, fallback: str) -> str:
    """A block hash to hand the locate_transfer fallback.

    ExtrinsicResponse.message is the literal string "Success" on this SDK, so
    returning it as the reference makes the fallback look up a block named
    "Success". Prefer the receipt's real block_hash.
    """
    receipt = getattr(result, "extrinsic_receipt", None)
    block_hash = getattr(receipt, "block_hash", None) if receipt is not None else None
    return str(block_hash) if block_hash else fallback



def locate_transfer(
    subtensor,
    *,
    block_hash: str,
    signer_ss58: str,
    dest: str,
    amount_tao: float,
) -> Optional[dict]:
    """Find our transfer inside its block. Returns a locator, or None.

    A payment proof must name the extrinsic, not just the block: a block holds
    many transfers and the platform verifies the one at the index we give it.
    The index is not in the transfer result, so it is recovered here by matching
    the extrinsic on signer, destination and amount.

    Returns None rather than guessing when the block cannot be read or no
    extrinsic matches. The caller treats that as "paid but unprovable" and
    leaves the payment for manual reconciliation, which is the safe direction:
    a wrong index is a proof the platform will reject.
    """
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        logging.warning("bt_compat: no substrate handle; cannot locate the transfer extrinsic")
        return None

    balance_cls = getattr(bt, "Balance", None) or getattr(getattr(bt, "utils", None), "Balance", None)
    try:
        expected_rao = int(balance_cls.from_tao(amount_tao).rao)
    except Exception:  # noqa: BLE001
        expected_rao = int(round(amount_tao * 1_000_000_000))

    def _addr(value) -> str:
        if isinstance(value, dict):
            value = value.get("Id") or value.get("id") or ""
        return str(value or "").strip()

    try:
        block = substrate.get_block(block_hash=block_hash)
    except Exception as e:  # noqa: BLE001 - best effort by design
        logging.warning(f"bt_compat: could not read block {block_hash[:12]}...: {type(e).__name__}: {e}")
        return None

    number = None
    header = (block or {}).get("header") or {}
    if isinstance(header, dict):
        try:
            number = int(header.get("number"))
        except (TypeError, ValueError):
            number = None

    for index, extrinsic in enumerate((block or {}).get("extrinsics") or []):
        value = extrinsic.value if hasattr(extrinsic, "value") else extrinsic
        if not isinstance(value, dict):
            continue
        call = value.get("call") or {}
        if str(call.get("call_module", "")) != "Balances":
            continue
        if not str(call.get("call_function", "")).startswith("transfer"):
            continue
        args = call.get("call_args") or []
        if isinstance(args, list):
            args = {a.get("name"): a.get("value") for a in args if isinstance(a, dict)}
        if _addr(args.get("dest")) != _addr(dest):
            continue
        try:
            if int(args.get("value")) != expected_rao:
                continue
        except (TypeError, ValueError):
            continue
        if _addr(value.get("address") or value.get("signer")) != _addr(signer_ss58):
            continue
        if number is None:
            logging.warning("bt_compat: found the transfer but the block header has no number")
            return None
        return {"block_hash": block_hash, "block_number": number, "extrinsic_index": index}

    logging.warning("bt_compat: our transfer was not found in the block it reported")
    return None
