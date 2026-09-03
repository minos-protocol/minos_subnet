"""Paid resubmission: one free config submission per round, pay for the rest.

Paid resubmission is disabled by default. It requires both network support
(``resubmission_fee_enabled`` in /scoring/network-config) and an explicit
operator opt-in (``MINER_PAY_FOR_RESUBMISSIONS``); neither alone moves TAO.

Fee, destination and free allowance are read from network configuration. With
nothing advertised this module reports "no payment required". An advertised
allowance of zero is not honoured without a further local opt-in — see
zero_free_allowance_permitted.

Ordering contract. A payment is real money and the submission it buys can still
fail, so these steps must stay in this order:

    1. record the intent locally, fsynced
    2. transfer on chain
    3. record the proof, fsynced
    4. submit, attaching the proof
    5. mark the proof spent only once the platform accepts it

Step 3 must land before step 4: an unspent proof is reusable on the next
attempt, so a miner never pays and gets nothing.

This is the client half only. Verifying a proof and rejecting a replayed one is
the platform's side.
"""
from __future__ import annotations

import json
import logging
import contextlib
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Free submissions per round when the platform advertises a policy but omits it.
# Also the floor: an advertised allowance of 0 or below falls back to this
# unless zero_free_allowance_permitted().
DEFAULT_FREE_SUBMISSIONS = 1

# Hard per-submission ceiling, enforced locally. Payment limits are kept on the
# miner side so an operator sets their own maximum; raising it is a deliberate
# local change.
DEFAULT_MAX_FEE_TAO = 0.01
# 24h aggregate ceiling. Deliberately small: a miner paying more than this in
# one day is far more likely to be looping than competing.
DEFAULT_MAX_DAILY_TAO = 0.05
# Ceiling on what EVERY hotkey sharing this ledger may spend in a rolling 24h.
# The per-hotkey cap bounds one miner; this one keeps a host running K hotkeys
# from multiplying that ceiling by K.
DEFAULT_MAX_DAILY_WALLET_TAO = 0.10


def max_fee_tao() -> float:
    """The hard ceiling on a single resubmission fee, in TAO.

    Read from MINOS_MAX_RESUBMISSION_FEE_TAO. A non-finite or negative override
    falls back to DEFAULT_MAX_FEE_TAO: nan compares False against every fee and
    would act as no ceiling at all.
    """
    raw = os.getenv("MINOS_MAX_RESUBMISSION_FEE_TAO")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_FEE_TAO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FEE_TAO
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "MINOS_MAX_RESUBMISSION_FEE_TAO=%r is not a usable ceiling; "
            "falling back to %s TAO", raw, DEFAULT_MAX_FEE_TAO
        )
        return DEFAULT_MAX_FEE_TAO
    return value


def zero_free_allowance_permitted() -> bool:
    """Whether an advertised allowance of ZERO free submissions is honoured.

    Off by default. A zero allowance charges for the round's first
    submission, so honouring it stays a local decision rather than one the
    advertised policy can make on the operator's behalf.
    """
    return os.getenv("MINER_ALLOW_ZERO_FREE_SUBMISSIONS", "").lower() in ("1", "true", "yes")


def pinned_destination() -> Optional[str]:
    """The destination this operator will pay, if they pinned one.

    The fee and address arrive in the network-config response. An operator who
    pins the address here will pay that address and no other: any different
    destination stops the payment rather than receiving it.

    Unset means "accept the advertised destination", which is the status quo.
    Pinning is the stronger setting, and is what an unattended miner holding a
    funded coldkey should use.

    Read from MINOS_EXPECTED_PAYMENT_ADDRESS.
    """
    raw = os.getenv("MINOS_EXPECTED_PAYMENT_ADDRESS", "").strip()
    return raw or None


def payment_opted_in() -> bool:
    """Whether this operator has explicitly agreed to pay for resubmissions.

    Spending is off unless the operator turns it on. Network configuration
    determines whether resubmissions carry a fee and what it is, but network
    configuration alone never moves TAO: a payment also requires this local
    opt-in, so an unattended or auto-updating miner does not begin spending on
    its own.

    ``max_fee_tao()`` and the daily ceilings bound what an opted-in miner can
    spend. They are limits, not authorisation; neither replaces this switch.

    Set MINER_PAY_FOR_RESUBMISSIONS to 1/true/yes/on to take part. Anything
    else, including unset, means no payment and no resubmission beyond the free
    allowance.
    """
    return os.getenv("MINER_PAY_FOR_RESUBMISSIONS", "").strip().lower() in ("1", "true", "yes", "on")


