"""Candidate normalization: one reward candidate per owner.

The reward curve is deliberately outside this module. Normalization only decides
which submissions are candidates for that unchanged ranking, by one rule:

    A round-pinned owner (coldkey) contributes ONE candidate per round.

That is what makes running many hotkeys under one coldkey stop multiplying an
operator's share of the reward. Registration already prices each hotkey; this
prices the reward slot.

The choice is made BEFORE scores are known — by explicit designation when the
platform supplies one, otherwise by earliest submission. Either way the operator
picks blind, so no hotkey can be selected for having scored well.

THE CONTEXT IS PINNED TO A BLOCK, and that matters more than it looks. If each
validator derived the owner map from its own metagraph read, two validators
reading at different heights could disagree about who owns which hotkey, collapse
different candidates, and submit different weights. The platform supplies one
round-pinned mapping and validators attest to having seen the same one.

Deliberately NOT here, and each removed on its own merits:

  * reward bonds and identity maturity — gates on WHO may earn, needing platform
    infrastructure that does not exist, and the maturity gate keeps genuinely new
    miners from earning at all.
  * collapsing equivalent configs across owners — a gate on SIMILARITY rather
    than identity. It cannot tell a sybil farm from two operators who
    independently found the same good config, and on this subnet convergence is
    normal and legitimate. Post-clamp equality made it worse, since clamping
    pushes distinct submissions toward each other.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from .config_commit import canonical_config


# Bumped from v2: the context shape and the rule set both changed. A platform
# still serving the v2 policy must NOT be accepted by a validator running this,
# because it would supply fields this no longer reads and expect collapses this
# no longer performs.
POLICY_VERSION = "minos-candidate-normalization-v3"
CONTEXT_DOMAIN = "minos-candidate-normalization-context-v3"
ATTESTATION_DOMAIN = b"minos-candidate-normalization-attestation-v3"
MIN_ATTESTATION_QUORUM = 2
RECEIPT_OWNER_NOT_DESIGNATED = "OWNER_REWARD_SLOT_NOT_DESIGNATED"
SCORE_TOLERANCE = 1e-9


class CandidateNormalizationError(ValueError):
    """Normalization cannot be applied safely to this round."""


@dataclass(frozen=True)
class NormalizationDecision:
    """Private decision record.  Do not include this object in public APIs."""

    selected: bool
    internal_reason: str
    represented_by: str

    @property
    def receipt_reason(self) -> Optional[str]:
        """Bounded reason for this miner's authenticated private receipt."""
        if self.selected:
            return None
        if self.internal_reason == "owner_alternate":
            return RECEIPT_OWNER_NOT_DESIGNATED
        raise CandidateNormalizationError(
            f"no bounded receipt reason for internal decision {self.internal_reason!r}"
        )


@dataclass(frozen=True)
class NormalizationResult:
    selected_hotkeys: Tuple[str, ...]
    decisions: Mapping[str, NormalizationDecision]
    candidate_count: int
    # Retained for the audit record's shape. With cross-owner collapse removed
    # this equals the selected count; it is not a distinct measurement.
    unique_solution_count: int
    unique_owner_count: int
    audit_digest: str

    @property
    def removed_count(self) -> int:
        return self.candidate_count - len(self.selected_hotkeys)


@dataclass(frozen=True)
class CandidateNormalizationContext:
    """Validated, round-pinned private context returned by the platform."""

    round_id: str
    snapshot_block: int
    score_schema_version: str
    owner_by_hotkey: Mapping[str, str]
    reward_designated_by_hotkey: Mapping[str, bool]
    selection_score_by_hotkey: Mapping[str, float]
    selection_time_by_hotkey: Mapping[str, float]
    context_digest: str
    attesting_hotkeys: Tuple[str, ...]


