"""
Round-only winner weighting.

Each Minos round is a fresh genomics challenge, so validator weights should be
computed from that round's finalized scores only. Miners must still have valid
scores in at least 5 of the last 20 finalized rounds before they can receive
winner/dust weight; the current round counts. The platform weight-history
schema still has a legacy ``ema_score`` field, which is intentionally left empty
for round-only scoring.

Weight distribution is winner-heavy: the top eligible current-round miner
receives the configured winner weight, ranks #2..N receive pruning dust, and the
caller sends the unallocated remainder to burn.
"""

from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from collections import defaultdict, OrderedDict
import functools
import logging
import math
import os

logger = logging.getLogger(__name__)

# Eligibility gate: score in at least 5 of the last 20 finalized rounds. The
# current round is appended before weights are computed, so it counts.
PARTICIPATION_WINDOW = int(os.getenv("PARTICIPATION_WINDOW", "20"))
MIN_PARTICIPATION_ROUNDS = int(os.getenv("MIN_PARTICIPATION_ROUNDS", "5"))

# Equal current-round scores are tied by earliest submission timestamp.
ROUND_SCORE_TOLERANCE = 1e-9

# Canonical-ranking tiebreak. When a canonical candidate is within this absolute
# current-round score gap of local rank 1, the canonical candidate is used as the
# winner. This keeps validators aligned on very close rounds without overriding
# clear local score differences.
CANONICAL_TIEBREAK_TOLERANCE = 0.001

# Minimum canonical coverage. Platform contributors below the per-validator
# stake floor are excluded, and the validator requires enough distinct
# validators before using the canonical ranking.
CANONICAL_MIN_VALIDATOR_COUNT = int(os.getenv("CANONICAL_MIN_VALIDATOR_COUNT", "2"))
CANONICAL_MIN_VALIDATOR_STAKE = float(os.getenv("CANONICAL_MIN_VALIDATOR_STAKE", "5000"))

# Reward defaults — FALLBACK ONLY. The live validator requires the authoritative
# values from /scoring/network-config (get_network_config) and ignores these.
# Current policy (absolute validator-vector weights before Bittensor's u16
# encoding): burn 0.0, rank #1 gets 0.9, and eligible ranks #2-#20 split the
# remaining ~0.10 by 0.8 geometric decay. Always check network-config for the
# latest — these are dynamic protocol values.
DEFAULT_BURN_RATE = 0.0
DEFAULT_WINNER_WEIGHT = 0.9
DEFAULT_DUST_TOP_N = 20
DEFAULT_DUST_DECAY = 0.80


def parse_submitted_at(value) -> Optional[float]:
    """Epoch seconds from a platform submitted_at, or None if absent or unparseable.

    Accepts an ISO-8601 string (with or without a trailing Z) or a datetime;
    naive values are read as UTC. Every caller must use this helper so all
    validators derive the same submission ordering from the same payload.
    """
    if not value:
        return None
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Unparseable submitted_at {value!r}: {e}")
        return None