def max_daily_tao() -> float:
    """Hard ceiling on TOTAL resubmission spend in a rolling 24h, in TAO.

    The per-submission ceiling bounds one payment; it does not bound repeated
    payments across a day. With ~20 rounds a day and several submissions each,
    the total can exceed what any single fee suggests.

    Spending already requires the local opt-in in ``payment_opted_in()``; this
    ceiling bounds the total once that opt-in is set.

    Read from MINOS_MAX_DAILY_RESUBMISSION_TAO. A non-finite or negative
    override falls back to the default, for the same reason as max_fee_tao().
    """
    raw = os.getenv("MINOS_MAX_DAILY_RESUBMISSION_TAO")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_DAILY_TAO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DAILY_TAO
    if not math.isfinite(value) or value < 0:
        return DEFAULT_MAX_DAILY_TAO
    return value


def _looks_like_ss58(addr: str) -> bool:
    """Reject a destination that is not plausibly an ss58 address.

    Uses the wallet validator when importable, else a structural check: a
    malformed destination means an irrecoverable transfer.
    """
    if not addr or not isinstance(addr, str):
        return False
    try:
        from bittensor_wallet.utils import is_valid_ss58_address  # type: ignore
        return bool(is_valid_ss58_address(addr))
    except Exception:
        pass
    _B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return 46 <= len(addr) <= 49 and set(addr) <= _B58


class SubmissionPolicy:
    """Fee policy as advertised by the platform."""

    def __init__(self, raw: Optional[Dict[str, Any]], quoted_fee_tao: Optional[float] = None):
        raw = raw or {}
        self.enabled = bool(raw.get("resubmission_fee_enabled", False))
        # Parsed defensively: this value decides when the miner starts
        # spending, and a malformed field must not raise out of the submission
        # path.
        try:
            self.free_submissions = int(
                raw.get("free_submissions_per_round", DEFAULT_FREE_SUBMISSIONS)
            )
        except (TypeError, ValueError):
            self.free_submissions = DEFAULT_FREE_SUBMISSIONS
        if self.free_submissions < 0:
            self.free_submissions = DEFAULT_FREE_SUBMISSIONS
        elif self.free_submissions == 0 and not zero_free_allowance_permitted():
            # A zero allowance must not start charging for the round's first
            # submission without the operator's opt-in.
            logger.warning(
                "Platform advertises free_submissions_per_round=0, which would "
                "charge a fee for this round's first submission; using %s instead. "
                "Set MINER_ALLOW_ZERO_FREE_SUBMISSIONS=1 to honour a zero allowance.",
                DEFAULT_FREE_SUBMISSIONS,
            )
            self.free_submissions = DEFAULT_FREE_SUBMISSIONS
        # The platform quotes the price for THIS hotkey's next paid submission
        # in round-status. It is authoritative: the fee escalates with the
        # owning coldkey's paid submissions this round, so the advertised base
        # fee is only correct for the first one. Paying the base fee for a later
        # submission underpays, and the transfer is already on chain when the
        # platform refuses it.
        #
        # Falls back to the advertised fee when no quote was given — an older
        # platform, or the first paid submission of the round.
        fee = raw.get("resubmission_fee_tao")
        if quoted_fee_tao is not None:
            fee = quoted_fee_tao
        try:
            self.fee_tao = float(fee) if fee is not None else None
        except (TypeError, ValueError):
            self.fee_tao = None
        self.destination = raw.get("resubmission_payment_address") or None

    @property
    def usable(self) -> bool:
        """Whether this policy may move TAO. Every condition must hold.

        Two independent gates, deciding different questions:

        1. WHETHER THE NETWORK CHARGES -- ``resubmission_fee_enabled`` from the
           platform. A protocol question, kept in one place so miners cannot
           disagree about it. Absent reads as off, so an older platform that
           does not serve the field is treated as not charging.
        2. WHETHER THIS OPERATOR TAKES PART -- ``payment_opted_in()``. A consent
           question about their wallet, which cannot live on the platform
           because the platform is the party being paid.

        Neither substitutes for the other: the platform cannot spend an
        operator's TAO, and an operator cannot pay a fee the network is not
        charging. The remaining checks bound what a policy that passes both may
        cost -- a price and a well-formed destination must be present, because a
        guessed destination is an irrecoverable transfer to the wrong account,
        and a fee over the ceiling is refused outright.
        """
        # Gate 1: the network charges.
        if not (self.enabled and self.fee_tao is not None and self.fee_tao > 0):
            return False
        # Gate 2: this operator agreed to pay.
        if not payment_opted_in():
            return False
        if not _looks_like_ss58(self.destination or ""):
            return False
        pinned = pinned_destination()
        if pinned and self.destination != pinned:
            # A well-formed address is not the same as the RIGHT address. Refuse
            # rather than pay a destination the operator did not sanction.
            logger.error(
                "Refusing to pay: the advertised destination %s does not match "
                "the pinned MINOS_EXPECTED_PAYMENT_ADDRESS %s",
                self.destination, pinned,
            )
            return False
        if self.fee_tao > max_fee_tao():
            # Refuse rather than clamp: paying a capped amount toward a bad
            # fee is still paying.
            return False
        return True

    def payment_required(self, submissions_already_made: int) -> bool:
        """True once the round's free allowance is used up.

        The allowance is the effective one — __init__ has already applied the
        zero-allowance floor.
        """
        if not self.usable:
            return False
        return submissions_already_made >= max(0, self.free_submissions)

    def __repr__(self) -> str:
        return (
            f"SubmissionPolicy(enabled={self.enabled}, free={self.free_submissions}, "
            f"fee_tao={self.fee_tao}, dest={'set' if self.destination else 'unset'})"
        )