def build_private_receipt_claim(
    *,
    round_id: str,
    submission_id: str,
    decision_root: str,
    decision: NormalizationDecision,
) -> Dict[str, str]:
    """Build the bounded claim that the platform signs for one miner.

    Authentication, ownership checks, finalization checks, and signing belong to
    the platform endpoint. This helper intentionally cannot serialize the private
    representative or the owner.

    Not wired up: the platform serves no receipt endpoint yet. It is kept
    because the bounded claim shape is what a receipt must not exceed, and
    deciding that alongside the decision logic is what stops a future endpoint
    leaking more than the reason.
    """
    if not isinstance(round_id, str) or not round_id:
        raise CandidateNormalizationError("receipt round_id is required")
    if not isinstance(submission_id, str) or not submission_id:
        raise CandidateNormalizationError("receipt submission_id is required")
    if not _valid_token(decision_root):
        raise CandidateNormalizationError("receipt decision_root is invalid")

    claim = {
        "policy_version": POLICY_VERSION,
        "round_id": round_id,
        "submission_id": submission_id,
        "status": "SELECTED" if decision.selected else "NOT_SELECTED",
        "decision_root": decision_root,
    }
    reason = decision.receipt_reason
    if reason is not None:
        claim["reason_code"] = reason
    return claim