def parse_deadline(value) -> Optional[datetime]:
    """Timezone-aware datetime from a platform deadline, or None if absent.

    Accepts an ISO-8601 string (with or without a trailing Z) or a datetime;
    naive values are read as UTC. Raises ValueError on a malformed value rather
    than returning None: None is the "no deadline supplied" signal and the
    deadline guards fail open on it, so a malformed deadline must not collapse
    into it.
    """
    # Only null/empty is "absent"; any other falsy value is a malformed payload.
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(
            f"Unparseable deadline {value!r}: expected an ISO string or datetime"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ScoreTracker:
    """Track current-round scores plus recent-window participation counts.

    Miners are identified by hotkey (ss58 address) for stability across
    metagraph resyncs. UID mapping happens at weight-setting time.
    """

    def __init__(
        self,
        min_rounds: int = MIN_PARTICIPATION_ROUNDS,
    ):
        self.min_rounds = min_rounds

        # hotkey -> current round score
        self.round_scores: Dict[str, float] = {}

        # hotkey -> current round raw score (same value, explicit for reporting)
        self.last_raw_scores: Dict[str, float] = {}

        # Recent finalized rounds for the 5-of-20 eligibility gate.
        self.round_history: List[dict] = []
        self._participation_counts: Dict[str, int] = defaultdict(int)
        self._recorded_round_ids = set()

    def recover_from_platform_state(
        self,
        legacy_score_entries: List[Dict[str, Any]],
        round_history: List[Dict[str, Any]],
    ):
        """Start fresh on scores while recovering recent participation.

        Historical platform scores are not loaded because old scores must not
        influence the next round's ranking. Recent participation history is
        loaded so the 5-of-20 eligibility gate survives validator restarts.
        Restart recovery for a currently scoring round is handled separately by
        /v2/get-submissions, which returns already-submitted scores for that
        round.
        """
        self.round_scores.clear()
        self.last_raw_scores.clear()
        self.round_history = []
        self._recorded_round_ids = set()
        self._participation_counts = defaultdict(int)
        for entry in round_history or []:
            if not isinstance(entry, dict):
                continue
            round_id = entry.get("round_id")
            if not round_id:
                continue
            scored_hotkeys = {
                hk for hk in entry.get("scored_hotkeys", []) if isinstance(hk, str) and hk
            }
            self.round_history.append({
                "round_id": round_id,
                "scored_hotkeys": scored_hotkeys,
            })
        self.round_history = self.round_history[-PARTICIPATION_WINDOW:]
        self._recalculate_participation()
        logger.info(
            "Round-only score tracker initialized fresh; ignored "
            f"{len(legacy_score_entries or [])} historical score entries and recovered "
            f"{len(self.round_history)} recent participation rounds"
        )

    def update(self, hotkey: str, raw_score: float) -> float:
        """Record a miner's current-round score and return it."""
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid round score for {hotkey[:16]}...: {raw_score!r}")
        # V2 deliberately emits 0.0 when the plausibility gate fails. Keep the
        # result in current-round state for audit/backfill convergence; ranking
        # and participation already require a strictly positive score.
        if not math.isfinite(score) or score < 0.0 or score > 1.0:
            raise ValueError(f"Round score out of range for {hotkey[:16]}...: {score!r}")
        self.round_scores[hotkey] = score
        self.last_raw_scores[hotkey] = score
        return score

    def record_round(self, round_id: str, scored_hotkeys: List[str]):
        """Finalize the current round's participation set.

        Scores for miners outside ``scored_hotkeys`` are dropped so stale state
        can never leak into the next weight update.
        """
        if round_id in self._recorded_round_ids:
            logger.debug(f"Round {round_id} already recorded, skipping")
            return

        scored_set = set(scored_hotkeys)
        self.round_scores = {
            hk: score for hk, score in self.round_scores.items() if hk in scored_set
        }
        self.last_raw_scores = {
            hk: score for hk, score in self.last_raw_scores.items() if hk in scored_set
        }

        counted_hotkeys = {
            hk for hk in scored_set if self.round_scores.get(hk, 0.0) > 0.0
        }

        self.round_history.append({
            "round_id": round_id,
            "scored_hotkeys": counted_hotkeys,
        })
        self.round_history = self.round_history[-PARTICIPATION_WINDOW:]
        self._recalculate_participation()

    def refresh_participation_window(self, round_history: List[Dict[str, Any]]) -> bool:
        """Rebuild the participation window from an authoritative platform snapshot.

        Unlike ``recover_from_platform_state`` this does NOT clear the current
        round's scores — it refreshes ONLY the recent-window participation so the
        eligibility gate reflects the platform's live view rather than stale
        in-memory state.

        Returns True if the window was refreshed, False if the snapshot carried
        no usable rounds — in which case the caller keeps the existing window.
        """
        by_round: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for entry in round_history or []:
            if not isinstance(entry, dict):
                continue
            round_id = entry.get("round_id")
            if not round_id:
                continue
            scored_hotkeys = {
                hk for hk in entry.get("scored_hotkeys", []) if isinstance(hk, str) and hk
            }
            # Credit only positive scores, matching record_round_scores, so a
            # miner's eligibility does not depend on which path recorded the
            # round. Applied only when the snapshot carries scores.
            scores = entry.get("scores")
            if isinstance(scores, dict):
                scored_hotkeys = {
                    hk for hk in scored_hotkeys
                    if isinstance(scores.get(hk), (int, float)) and scores.get(hk) > 0.0
                }
            # Round ids in the snapshot are not guaranteed distinct; one round
            # must occupy exactly one window slot. Last write wins.
            by_round[round_id] = {"round_id": round_id, "scored_hotkeys": scored_hotkeys}

        new_history = list(by_round.values())
        if not new_history:
            return False
        # A truncated snapshot must never shrink the window below what is
        # already known locally.
        if len(new_history) < len(self.round_history):
            logger.warning(
                f"Participation snapshot carries {len(new_history)} distinct rounds "
                f"but {len(self.round_history)} are already recorded locally; "
                f"keeping the local window."
            )
            return False

        # Merge per round, never replace: /v2/get-validator-state is scoped to
        # this validator, so its scored_hotkeys for a round are a subset of the
        # miners scored for it. Replacing would drop peer-scored miners from the
        # eligibility window, and record_round cannot restore them (it returns
        # early on _recorded_round_ids).
        existing = {e["round_id"]: set(e.get("scored_hotkeys") or ()) for e in self.round_history}
        merged = []
        for entry in new_history:
            prior = existing.get(entry["round_id"])
            if prior:
                entry = {
                    "round_id": entry["round_id"],
                    "scored_hotkeys": set(entry["scored_hotkeys"]) | prior,
                }
            merged.append(entry)

        self.round_history = merged[-PARTICIPATION_WINDOW:]
        self._recalculate_participation()
        return True

    def _recalculate_participation(self):
        """Recalculate recent-window participation counts.

        Counts are rebuilt from the last ``PARTICIPATION_WINDOW`` entries so a
        miner that disappears eventually loses eligibility.
        """
        counts: Dict[str, int] = defaultdict(int)
        for entry in self.round_history:
            for hotkey in entry["scored_hotkeys"]:
                counts[hotkey] += 1
        self._participation_counts = counts
        self._recorded_round_ids = {entry["round_id"] for entry in self.round_history}

    def get_participation_count(self, hotkey: str) -> int:
        """Return the miner's valid scored-round count in the recent window."""
        return self._participation_counts.get(hotkey, 0)

    def is_eligible(self, hotkey: str) -> bool:
        """Return whether a miner has met the recent-window round threshold."""
        return self.get_participation_count(hotkey) >= self.min_rounds

    def _sort_by_round_score(
        self,
        hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        tolerance: float = ROUND_SCORE_TOLERANCE,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> List[str]:
        """Sort by current-round score descending, then earliest submission."""
        score_source = self.round_scores if ranking_scores is None else ranking_scores

        def _cmp(hk_a, hk_b):
            sa = score_source.get(hk_a, 0.0)
            sb = score_source.get(hk_b, 0.0)
            ta = submission_times.get(hk_a, float("inf")) if submission_times else float("inf")
            tb = submission_times.get(hk_b, float("inf")) if submission_times else float("inf")
            if abs(sa - sb) <= tolerance:
                if ta < tb:
                    return -1
                if tb < ta:
                    return 1
                # Hotkey is the last-resort key: it is unique and identical
                # everywhere, so the ordering stays total and reproducible.
                # Input order is per-validator and must never decide a tie.
                return -1 if hk_a < hk_b else (1 if hk_a > hk_b else 0)
            return -1 if sa > sb else 1

        return sorted(hotkeys, key=functools.cmp_to_key(_cmp))

    def _ranked_positive_eligible(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> List[str]:
        """Return eligible current-round scored miners with positive scores."""
        score_source = self.round_scores if ranking_scores is None else ranking_scores
        eligible = [hk for hk in miner_hotkeys if self.is_eligible(hk)]
        return [
            hk for hk in self._sort_by_round_score(
                eligible,
                submission_times,
                tolerance=ROUND_SCORE_TOLERANCE,
                ranking_scores=ranking_scores,
            )
            if score_source.get(hk, 0.0) > 0
        ]

    def needs_canonical_tiebreak(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> bool:
        """Return True when canonical ranking can affect winner selection."""
        score_source = self.round_scores if ranking_scores is None else ranking_scores
        ranked = self._ranked_positive_eligible(
            miner_hotkeys, submission_times, ranking_scores
        )
        if len(ranked) < 2:
            return False

        top_score = score_source.get(ranked[0], 0.0)
        return any(
            (top_score - score_source.get(hk, 0.0))
            <= CANONICAL_TIEBREAK_TOLERANCE + ROUND_SCORE_TOLERANCE
            for hk in ranked[1:]
        )

    def get_winner_heavy_pruning_dust_weights(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        *,
        burn_rate: float,
        winner_weight: float,
        dust_top_n: int,
        dust_decay: float,
        canonical_top: Optional[str] = None,
        canonical_ranking: Optional[List[str]] = None,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Compute round-only winner-heavy validator-vector miner weights."""
        weights = {hk: 0.0 for hk in miner_hotkeys}
        if not miner_hotkeys:
            return weights

        burn_rate = float(burn_rate)
        winner_weight = float(winner_weight)
        dust_top_n = int(dust_top_n)
        dust_decay = float(dust_decay)
        miner_budget = 1.0 - burn_rate
        if not 0.0 <= burn_rate <= 1.0:
            raise ValueError(f"burn_rate must be between 0 and 1, got {burn_rate}")
        if not 0.0 <= winner_weight <= miner_budget:
            raise ValueError(
                f"winner_weight must be between 0 and miner budget "
                f"{miner_budget}, got {winner_weight}"
            )
        if dust_top_n < 1:
            raise ValueError(f"dust_top_n must be >= 1, got {dust_top_n}")
        # Must be finite and bounded: dust_decay comes from network-config via
        # json.loads, which accepts Infinity and NaN, and a non-finite decay
        # propagates NaN through every dust weight and past every later guard.
        if not math.isfinite(dust_decay) or not 0.0 <= dust_decay <= 1.0:
            raise ValueError(
                f"dust_decay must be a finite value between 0 and 1, got {dust_decay}"
            )

        score_source = self.round_scores if ranking_scores is None else ranking_scores
        ranked = self._ranked_positive_eligible(
            miner_hotkeys, submission_times, ranking_scores
        )
        if not ranked:
            logger.warning("No positive current-round scores — returning zero miner weights")
            return weights

        winner = ranked[0]
        canonical_candidates: List[str] = []
        if canonical_ranking:
            seen = set()
            for hk in canonical_ranking:
                if not isinstance(hk, str):
                    continue
                if not hk or hk in seen:
                    continue
                canonical_candidates.append(hk)
                seen.add(hk)
        elif canonical_top is not None:
            canonical_candidates = [canonical_top]

        if canonical_candidates:
            ranked_set = set(ranked)
            top_score = score_source.get(ranked[0], 0.0)
            canonical_applied = False
            for candidate in canonical_candidates:
                if candidate not in ranked_set:
                    continue
                if candidate == ranked[0]:
                    winner = candidate
                    canonical_applied = True
                    break
                canonical_score = score_source.get(candidate, 0.0)
                gap = top_score - canonical_score
                if gap <= CANONICAL_TIEBREAK_TOLERANCE + ROUND_SCORE_TOLERANCE:
                    winner = candidate
                    canonical_applied = True
                    logger.info(
                        f"Canonical tiebreak: local round rank-1 was "
                        f"{ranked[0][:16]}... (score={top_score:.4f}); "
                        f"deferring to canonical winner {candidate[:16]}... "
                        f"(score={canonical_score:.4f}, gap "
                        f"{gap*100:.2f}% within "
                        f"{CANONICAL_TIEBREAK_TOLERANCE*100:.1f}% tolerance)"
                    )
                    break
            if not canonical_applied:
                # Distinguish "canonical ranking agreed" from "canonical ranking
                # was fetched but no candidate was usable"; the latter means the
                # winner is purely local.
                logger.warning(
                    f"Canonical tiebreak did not apply: none of "
                    f"{len(canonical_candidates)} canonical candidate(s) is both "
                    f"locally ranked and within "
                    f"{CANONICAL_TIEBREAK_TOLERANCE*100:.1f}% of local rank-1 "
                    f"{ranked[0][:16]}... (score={top_score:.4f}); using the local "
                    f"ranking, which breaks exact ties by hotkey so it stays "
                    f"identical across validators"
                )

        weights[winner] = winner_weight

        dust_pool = max(0.0, miner_budget - winner_weight)
        dust_recipients = [hk for hk in ranked if hk != winner][:dust_top_n - 1]
        if dust_pool > 0 and dust_recipients:
            dust_raw = [dust_decay ** i for i in range(len(dust_recipients))]
            dust_total = sum(dust_raw)
            if dust_total > 0:
                for hk, raw in zip(dust_recipients, dust_raw):
                    weights[hk] = dust_pool * raw / dust_total

        logger.info(
            f"Round-only weights: winner={winner[:16]}... "
            f"winner_weight={winner_weight:.4f}, "
            f"dust_pool={dust_pool:.4f}, dust_recipients={len(dust_recipients)}"
        )
        return weights

    def get_rankings(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Optional[int]]:
        """Get current-round rankings. Unscored/zero-score miners get None.

        submission_times must be threaded through so ties resolve here exactly
        as they do in winner selection; without it every timestamp becomes
        float("inf") and the reported rank can contradict the chosen winner.
        """
        ranked = self._ranked_positive_eligible(
            miner_hotkeys, submission_times, ranking_scores
        )
        rankings: Dict[str, Optional[int]] = {hk: None for hk in miner_hotkeys}
        for rank, hk in enumerate(ranked, start=1):
            rankings[hk] = rank
        return rankings

    def build_weight_history(
        self,
        round_id: str,
        validator_hotkey: str,
        miner_hotkeys: List[str],
        weights: Dict[str, float],
        submission_times: Optional[Dict[str, float]] = None,
        ranking_hotkeys: Optional[List[str]] = None,
        ranking_scores: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Build the platform weight-history payload.

        ``miner_hotkeys`` remains the complete reporting population.  When
        private candidate normalization is active, ``ranking_hotkeys`` is the
        normalized subset that was actually eligible to occupy rank slots.
        Excluded rows deliberately receive no detailed public reason here.
        """
        ranking_population = (
            miner_hotkeys if ranking_hotkeys is None else ranking_hotkeys
        )
        rankings = self.get_rankings(
            ranking_population, submission_times, ranking_scores
        )

        entries = []
        for hk in miner_hotkeys:
            entries.append({
                "miner_hotkey": hk,
                "raw_score": self.last_raw_scores.get(hk),
                # Legacy platform schema field. Round-only scoring leaves it
                # empty rather than mirroring the current score.
                "ema_score": None,
                "rank": rankings.get(hk),
                "weight": weights.get(hk, 0.0),
                "eligible": self.is_eligible(hk),
                "participation_count": self.get_participation_count(hk),
            })

        return entries

    def get_stats(self) -> Dict[str, Any]:
        """Get current round statistics for logging."""
        all_hotkeys = list(self.round_scores.keys())
        score_values = list(self.round_scores.values())

        return {
            "total_miners_tracked": len(all_hotkeys),
            "eligible_count": sum(1 for hk in all_hotkeys if self.is_eligible(hk)),
            "rounds_tracked": len(self.round_history),
            "min_rounds_required": self.min_rounds,
            "top_round_score": max(score_values) if score_values else 0.0,
            "mean_round_score": sum(score_values) / len(score_values) if score_values else 0.0,
        }