def max_daily_wallet_tao() -> float:
    """Hard ceiling on resubmission spend across ALL hotkeys sharing one ledger.

    ``max_daily_tao()`` bounds a single hotkey. An operator running several
    hotkeys on one host multiplies that ceiling by the number of hotkeys, which
    is not what "a 0.05 TAO daily cap" reads as. This bounds the host.

    Scope: the ledger is a file on this machine, so this ceiling is
    per-machine, not per-coldkey. Hotkeys of the same coldkey on other hosts
    keep their own ledgers and their own ceilings. A true coldkey-wide cap needs
    accounting this side of the wallet cannot see.

    Read from MINOS_MAX_DAILY_WALLET_TAO. A non-finite or negative override
    falls back to the default, for the same reason as max_fee_tao().
    """
    raw = os.getenv("MINOS_MAX_DAILY_WALLET_TAO")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_DAILY_WALLET_TAO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DAILY_WALLET_TAO
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "MINOS_MAX_DAILY_WALLET_TAO=%r is not a usable amount; "
            "falling back to %s TAO", raw, DEFAULT_MAX_DAILY_WALLET_TAO
        )
        return DEFAULT_MAX_DAILY_WALLET_TAO
    return value


class PaymentLedger:
    """Durable record of payments made, so a paid-for slot survives a crash.

    Entries are keyed by (round_id, hotkey). ``spent`` marks a proof the platform
    has already accepted; an unspent proof is reused rather than paying twice.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(
            path
            or os.getenv("MINOS_PAYMENT_LEDGER")
            or (Path.home() / ".minos" / "submission_payments.jsonl")
        )

    def _append(self, entry: Dict[str, Any]) -> None:
        # 0o700 so a permissive umask cannot leave the directory holding
        # secrets world-traversable; exist_ok leaves an existing mode alone.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # os.open with 0600 rather than open()+chmod, which would create the
        # file under the umask first and expose it until the chmod lands.
        # O_NOFOLLOW refuses a symlink planted at this path.
        fd = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            self.path.chmod(0o600)  # tighten a pre-existing file
        except OSError:
            pass

    def record_intent(self, round_id: str, hotkey: str, fee_tao: float, dest: str) -> None:
        """Written BEFORE the transfer, so an interrupted payment is traceable."""
        self._append({
            "kind": "intent", "round_id": round_id, "hotkey": hotkey,
            "fee_tao": fee_tao, "destination": dest, "ts": time.time(),
        })

    def record_proof(self, round_id: str, hotkey: str, proof: Dict[str, Any]) -> None:
        """Written BEFORE the submission that spends it."""
        self._append({
            "kind": "proof", "round_id": round_id, "hotkey": hotkey,
            "proof": proof, "ts": time.time(),
        })

    def record_failure(self, round_id: str, hotkey: str, reason: str) -> None:
        """A transfer that clearly did NOT move money, settling its intent.

        Only for readable failures. An ambiguous outcome must leave the intent
        unsettled — see unresolved_intent.
        """
        self._append({
            "kind": "failed", "round_id": round_id, "hotkey": hotkey,
            "reason": reason, "ts": time.time(),
        })

    def mark_spent(self, round_id: str, hotkey: str, proof: Dict[str, Any]) -> None:
        self._append({
            "kind": "spent", "round_id": round_id, "hotkey": hotkey,
            "proof": proof, "ts": time.time(),
        })

    @staticmethod
    def _proof_key(proof: Any) -> str:
        """Identity of one payment.

        proof_id distinguishes payments that agree on every other field. Ledger
        lines without one fall back to the whole-object form so their spent
        status survives.
        """
        if isinstance(proof, dict) and proof.get("proof_id"):
            return f"id:{proof['proof_id']}"
        return "json:" + json.dumps(proof, sort_keys=True, default=str)

    def record_spend_without_proof(
        self, round_id: str, hotkey: str, amount_tao: float, detail: str
    ) -> None:
        """Record TAO that left the wallet without producing a usable proof.

        Written so the 24h ceiling sees it. Deliberately NOT a "proof" line:
        unspent_proof must never hand this to a submission, because there is
        nothing here the platform could verify.
        """
        self._append({
            "kind": "spent_unprovable",
            "round_id": round_id,
            "hotkey": hotkey,
            "spend": {"amount_tao": amount_tao, "paid_at": int(time.time())},
            "detail": detail[:200],
        })

    def spend_since(self, cutoff_ts: int, hotkey: Optional[str] = None) -> float:
        """Total TAO recorded as paid at or after ``cutoff_ts``.

        Counts money we know LEFT the wallet: recorded proofs, and transfers
        that completed without yielding a usable proof. Intents are excluded —
        an intent may never have moved money, and counting it would block a
        miner over a payment that never happened.

        Filtered to ``hotkey`` when given. The ledger is shared by every hotkey
        on the host, so an unfiltered sum makes an operator running K hotkeys hit
        the ceiling K times sooner and silently stop submitting. The allowance is
        per hotkey, so the ceiling that guards it is too.

        Passing None sums the whole ledger, which is what an operator wants when
        asking "what has this machine spent".

        This feeds a safety ceiling, so it must never under-report. A payment
        record it cannot read is money it cannot rule out, and skipping one
        would raise the remaining allowance — the wrong direction. So anything
        that leaves the ledger not fully accounted for returns ``inf``, which
        makes every ceiling comparison refuse rather than permit. Recovering is
        the operator's call: inspect or move the file.

        Records this function is not summing — other hotkeys when filtering,
        and intents, which may never have moved money — are skipped normally.
        Only unreadable PAYMENT records are unaccountable.
        """
        if not self.path.exists():
            return 0.0
        total = 0.0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        return self._unaccountable("a line is not valid JSON")
                    kind = entry.get("kind")
                    # "spent_unprovable" is money that left the wallet without
                    # yielding a usable proof. Excluding it would make the
                    # ceiling blind to precisely the failures that cost the most,
                    # since no proof line is ever written for them.
                    if kind not in ("proof", "spent_unprovable"):
                        continue
                    entry_hotkey = entry.get("hotkey")
                    if hotkey is not None:
                        if not entry_hotkey:
                            return self._unaccountable(
                                "a payment record carries no hotkey, so it cannot be "
                                "attributed when filtering"
                            )
                        if entry_hotkey != hotkey:
                            continue
                    proof = entry.get("proof") or entry.get("spend") or {}
                    if not isinstance(proof, dict):
                        return self._unaccountable(
                            "a payment record's proof is not an object"
                        )
                    try:
                        paid_at = int(proof.get("paid_at", 0))
                        amount = float(proof.get("amount_tao", 0) or 0)
                    except (TypeError, ValueError):
                        return self._unaccountable(
                            "a payment record has an unreadable amount or timestamp"
                        )
                    if paid_at >= cutoff_ts and amount > 0:
                        total += amount
        except (OSError, UnicodeDecodeError) as exc:
            return self._unaccountable(f"the ledger could not be read ({exc})")
        return total

    def _unaccountable(self, reason: str) -> float:
        """Report unbounded spend so every ceiling refuses.

        Returned instead of a partial sum whenever the ledger cannot be fully
        accounted for. A partial sum would look like room left under the cap.
        """
        logger.error(
            "Payment ledger %s cannot be fully accounted for: %s. Reporting "
            "unlimited spend, so the daily ceilings will refuse further paid "
            "submissions. Inspect or move the file to resume paying.",
            self.path, reason,
        )
        return float("inf")

    def unspent_proof(self, round_id: str, hotkey: str) -> Optional[Dict[str, Any]]:
        """A proof already paid for but not yet accepted — reuse it, don't re-pay."""
        if not self.path.exists():
            return None
        proof = None
        spent = set()
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                # A line parsing to a non-object is corruption, not an entry;
                # skip it like an unparseable one.
                if not isinstance(e, dict):
                    continue
                if e.get("round_id") != round_id or e.get("hotkey") != hotkey:
                    continue
                if e.get("kind") == "proof":
                    proof = e.get("proof")
                elif e.get("kind") == "spent":
                    spent.add(self._proof_key(e.get("proof")))
        if proof and self._proof_key(proof) not in spent:
            return proof
        return None

    def unresolved_intent(self, round_id: str, hotkey: str) -> bool:
        """An intent was written but no proof followed — outcome UNKNOWN.

        record_intent lands before the transfer, so this state means a transfer
        was attempted and never resolved either way. It blocks further spending
        on the round; an operator settles it against the chain.
        """
        if not self.path.exists():
            return False
        intents = 0
        settled = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                if e.get("round_id") != round_id or e.get("hotkey") != hotkey:
                    continue
                if e.get("kind") == "intent":
                    intents += 1
                # A proof settles an intent (money moved) and so does a
                # recorded failure (money did not); only an intent with
                # neither is unknown.
                elif e.get("kind") in ("proof", "failed"):
                    settled += 1
        return intents > settled


