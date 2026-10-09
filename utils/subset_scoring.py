"""
Helpers for subset-based validator scoring.

Provides utilities for extracting miner/validator lists from the Bittensor
metagraph and checking whether the scoring deadline is approaching.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from utils.weight_tracking import parse_deadline

# Stop starting new secondary scoring this many seconds before scoring_end_time.
#
# A literal, not an env read: this module is imported before load_dotenv(), so
# reading the environment here would miss .env on a manual launch. The override
# is applied beside SCORE_GRACE_SECONDS in neurons/validator.py.
DEFAULT_CUTOFF_LEAD_SECONDS = 420


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
    """Return seconds remaining until scoring_end_time. Negative if past deadline.

    A naive deadline is read as UTC, matching parse_deadline, rather than as the
    host's local time, so two validators compute the same remaining time from the
    same assignment. Taking the timezone from the deadline itself
    (`scoring_end_time.tzinfo or tz`) does not do that: it leaves a naive deadline
    naive while `now` is aware, and the subtraction raises TypeError.
    """
    deadline = parse_deadline(scoring_end_time)
    return (deadline - datetime.now(tz)).total_seconds()


def should_stop_secondary_scoring(
    scoring_end_time: Optional[datetime],
    buffer_seconds: int = DEFAULT_CUTOFF_LEAD_SECONDS,
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


def scoring_window_closed(
    scoring_end_time: Optional[datetime],
    grace_seconds: int,
) -> bool:
    """Whether a newly submitted score would still be accepted.

    /submit-score accepts a score up to grace_seconds past scoring_end_time.
    Past that, work in progress can no longer produce anything storable, so it
    is both pointless to start more and pointless to keep running what is
    already going -- which is what the reaper in the scoring loop acts on.

    Fails open on a missing deadline: the single-validator fallback path has no
    assignment, and stopping all scoring there would cost the whole round
    rather than its tail.
    """
    if scoring_end_time is None:
        return False
    remaining = seconds_until_deadline(scoring_end_time)
    return remaining < -grace_seconds
