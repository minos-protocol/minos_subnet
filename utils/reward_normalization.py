"""One reward-eligible representative per coldkey.

Several hotkeys can share one coldkey, and emissions are paid to the coldkey.
Without this, an operator registering K hotkeys occupies K rank slots and is paid
K times for the same work, which is the whole economics of a sybil fleet.

WHERE THE OWNERSHIP COMES FROM
------------------------------
The metagraph, which every validator already syncs, with the platform's cached
map as the fallback when a validator cannot read it.

Either source is fine, because the mapping is STABLE: a hotkey belongs to the
coldkey that registered it and stays there, so a copy cached a few blocks ago is
as correct as a fresh one. Preferring the local metagraph is about availability
and freshness, not about which source to believe.

The thing that must NOT be substituted is the UID ordering. The weight vector is
indexed by UID against the validator's own metagraph, so that ordering has to
come from the same view the weights are submitted against -- otherwise correct
weights land in the wrong slots.

WHAT IT DOES
------------
Walks the ranking in order and keeps the first hotkey seen for each coldkey.
Order is the caller's -- best first -- so the survivor is the coldkey's best
performer. Ties are already resolved upstream by score then submission time, so
this adds no new tiebreak and cannot reorder anything.

A hotkey whose owner cannot be resolved is KEPT. Dropping it would let a stale
or partial metagraph silently remove real miners from the reward set; the failure
direction has to be "pay someone twice" rather than "pay a legitimate miner
nothing".
"""
import logging
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


def owners_from_metagraph(metagraph) -> Dict[str, str]:
    """Map hotkey -> coldkey from a synced metagraph.

    Returns {} when the metagraph carries no coldkeys, which the caller must
    treat as "cannot deduplicate" rather than "nobody shares a coldkey".
    """
    hotkeys = list(getattr(metagraph, "hotkeys", None) or [])
    coldkeys = list(getattr(metagraph, "coldkeys", None) or [])
    if not hotkeys or not coldkeys:
        return {}
    owners: Dict[str, str] = {}
    for i, hotkey in enumerate(hotkeys):
        if i >= len(coldkeys):
            break
        coldkey = coldkeys[i]
        if hotkey and coldkey:
            owners[hotkey] = coldkey
    return owners


def one_per_owner(
    ranked_hotkeys: List[str],
    owner_by_hotkey: Mapping[str, str],
) -> Tuple[List[str], Dict[str, str]]:
    """Keep each coldkey's best-ranked hotkey.

    ``ranked_hotkeys`` must already be ordered best first.

    Returns ``(kept, represented_by)`` where ``represented_by`` maps each dropped
    hotkey to the hotkey that now represents its coldkey -- kept so the exclusion
    can be explained rather than appearing as an unexplained zero.
    """
    kept: List[str] = []
    represented_by: Dict[str, str] = {}
    winner_for_coldkey: Dict[str, str] = {}

    for hotkey in ranked_hotkeys:
        coldkey = owner_by_hotkey.get(hotkey)
        if not coldkey:
            # Unknown owner: keep. A stale metagraph must not silently remove a
            # legitimate miner from the reward set.
            kept.append(hotkey)
            continue
        winner = winner_for_coldkey.get(coldkey)
        if winner is None:
            winner_for_coldkey[coldkey] = hotkey
            kept.append(hotkey)
        else:
            represented_by[hotkey] = winner

    return kept, represented_by


def apply(
    ranked_hotkeys: List[str],
    metagraph,
    *,
    enabled: bool,
    logger_=None,
) -> Tuple[List[str], Dict[str, str]]:
    """``one_per_owner`` against the metagraph, or a no-op when disabled.

    Disabled, and when ownership cannot be read at all, the ranking is returned
    untouched. Both are the safe direction: the cost is that a sybil fleet is
    paid more than once for one round, which is recoverable. The other direction
    -- dropping miners because ownership was unreadable -- is not.
    """
    log = logger_ or logger
    if not enabled:
        return list(ranked_hotkeys), {}

    owners = owners_from_metagraph(metagraph)
    if not owners:
        log.warning(
            "One-reward-per-coldkey is enabled but the metagraph carries no "
            "coldkeys; ranking every hotkey rather than dropping any"
        )
        return list(ranked_hotkeys), {}

    kept, represented_by = one_per_owner(ranked_hotkeys, owners)
    if represented_by:
        log.info(
            f"One reward per coldkey: {len(kept)} of {len(ranked_hotkeys)} "
            f"hotkeys rank; {len(represented_by)} share a coldkey with a "
            f"better-ranked hotkey"
        )
    return kept, represented_by