class PaymentSignerMismatch(Exception):
    """The wallet that would sign the fee does not own the hotkey submitting."""


def preflight_payment_signer(*, subtensor, wallet, hotkey: str, logger=None) -> bool:
    """Check we may pay before we do. False means do not transfer.

    A payment is accepted only when the signing coldkey owned the submitting
    hotkey at the payment block. That check runs after the transfer, so the
    same check is mirrored here beforehand: an on-chain transfer cannot be
    reversed, so ownership is verified before spending.

    A wallet or chain read that fails returns False. Refusing to pay when
    ownership cannot be established is recoverable; paying when it cannot is not.
    """
    try:
        signer = str(wallet.coldkeypub.ss58_address).strip()
    except Exception as e:  # noqa: BLE001
        if logger:
            logger.error(
                f"Refusing to pay: cannot read the wallet coldkey "
                f"({type(e).__name__}: {e}). Is the coldkeypub present?"
            )
        return False

    try:
        owner = subtensor.get_hotkey_owner(hotkey)
    except Exception as e:  # noqa: BLE001
        if logger:
            logger.error(
                f"Refusing to pay: could not read the on-chain owner of this hotkey "
                f"({type(e).__name__}: {e}). Not paying a fee that may be refused."
            )
        return False

    owner = str(owner or "").strip()
    if not owner:
        if logger:
            logger.error(
                f"Refusing to pay: hotkey {hotkey[:16]}... has no on-chain owner. "
                f"Is it registered on this network?"
            )
        return False
    if owner != signer:
        if logger:
            logger.error(
                f"Refusing to pay: this wallet's coldkey ({signer[:16]}...) does not "
                f"own hotkey {hotkey[:16]}... (owned by {owner[:16]}...). The fee "
                f"would be rejected. Pay from the owning coldkey's wallet."
            )
        return False
    return True


