"""
Helpers for subset-based validator scoring.

Provides utilities for extracting miner/validator lists from the Bittensor
metagraph and checking whether the scoring deadline is approaching.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional


def get_miners_from_metagraph(metagraph, my_uid: Optional[int] = None) -> List[str]:
    """
    Return hotkeys of all miners (neurons without validator_permit) in the metagraph.

    Args:
        metagraph: Bittensor metagraph object.
        my_uid: This validator's UID — excluded from the miner list.

    Returns:
        List of miner hotkeys ordered by UID (stable ordering).
    """
    miners = []
    for uid in range(len(metagraph.hotkeys)):
        if uid == my_uid:
            continue
        has_permit = (
            bool(metagraph.validator_permit[uid])
            if hasattr(metagraph, "validator_permit")
            else False
        )
        if not has_permit:
            miners.append(metagraph.hotkeys[uid])
    return miners


def get_validators_from_metagraph(metagraph, my_uid: Optional[int] = None) -> List[Dict]:
    """
    Return a list of validator info dicts sorted by stake descending.

    Args:
        metagraph: Bittensor metagraph object.
        my_uid: This validator's UID — included in the list (it is a validator too).

    Returns:
        List of {hotkey, stake, uid} dicts, sorted by stake descending.
    """
    validators = []
    for uid in range(len(metagraph.hotkeys)):
        has_permit = (
            bool(metagraph.validator_permit[uid])
            if hasattr(metagraph, "validator_permit")
            else False
        )
        if has_permit:
            stake = float(metagraph.S[uid]) if hasattr(metagraph, "S") else 0.0
            validators.append({
                "hotkey": metagraph.hotkeys[uid],
                "stake": stake,
                "uid": uid,
            })
    return sorted(validators, key=lambda v: (-v["stake"], v["hotkey"]))


def seconds_until_deadline(
    scoring_end_time: datetime,
    tz: timezone = timezone.utc,
) -> float:
    """Return seconds remaining until scoring_end_time. Negative if past deadline."""
    now = datetime.now(scoring_end_time.tzinfo or tz)
    return (scoring_end_time - now).total_seconds()


def should_stop_secondary_scoring(
    scoring_end_time: Optional[datetime],
    buffer_seconds: int = 180,
) -> bool:
    """
    Return True if the scoring deadline is close enough that secondary (non-primary)
    miners should no longer be scored.

    Args:
        scoring_end_time: Deadline from the platform assignment response.
        buffer_seconds: Stop secondary scoring this many seconds before deadline.

    Returns:
        True if secondary scoring should stop, False to continue.
    """
    if scoring_end_time is None:
        return False
    remaining = seconds_until_deadline(scoring_end_time)
    return remaining < buffer_seconds
