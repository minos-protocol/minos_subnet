"""
Helpers for subset-based validator scoring.

Provides utilities for extracting miner/validator lists from the Bittensor
metagraph and checking whether the scoring deadline is approaching.
"""

import math
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


def per_job_wall_clock_budget(
    scoring_end_time: Optional[datetime],
    num_jobs: int,
    concurrency: int,
    max_job_seconds: int,
    buffer_seconds: int = 180,
    min_job_seconds: int = 300,
) -> int:
    """
    Return the wall-clock budget (seconds) a single miner scoring job may use.

    Without a deadline the full max_job_seconds applies. Under deadline
    pressure the remaining time (minus buffer_seconds) is split evenly across
    the ceil(num_jobs / concurrency) batches still needed, so a cohort of
    deliberately slow configs cannot consume the whole scoring window and
    starve the remaining miners. Templates enforce the budget through their
    subprocess timeout, i.e. an over-budget job is killed early.

    Args:
        scoring_end_time: Deadline from the platform assignment response.
        num_jobs: Miner jobs still to score in this phase.
        concurrency: Max jobs the validator runs at once.
        max_job_seconds: Full per-tool timeout (the no-pressure budget).
        buffer_seconds: Window reserved before the deadline.
        min_job_seconds: Floor so jobs are not starved to zero near the deadline.

    Returns:
        Per-job budget in seconds, clamped to [min_job_seconds, max_job_seconds].
    """
    if scoring_end_time is None:
        return max_job_seconds

    jobs = max(1, num_jobs)
    workers = max(1, concurrency)
    batches = math.ceil(jobs / workers)

    available = seconds_until_deadline(scoring_end_time) - buffer_seconds
    per_job = available / batches

    return int(max(min_job_seconds, min(max_job_seconds, per_job)))