@contextlib.contextmanager
def _wallet_spend_lock(ledger: "PaymentLedger"):
    """Serialise spending across PROCESSES sharing one ledger.

    Everything the payment path relies on — the unspent-proof reuse, the
    unresolved-intent guard, the 24h ceiling — is a read of the ledger followed
    by a transfer. Two processes on one wallet (a pm2 duplicate, a restarted
    miner whose predecessor has not exited, two hotkeys on one host) interleave
    those and both transfer: the guards see the file before either wrote to it.

    An advisory flock on a sidecar file. Sidecar rather than the ledger itself so
    the lock is unaffected by the ledger being rotated or replaced underneath us.

    Best effort by design: a platform without flock, or a filesystem that refuses
    it, yields an unlocked path rather than blocking the miner. That is the same
    exposure as before this existed, never worse.
    """
    path = ledger.path.with_suffix(ledger.path.suffix + ".lock")
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception as e:  # noqa: BLE001 - no flock here; proceed unlocked
            logging_warn = getattr(__import__("logging"), "warning")
            logging_warn(
                "submission_payment: could not take the spend lock (%s); "
                "concurrent miner processes could pay twice", e
            )
    except OSError:
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                handle.close()  # releases the flock
            except OSError:
                pass


def pay_for_resubmission(
    *,
    bt_compat,
    subtensor,
    wallet,
    policy: SubmissionPolicy,
    round_id: str,
    hotkey: str,
    ledger: Optional[PaymentLedger] = None,
    logger=None,
) -> Optional[Dict[str, Any]]:
    """Buy one extra submission. Returns a proof dict, or None if it failed.

    Reuses an unspent proof rather than paying twice — the common case after a
    submission failed for reasons unrelated to payment.
    """
    ledger = ledger or PaymentLedger()

    with _wallet_spend_lock(ledger):
        return _pay_for_resubmission_locked(
            bt_compat=bt_compat, subtensor=subtensor, wallet=wallet, policy=policy,
            round_id=round_id, hotkey=hotkey, ledger=ledger, logger=logger,
        )