def compute_context_digest(
    *,
    round_id: str,
    snapshot_block: int,
    score_schema_version: str,
    owner_by_hotkey: Mapping[str, str],
    reward_designated_by_hotkey: Mapping[str, bool],
    selection_score_by_hotkey: Mapping[str, float],
    selection_time_by_hotkey: Mapping[str, float],
) -> str:
    """Deterministic integrity digest for a private normalization context.

    This is an audit/integrity checksum, not proof that the platform derived
    owners correctly.  The endpoint remains authenticated and
    the underlying signed owner snapshot/private audit records are authoritative.
    """
    payload = {
        "domain": CONTEXT_DOMAIN,
        "policy_version": POLICY_VERSION,
        "round_id": round_id,
        "snapshot_block": snapshot_block,
        "score_schema_version": score_schema_version,
        "owner_by_hotkey": dict(owner_by_hotkey),
        "reward_designated_by_hotkey": dict(reward_designated_by_hotkey),
        # Normalize JSON number spelling so 1 and 1.0 cannot create different
        # context digests across platform implementations.
        "selection_score_by_hotkey": {
            key: float(value) for key, value in selection_score_by_hotkey.items()
        },
        "selection_time_by_hotkey": {
            key: float(value) for key, value in selection_time_by_hotkey.items()
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_token(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def _finite_time(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float("inf")
    return parsed if math.isfinite(parsed) else float("inf")


def context_attestation_message(context_digest: str) -> bytes:
    """Canonical message independently signed for a private context digest."""
    if not _valid_token(context_digest):
        raise CandidateNormalizationError("context_digest is invalid for attestation")
    return ATTESTATION_DOMAIN + b"\x1f" + context_digest.encode("ascii")


def validate_context_attestations(
    payload: Mapping[str, Any],
    *,
    authorized_attesters: Iterable[str],
    min_attestations: int,
    verify_signature: Callable[[str, bytes, str], bool],
) -> Tuple[str, ...]:
    """Require a configured independent quorum over the private context.

    The authorized signer set and quorum come from validator network config,
    never from the platform response being verified. Signatures cover the
    context digest, which in turn binds the round, ownership, bond/maturity,
    designation, scores, and commitment ordering.
    """
    authorized = tuple(authorized_attesters)
    if (
        not authorized
        or len(set(authorized)) != len(authorized)
        or not all(isinstance(hotkey, str) and hotkey for hotkey in authorized)
    ):
        raise CandidateNormalizationError("authorized attesters are invalid")
    if (
        isinstance(min_attestations, bool)
        or not isinstance(min_attestations, int)
        or min_attestations < MIN_ATTESTATION_QUORUM
        or min_attestations > len(authorized)
    ):
        raise CandidateNormalizationError(
            f"attestation quorum must be between {MIN_ATTESTATION_QUORUM} "
            "and the authorized attester count"
        )
    attestations = payload.get("attestations")
    if not isinstance(attestations, list):
        raise CandidateNormalizationError("attestations must be a list")

    digest = payload.get("context_digest")
    message = context_attestation_message(digest)
    authorized_set = set(authorized)
    valid = []
    seen = set()
    for entry in attestations:
        if not isinstance(entry, Mapping):
            raise CandidateNormalizationError("attestation entry must be an object")
        hotkey = entry.get("validator_hotkey")
        signature = entry.get("signature")
        if hotkey not in authorized_set:
            continue
        if hotkey in seen:
            raise CandidateNormalizationError("duplicate context attestation")
        seen.add(hotkey)
        if (
            not isinstance(signature, str)
            or len(signature) != 128
            or any(ch not in "0123456789abcdef" for ch in signature)
        ):
            continue
        try:
            verified = verify_signature(hotkey, message, signature)
        except Exception:
            verified = False
        if verified:
            valid.append(hotkey)

    if len(valid) < min_attestations:
        raise CandidateNormalizationError(
            f"candidate context has {len(valid)} valid attestation(s); "
            f"requires {min_attestations}"
        )
    return tuple(sorted(valid))


def validate_context_payload(
    payload: Mapping[str, Any],
    *,
    expected_round_id: str,
    expected_score_schema_version: str,
    required_hotkeys: Iterable[str],
    authorized_attesters: Iterable[str],
    min_attestations: int,
    verify_signature: Callable[[str, bytes, str], bool],
) -> CandidateNormalizationContext:
    """Validate a platform context strictly; incomplete contexts fail closed."""
    if not isinstance(payload, Mapping):
        raise CandidateNormalizationError("normalization context must be an object")
    if payload.get("policy_version") != POLICY_VERSION:
        raise CandidateNormalizationError(
            f"unsupported policy_version={payload.get('policy_version')!r}"
        )
    if payload.get("round_id") != expected_round_id:
        raise CandidateNormalizationError("normalization context is for a different round")
    if payload.get("score_schema_version") != expected_score_schema_version:
        raise CandidateNormalizationError(
            "normalization context uses a different score schema version"
        )
    snapshot_block = payload.get("snapshot_block")
    if isinstance(snapshot_block, bool) or not isinstance(snapshot_block, int) or snapshot_block <= 0:
        raise CandidateNormalizationError("snapshot_block must be a positive integer")

    map_fields = (
        "owner_by_hotkey",
        "reward_designated_by_hotkey",
        "selection_score_by_hotkey",
        "selection_time_by_hotkey",
    )
    maps: Dict[str, Mapping[str, Any]] = {}
    for field in map_fields:
        value = payload.get(field)
        if not isinstance(value, Mapping):
            raise CandidateNormalizationError(f"{field} must be an object")
        maps[field] = value

    required = list(required_hotkeys)
    needed = set(required)
    if len(needed) != len(required):
        raise CandidateNormalizationError("required_hotkeys contains duplicates")
    for field, values in maps.items():
        missing = needed - set(values)
        if missing:
            raise CandidateNormalizationError(
                f"{field} is missing {len(missing)} reward candidate(s)"
            )

    owners: Dict[str, str] = {}
    designations: Dict[str, bool] = {}
    scores: Dict[str, float] = {}
    times: Dict[str, float] = {}
    for hotkey in needed:
        owner = maps["owner_by_hotkey"][hotkey]
        if not isinstance(owner, str) or not owner.strip():
            raise CandidateNormalizationError(f"invalid owner for {hotkey}")
        owners[hotkey] = owner.strip()

        designation = maps["reward_designated_by_hotkey"][hotkey]
        if not isinstance(designation, bool):
            raise CandidateNormalizationError(
                f"invalid reward designation for {hotkey}"
            )
        designations[hotkey] = designation

        try:
            score = float(maps["selection_score_by_hotkey"][hotkey])
        except (TypeError, ValueError):
            raise CandidateNormalizationError(f"invalid selection score for {hotkey}")
        if not math.isfinite(score) or not 0.0 < score <= 1.0:
            raise CandidateNormalizationError(f"selection score out of range for {hotkey}")
        scores[hotkey] = score
        try:
            selection_time = float(maps["selection_time_by_hotkey"][hotkey])
        except (TypeError, ValueError):
            raise CandidateNormalizationError(f"invalid selection time for {hotkey}")
        if not math.isfinite(selection_time) or selection_time < 0.0:
            raise CandidateNormalizationError(f"selection time out of range for {hotkey}")
        times[hotkey] = selection_time

    # An owner designates at most one candidate. More than one is a malformed
    # context, not a tie to resolve later: resolving it after scores are known
    # is exactly the post-score portfolio choice this rule removes.
    for owner in set(owners.values()):
        owner_hotkeys = [hotkey for hotkey in needed if owners[hotkey] == owner]
        if sum(1 for hotkey in owner_hotkeys if designations[hotkey]) > 1:
            raise CandidateNormalizationError(
                "an owner has more than one reward-designated candidate"
            )

    supplied_digest = payload.get("context_digest")
    if not _valid_token(supplied_digest):
        raise CandidateNormalizationError("context_digest must be a lowercase SHA-256 hex digest")
    expected_digest = compute_context_digest(
        round_id=expected_round_id,
        snapshot_block=snapshot_block,
        score_schema_version=expected_score_schema_version,
        owner_by_hotkey=maps["owner_by_hotkey"],
        reward_designated_by_hotkey=maps["reward_designated_by_hotkey"],
        selection_score_by_hotkey=maps["selection_score_by_hotkey"],
        selection_time_by_hotkey=maps["selection_time_by_hotkey"],
    )
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise CandidateNormalizationError("normalization context digest mismatch")
    attesting_hotkeys = validate_context_attestations(
        payload,
        authorized_attesters=authorized_attesters,
        min_attestations=min_attestations,
        verify_signature=verify_signature,
    )

    return CandidateNormalizationContext(
        round_id=expected_round_id,
        snapshot_block=snapshot_block,
        score_schema_version=expected_score_schema_version,
        owner_by_hotkey=owners,
        reward_designated_by_hotkey=designations,
        selection_score_by_hotkey=scores,
        selection_time_by_hotkey=times,
        context_digest=supplied_digest,
        attesting_hotkeys=attesting_hotkeys,
    )


def _score_cmp(scores: Mapping[str, float], times: Mapping[str, float]):
    def _cmp(left: str, right: str) -> int:
        left_score = scores[left]
        right_score = scores[right]
        if abs(left_score - right_score) > SCORE_TOLERANCE:
            return -1 if left_score > right_score else 1
        left_time = _finite_time(times.get(left))
        right_time = _finite_time(times.get(right))
        if left_time != right_time:
            return -1 if left_time < right_time else 1
        return -1 if left < right else (1 if left > right else 0)

    return _cmp


def normalize_candidates(
    *,
    round_id: str,
    candidate_hotkeys: Iterable[str],
    owner_by_hotkey: Mapping[str, str],
    selection_score_by_hotkey: Mapping[str, float],
    selection_time_by_hotkey: Optional[Mapping[str, float]] = None,
    reward_designated_by_hotkey: Optional[Mapping[str, bool]] = None,
) -> NormalizationResult:
    """Keep one candidate per owner; leave the ranking itself untouched.

    Live owner designation is explicit and fixed before scoring. Replay can omit
    ``reward_designated_by_hotkey`` and uses earliest submission as a proxy.
    Both are deliberately independent of the eventual score, so no hotkey can be
    chosen for having scored well. Only after that collapse are the survivors
    ranked by finalized canonical score.
    """
    candidates = list(candidate_hotkeys)
    if not round_id:
        raise CandidateNormalizationError("round_id is required")
    if len(candidates) != len(set(candidates)):
        raise CandidateNormalizationError("candidate_hotkeys contains duplicates")
    if not candidates:
        empty_digest = hashlib.sha256(
            f"{POLICY_VERSION}\x1f{round_id}\x1fempty".encode("utf-8")
        ).hexdigest()
        return NormalizationResult((), {}, 0, 0, 0, empty_digest)

    times = dict(selection_time_by_hotkey or {})
    owners: Dict[str, str] = {}
    scores: Dict[str, float] = {}
    explicit_designations: Dict[str, bool] = {}
    for hotkey in candidates:
        owner = owner_by_hotkey.get(hotkey)
        score = selection_score_by_hotkey.get(hotkey)
        if not isinstance(owner, str) or not owner.strip():
            raise CandidateNormalizationError(f"missing owner for {hotkey}")
        try:
            score = float(score)
        except (TypeError, ValueError):
            raise CandidateNormalizationError(f"missing/invalid selection score for {hotkey}")
        if not math.isfinite(score) or not 0.0 < score <= 1.0:
            raise CandidateNormalizationError(f"selection score out of range for {hotkey}")
        owners[hotkey] = owner.strip()
        scores[hotkey] = score
        if reward_designated_by_hotkey is not None:
            designation = reward_designated_by_hotkey.get(hotkey)
            if not isinstance(designation, bool):
                raise CandidateNormalizationError(
                    f"missing/invalid reward designation for {hotkey}"
                )
            explicit_designations[hotkey] = designation

    decisions: Dict[str, NormalizationDecision] = {}
    cmp = _score_cmp(scores, times)

    # Every candidate is eligible. Gates on WHO may earn — reward bonds, identity
    # maturity — are deliberately not part of this rule.
    gate_eligible = list(candidates)

    # One pre-score designated candidate for each round-pinned owner.
    by_owner: Dict[str, list] = {}
    for hotkey in gate_eligible:
        by_owner.setdefault(owners[hotkey], []).append(hotkey)
    owner_representatives = []
    for group in by_owner.values():
        if reward_designated_by_hotkey is None:
            representative = min(
                group, key=lambda hotkey: (_finite_time(times.get(hotkey)), hotkey)
            )
        else:
            designated = [
                hotkey for hotkey in group if explicit_designations[hotkey]
            ]
            if len(designated) > 1:
                raise CandidateNormalizationError(
                    "an owner has more than one reward-designated candidate"
                )
            representative = designated[0] if designated else None
        if representative is not None:
            owner_representatives.append(representative)
        for hotkey in group:
            if hotkey != representative:
                decisions[hotkey] = NormalizationDecision(
                    selected=False,
                    internal_reason="owner_alternate",
                    represented_by=representative or "",
                )

    # Each owner's representative is selected. Configs are NOT compared across
    # owners: two operators who independently reached the same config are two
    # candidates, because similarity is not identity and this subnet's field
    # converges by design.
    selected = list(owner_representatives)
    for hotkey in selected:
        decisions[hotkey] = NormalizationDecision(
            selected=True,
            internal_reason="selected",
            represented_by=hotkey,
        )

    selected = sorted(selected, key=functools.cmp_to_key(cmp))
    private_audit = {
        "domain": POLICY_VERSION,
        "round_id": round_id,
        "selected": selected,
        "decisions": {
            hotkey: {
                "selected": decisions[hotkey].selected,
                "internal_reason": decisions[hotkey].internal_reason,
                "represented_by": decisions[hotkey].represented_by,
            }
            for hotkey in sorted(decisions)
        },
    }
    audit_digest = hashlib.sha256(
        json.dumps(
            private_audit,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return NormalizationResult(
        selected_hotkeys=tuple(selected),
        decisions=decisions,
        candidate_count=len(candidates),
        unique_owner_count=len(by_owner),
        unique_solution_count=len(selected),
        audit_digest=audit_digest,
    )