def _pay_for_resubmission_locked(
    *, bt_compat, subtensor, wallet, policy, round_id, hotkey, ledger, logger
) -> Optional[Dict[str, Any]]:
    """The body of pay_for_resubmission, run while holding the spend lock."""
    # Checked before any stored proof is honoured: turning the feature off must
    # stop attaching proofs written while it was on.
    if not policy.usable:
        return None

    existing = ledger.unspent_proof(round_id, hotkey)
    if existing:
        if logger:
            logger.info(f"Reusing an unspent resubmission payment for round {round_id[:8]}...")
        return existing

    # An intent with no proof means a previous transfer's outcome is unknown.
    # Never pay a second time to resolve it — see PaymentLedger.unresolved_intent.
    if ledger.unresolved_intent(round_id, hotkey):
        if logger:
            logger.error(
                f"Round {round_id[:8]}...: a previous resubmission payment has an "
                f"unknown outcome (intent recorded, no proof). NOT paying again. "
                f"Check {ledger.path} against the chain and remove the stale intent "
                f"once settled."
            )
        return None

    # Mirror the platform's ownership rule before the transfer, not after it.
    # A fee paid from the wrong coldkey is refused on arrival and not refunded.
    if not preflight_payment_signer(
        subtensor=subtensor, wallet=wallet, hotkey=hotkey, logger=logger
    ):
        return None

    # Rolling 24h ceilings, checked against what actually completed. The
    # per-submission cap bounds one payment; these bound a bad day. Both must
    # pass: the per-hotkey cap stops one miner looping, and the wallet cap stops
    # K hotkeys on one host multiplying that ceiling by K.
    cutoff = int(time.time()) - 86400
    cap = max_daily_tao()
    spent = ledger.spend_since(cutoff, hotkey=hotkey)
    if spent + policy.fee_tao > cap:
        if logger:
            logger.error(
                f"Refusing to pay: {spent:.4f} TAO already spent on resubmissions by "
                f"this hotkey in the last 24h; this {policy.fee_tao} TAO fee would exceed the "
                f"{cap} TAO daily ceiling. Raise MINOS_MAX_DAILY_RESUBMISSION_TAO "
                f"deliberately if this is intended."
            )
        return None

    wallet_cap = max_daily_wallet_tao()
    wallet_spent = ledger.spend_since(cutoff)
    if wallet_spent + policy.fee_tao > wallet_cap:
        if logger:
            logger.error(
                f"Refusing to pay: {wallet_spent:.4f} TAO already spent on resubmissions by "
                f"ALL hotkeys on this host in the last 24h; this {policy.fee_tao} TAO fee "
                f"would exceed the {wallet_cap} TAO wallet-wide ceiling. Raise "
                f"MINOS_MAX_DAILY_WALLET_TAO deliberately if this is intended."
            )
        return None

    # State the terms before money moves, so the operator can see from the log
    # what was paid, to whom, and under what ceilings -- without reading code.
    if logger:
        logger.info(
            f"Paying for resubmission in round {round_id[:8]}...: "
            f"{policy.fee_tao} TAO -> {policy.destination} "
            f"(per-submission cap {max_fee_tao()}, hotkey 24h cap {cap}, "
            f"host 24h cap {wallet_cap}, "
            f"destination {'pinned' if pinned_destination() else 'unpinned'})"
        )
    ledger.record_intent(round_id, hotkey, policy.fee_tao, policy.destination)
    ok, detail, locator = bt_compat.transfer(
        subtensor, wallet=wallet, dest=policy.destination, amount_tao=policy.fee_tao
    )
    if not ok:
        ambiguous = detail == getattr(bt_compat, "AMBIGUOUS", "ambiguous:outcome-unknown")
        if ambiguous:
            # The money may have moved. Leave the intent UNSETTLED so
            # unresolved_intent blocks further spending on this round.
            if logger:
                logger.error(
                    f"Round {round_id[:8]}...: resubmission transfer outcome is "
                    f"UNKNOWN. Not retrying. Reconcile {ledger.path} against the "
                    f"chain before re-enabling payment for this round."
                )
            return None
        # A readable failure: nothing moved. Settle the intent so one declined
        # transfer does not wedge the round.
        try:
            ledger.record_failure(round_id, hotkey, str(detail))
        except Exception:  # noqa: BLE001 - best effort; worst case we block
            pass
        if logger:
            logger.warning(f"Resubmission payment failed ({detail}); not submitting")
        return None

    # Without an on-chain reference the proof is unverifiable. Leave the intent
    # unresolved rather than fabricate one.
    if not locator and not detail:
        if logger:
            logger.error(
                f"Round {round_id[:8]}...: transfer reported success but returned no "
                f"on-chain reference; refusing to build an unverifiable proof. "
                f"The payment may have landed — reconcile {ledger.path} manually."
            )
        return None

    # The platform verifies the extrinsic at a specific index in a specific
    # block. The SDK receipt already carries all three, so prefer it; scanning
    # the block is only a fallback for builds whose receipt is incomplete.
    if not locator:
        try:
            locator = bt_compat.locate_transfer(
                subtensor,
                block_hash=detail,
                signer_ss58=str(wallet.coldkeypub.ss58_address),
                dest=policy.destination,
                amount_tao=policy.fee_tao,
            )
        except Exception as e:  # noqa: BLE001 - best effort; handled below
            if logger:
                logger.warning(f"Could not locate the transfer extrinsic: {type(e).__name__}: {e}")

    if not locator:
        # Paid, but we cannot say where. Record the SPEND even though there is
        # no proof: the money is gone, and a ceiling that only counts provable
        # payments is blind to exactly the failures that cost the most.
        try:
            ledger.record_spend_without_proof(
                round_id, hotkey, policy.fee_tao, str(detail)
            )
        except Exception:  # noqa: BLE001 - best effort; logged below regardless
            pass
        # Paid, but we cannot say where. A proof without a verifiable locator
        # will be refused, and paying again would double-spend the fee, so the
        # intent stays unresolved for manual reconciliation.
        if logger:
            logger.error(
                f"Round {round_id[:8]}...: the resubmission fee was PAID but the "
                f"extrinsic could not be located on chain, so no verifiable proof "
                f"can be built. Do not pay again — reconcile {ledger.path} manually "
                f"(transfer reported: {str(detail)[:60]})."
            )
        return None

    proof = {
        "destination": policy.destination,
        "amount_tao": policy.fee_tao,
        "round_id": round_id,
        "hotkey": hotkey,
        # What the platform actually verifies against chain.
        "block_hash": locator["block_hash"],
        "block_number": locator["block_number"],
        "extrinsic_index": locator["extrinsic_index"],
        "reference": detail,
        "paid_at": int(time.time()),
        # Proof identity, matched by mark_spent; distinguishes payments that
        # agree on every other field.
        "proof_id": uuid.uuid4().hex,
    }
    # Money has already moved: a raising ledger write here would lose the only
    # record of it. Log loudly and still return the proof so the caller can
    # spend it on this submission.
    try:
        ledger.record_proof(round_id, hotkey, proof)
    except Exception as e:  # noqa: BLE001
        if logger:
            logger.error(
                f"Round {round_id[:8]}...: PAYMENT SENT ({policy.fee_tao} TAO to "
                f"{policy.destination}, ref {detail}) but the ledger write FAILED "
                f"({type(e).__name__}: {e}). Record this reference manually — it is "
                f"the only evidence of the payment."
            )
    return proof
